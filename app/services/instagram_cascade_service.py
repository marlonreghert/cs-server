"""Find a venue's Instagram handle from the cheapest source that has it.

Order is the feature: two free sources the platform already owns, then — only if
both come up empty — the paid Apify search. Every candidate is then VERIFIED
against Instagram's public profile metadata, which the old flow never did: a
search result scoring 0.75 used to be accepted without anyone checking the
profile existed.

Nothing here fails a venue. A source that raises, an archive that cannot be
read, a probe that times out, a judge that is not configured — each degrades the
answer and is recorded, and the run continues.
"""
from __future__ import annotations

import difflib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.api.instagram_profile_probe import EXIST_ABSENT, EXIST_PRESENT, EXIST_UNKNOWN
from app.metrics import (
    INSTAGRAM_CASCADE_PAID_CALLS_TOTAL,
    INSTAGRAM_CASCADE_RESULTS_TOTAL,
    INSTAGRAM_CASCADE_TIER_ATTEMPTS_TOTAL,
    INSTAGRAM_HANDLE_REJECTED_TOTAL,
    INSTAGRAM_JUDGE_TOTAL,
    INSTAGRAM_PROFILE_PROBE_TOTAL,
)
from app.models.instagram import VenueInstagram
from app.services.instagram_handle_sources import (
    PAID_SOURCES,
    SOURCE_APIFY_SEARCH,
    SOURCE_ARCHIVED_GMAPS,
    SOURCE_GOOGLE_WEBSITE,
    SOURCE_ORDER,
    extract_handle,
    normalize_handle,
)
from app.services.instagram_judge import MODE_UNAVAILABLE, cap_for_mode
from app.services.venue_photo_archive_service import parse_venue_ids

logger = logging.getLogger(__name__)

# Provenance: a handle printed on the venue's OWN listing is near-certain; a
# search result is a guess until the other signals back it up.
PROVENANCE_WEIGHT = {
    SOURCE_GOOGLE_WEBSITE: 0.75,
    SOURCE_ARCHIVED_GMAPS: 0.70,
    SOURCE_APIFY_SEARCH: 0.20,
}

EXISTENCE_BONUS = 0.15
# Weighted so a paid-tier candidate with an EXACT name match on a confirmed
# profile (0.20 + 0.15 + 0.40) just reaches the accept bar without the judge,
# while the measured ambiguous case — venue "Bercy Boa Viagem" vs profile
# "Bercy Village", similarity 0.76 — lands at ~0.65 and is adjudicated. Those
# two points are what the weights are fitted to.
NAME_WEIGHT = 0.40

DEFAULT_ACCEPT = 0.75
DEFAULT_AMBIGUOUS_LOW = 0.50


@dataclass
class CascadeResult:
    venue_id: str
    handle: Optional[str] = None
    source: Optional[str] = None
    accepted: bool = False
    confidence: float = 0.0
    signals: dict = field(default_factory=dict)
    profile_exists: str = EXIST_UNKNOWN
    display_name: Optional[str] = None
    name_similarity: Optional[float] = None
    judge: Optional[Any] = None
    judge_mode: Optional[str] = None
    rejections: list = field(default_factory=list)
    tier_errors: list = field(default_factory=list)
    tier_unavailable: list = field(default_factory=list)
    error: Optional[str] = None


def name_similarity(venue_name: Optional[str], display_name: Optional[str]) -> float:
    if not venue_name or not display_name:
        return 0.0
    return difflib.SequenceMatcher(
        None, venue_name.strip().lower(), display_name.strip().lower()
    ).ratio()


class InstagramCascadeService:
    def __init__(
        self,
        *,
        venue_dao,
        google_listing=None,
        archive=None,
        paid_search=None,
        probe=None,
        judge=None,
        photo_archive=None,
        accept_threshold: float = DEFAULT_ACCEPT,
        ambiguous_low: float = DEFAULT_AMBIGUOUS_LOW,
    ):
        self.venue_dao = venue_dao
        self.google_listing = google_listing
        self.archive = archive
        self.paid_search = paid_search
        self.probe = probe
        self.judge = judge
        self.photo_archive = photo_archive
        self.accept_threshold = accept_threshold
        self.ambiguous_low = ambiguous_low

    # ── sources ──────────────────────────────────────────────────────────────
    async def _candidates_from(self, source: str, venue_id: str, venue, result: CascadeResult):
        """Yield candidate (handle, display_name) pairs from one source.

        Returns None when the source is unavailable, so the caller can tell
        "nothing found" from "could not look".
        """
        if source == SOURCE_APIFY_SEARCH:
            if self.paid_search is None:
                result.tier_unavailable.append(source)
                return None
            INSTAGRAM_CASCADE_PAID_CALLS_TOTAL.inc()
            found = await self.paid_search.search(venue)
            out = []
            for c in found or []:
                handle = normalize_handle(c.get("username"))
                if handle:
                    out.append((handle, c.get("display_name")))
            return out

        reader = self.google_listing if source == SOURCE_GOOGLE_WEBSITE else self.archive
        if reader is None:
            result.tier_unavailable.append(source)
            return None
        website = await reader.website_for(venue_id, venue)
        if not website:
            return []
        handle, reason = extract_handle(website)
        if reason:
            result.rejections.append(reason)
            INSTAGRAM_HANDLE_REJECTED_TOTAL.labels(reason=reason).inc()
            return []
        return [(handle, None)]

    # ── scoring ──────────────────────────────────────────────────────────────
    def _score(self, source, probe_result, venue, display_name) -> tuple[float, dict]:
        disp = display_name or (probe_result.display_name if probe_result else None)
        sim = name_similarity(getattr(venue, "venue_name", None), disp)
        exists_bonus = (
            EXISTENCE_BONUS
            if probe_result is not None and probe_result.existence == EXIST_PRESENT
            else 0.0
        )
        signals = {
            "provenance": PROVENANCE_WEIGHT.get(source, 0.2),
            "profile_exists": exists_bonus,
            "name_similarity": round(sim, 4),
        }
        confidence = signals["provenance"] + exists_bonus + NAME_WEIGHT * sim
        return min(confidence, 1.0), signals

    # ── one venue ────────────────────────────────────────────────────────────
    async def discover(self, venue_id: str, config: Optional[dict] = None) -> CascadeResult:
        config = dict(config or {})
        result = CascadeResult(venue_id=venue_id)
        venue = self.venue_dao.get_venue(venue_id)

        # Freshness gate: skip before consulting ANY source, free or not.
        if not config.get("force_refresh"):
            existing = self.venue_dao.get_venue_instagram(venue_id)
            if existing is not None:
                result.handle = existing.instagram_handle
                result.accepted = bool(existing.instagram_handle)
                result.source = getattr(existing, "source", None)
                result.confidence = existing.confidence_score or 0.0
                return result

        best: Optional[CascadeResult] = None
        for source in SOURCE_ORDER:
            if not self._source_enabled(source, config):
                continue
            INSTAGRAM_CASCADE_TIER_ATTEMPTS_TOTAL.labels(source=source).inc()
            try:
                candidates = await self._candidates_from(source, venue_id, venue, result)
            except PermissionError as e:
                # The archive read is IAM-gated; an unapplied policy must look
                # like "cannot look here", not like a broken pipeline.
                result.tier_unavailable.append(source)
                logger.warning(f"[InstagramCascade] {source} unavailable: {e}")
                continue
            except Exception as e:
                result.tier_errors.append(source)
                logger.error(f"[InstagramCascade] {source} failed for {venue_id}: {e}")
                continue
            if candidates is None or not candidates:
                continue

            for handle, display in candidates:
                probe_result = await self._probe(handle)
                confidence, signals = self._score(source, probe_result, venue, display)
                cand = CascadeResult(
                    venue_id=venue_id, handle=handle, source=source,
                    confidence=confidence, signals=signals,
                    profile_exists=probe_result.existence if probe_result else EXIST_UNKNOWN,
                    display_name=(display or (probe_result.display_name if probe_result else None)),
                    name_similarity=signals["name_similarity"],
                )
                if probe_result is not None and probe_result.existence == EXIST_ABSENT:
                    # Verified NOT to exist — never accept, keep looking.
                    continue
                if self.ambiguous_low <= confidence < self.accept_threshold:
                    cand = await self._adjudicate(cand, venue, probe_result)
                if best is None or cand.confidence > best.confidence:
                    best = cand
                if cand.confidence >= self.accept_threshold:
                    break
            if best is not None and best.confidence >= self.accept_threshold:
                break

        return self._finalize(result, best, venue_id)

    def _source_enabled(self, source: str, config: dict) -> bool:
        if source in PAID_SOURCES and config.get("tier_apify_search_enabled") is False:
            return False
        return config.get(f"tier_{source}_enabled", True) is not False

    async def _probe(self, handle):
        if self.probe is None:
            return None
        probe_result = await self.probe.fetch(handle)
        INSTAGRAM_PROFILE_PROBE_TOTAL.labels(result=probe_result.existence).inc()
        return probe_result

    async def _adjudicate(self, cand: CascadeResult, venue, probe_result) -> CascadeResult:
        if self.judge is None:
            cand.judge_mode = MODE_UNAVAILABLE
            INSTAGRAM_JUDGE_TOTAL.labels(mode=MODE_UNAVAILABLE, verdict="skipped").inc()
            return cand
        photos = []
        if self.photo_archive is not None:
            try:
                photos = await self.photo_archive.venue_photos(cand.venue_id)
            except Exception as e:
                logger.warning(f"[InstagramCascade] venue photos unavailable: {e}")
        profile = {
            "display_name": cand.display_name,
            "image_url": getattr(probe_result, "image_url", None),
            "followers_count": getattr(probe_result, "followers_count", None),
        }
        verdict = await self.judge.judge(
            venue=venue, candidate=cand.handle, profile=profile, venue_photos=photos
        )
        if verdict is None:
            cand.judge_mode = MODE_UNAVAILABLE
            INSTAGRAM_JUDGE_TOTAL.labels(mode=MODE_UNAVAILABLE, verdict="error").inc()
            return cand
        cand.judge = verdict
        cand.judge_mode = verdict.mode
        INSTAGRAM_JUDGE_TOTAL.labels(
            mode=verdict.mode, verdict="match" if verdict.is_match else "no_match"
        ).inc()
        cand.confidence = cap_for_mode(
            verdict.mode, verdict.confidence if verdict.is_match else min(cand.confidence, 0.4)
        )
        return cand

    def _finalize(self, result: CascadeResult, best, venue_id) -> CascadeResult:
        if best is None:
            result.accepted = False
            self._persist(result, status="not_found")
            INSTAGRAM_CASCADE_RESULTS_TOTAL.labels(source="none", result="not_found").inc()
            return result

        # Carry the run-level bookkeeping onto the winning candidate.
        best.rejections = result.rejections
        best.tier_errors = result.tier_errors
        best.tier_unavailable = result.tier_unavailable
        best.accepted = best.confidence >= self.accept_threshold
        status = "found" if best.accepted else (
            "low_confidence" if best.confidence >= self.ambiguous_low else "not_found"
        )
        self._persist(best, status=status)
        INSTAGRAM_CASCADE_RESULTS_TOTAL.labels(
            source=best.source or "none",
            result="accepted" if best.accepted else status,
        ).inc()
        return best

    def _persist(self, result: CascadeResult, *, status: str) -> None:
        try:
            self.venue_dao.set_venue_instagram(
                VenueInstagram(
                    venue_id=result.venue_id,
                    instagram_handle=result.handle if status != "not_found" else None,
                    instagram_url=(
                        f"https://instagram.com/{result.handle}"
                        if result.handle and status != "not_found"
                        else None
                    ),
                    confidence_score=result.confidence,
                    status=status,
                    source=result.source,
                    discovery={
                        "source": result.source,
                        "confidence": result.confidence,
                        "signals": result.signals,
                        "profile_exists": result.profile_exists,
                        "display_name": result.display_name,
                        "name_similarity": result.name_similarity,
                        "judge_mode": result.judge_mode,
                        "judge_reason": getattr(result.judge, "reason", None),
                        "checked_at": time.time(),
                    },
                )
            )
        except Exception as e:
            result.error = str(e)
            logger.error(f"[InstagramCascade] persist failed for {result.venue_id}: {e}")

    # ── whole catalog ────────────────────────────────────────────────────────
    async def run(self, config: Optional[dict] = None) -> dict:
        config = dict(config or {})
        catalogue = list(self.venue_dao.list_servable_venue_ids() or [])
        # The operator's subset is a COST CONTROL, not a hint. Ignoring it turns
        # a two-venue check into a full-catalogue run — with force_refresh, ~451
        # paid searches. An unknown id is reported, never fatal: a typo in one
        # id must not cancel the rest of the run.
        requested = parse_venue_ids(config.get("venue_ids"))
        if requested:
            known = set(catalogue)
            venue_ids = [v for v in requested if v in known]
            unknown = [v for v in requested if v not in known]
            if unknown:
                logger.warning(
                    f"[InstagramCascade] {len(unknown)} requested venue id(s) are "
                    f"not servable and were skipped: {unknown[:10]}"
                )
        else:
            venue_ids = catalogue
            unknown = []
        summary = {
            "considered": 0, "accepted": 0, "not_found": 0, "errors": 0,
            "paid_calls_before": 0, "unknown_venue_ids": len(unknown),
        }
        for venue_id in venue_ids:
            summary["considered"] += 1
            try:
                res = await self.discover(venue_id, config)
                if res.accepted:
                    summary["accepted"] += 1
                else:
                    summary["not_found"] += 1
            except Exception as e:  # one venue must never end the run
                summary["errors"] += 1
                logger.error(f"[InstagramCascade] venue {venue_id} failed: {e}")
        logger.info(f"[InstagramCascade] run complete: {summary}")
        return summary
