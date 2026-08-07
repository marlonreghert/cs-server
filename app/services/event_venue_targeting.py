"""Event venue targeting: which venues are worth crawling for events.

Two-stage targeting so paid event crawls (future plans: Instagram scrapes,
model calls) only ever aim at venues that plausibly host events. See
plans/260804_event-venue-targeting.md.

Stage 1 — the category gate (free, whole servable catalog). A venue's resolved
display category (app/models/venue_category.py) is checked against an
admin-tunable allow-list; a venue whose vibe profile carries a live-music or
club signal is promoted even when its category fails, because reading a label
already in RDS costs nothing. `OTHER` is never a candidate by default — the
deliberate opposite of venue_eligibility's block-list posture, because an
unknown venue here merely goes uncrawled (free, reversible) rather than being
hidden from users.

Stage 2 — the evidence gate (bounded to the top N category survivors by
priority, ordered via `list_event_candidate_ids_by_priority` — NOT
`list_servable_venue_ids_by_priority`, which drops `venue_source='google_only'`
venues for BestTime-refresh reasons that do not apply here). Scores from data
already paid for: flyer-classified archive photos and Instagram caption event
markers, both within a lookback window. Zero model calls, zero external API
calls, by construction — everything it reads was already fetched by an earlier,
separately-billed pipeline.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.metrics import (
    EVENT_CANDIDATE_VENUES,
    EVENT_TARGETING_CONFIG_FALLBACK_TOTAL,
    EVENT_TARGETING_VENUES_TOTAL,
)
from app.models.venue_category import CATEGORIES, load_category_map, resolve_category
from app.services.event_caption_matcher import find_event_markers

logger = logging.getLogger(__name__)

ADMIN_CONFIG_EVENT_CANDIDATE_CATEGORIES_KEY = "admin_config:event_candidate_categories"

# ── tiers ─────────────────────────────────────────────────────────────────────
TIER_UNEVALUATED = "unevaluated"
TIER_CATEGORY_CANDIDATE = "category_candidate"
TIER_EVIDENCE_CONFIRMED = "evidence_confirmed"
TIER_EVIDENCE_REJECTED = "evidence_rejected"
TIER_EXCLUDED_CATEGORY = "excluded_category"
ALL_TIERS: tuple[str, ...] = (
    TIER_UNEVALUATED, TIER_CATEGORY_CANDIDATE, TIER_EVIDENCE_CONFIRMED,
    TIER_EVIDENCE_REJECTED, TIER_EXCLUDED_CATEGORY,
)
# The only two venue_source values (migration 0021_venue_source) — the gauge's
# second label is cheap because this set never grows.
VENUE_SOURCES: tuple[str, ...] = ("besttime", "google_only")

# ── stage 1: category allow-list + free vibe-signal override ─────────────────
DEFAULT_ALLOWED_CATEGORIES: tuple[str, ...] = (
    "NIGHTCLUB", "LIVE_MUSIC", "EVENT_VENUE", "KARAOKE", "CASINO",
    "ENTERTAINMENT", "PUB", "BAR", "COCKTAIL_BAR", "BREWERY",
)
DEFAULT_VIBE_MUSIC_FORMAT_OVERRIDES: tuple[str, ...] = (
    "Banda ao vivo", "Roda de samba", "DJ",
)
DEFAULT_VIBE_ESTILO_OVERRIDES: tuple[str, ...] = ("Balada", "Club")


@dataclass(frozen=True)
class EventCandidateCategoryConfig:
    allowed_categories: frozenset
    vibe_music_format_overrides: frozenset
    vibe_estilo_overrides: frozenset

    @classmethod
    def defaults(cls) -> "EventCandidateCategoryConfig":
        return cls(
            allowed_categories=frozenset(DEFAULT_ALLOWED_CATEGORIES),
            vibe_music_format_overrides=frozenset(DEFAULT_VIBE_MUSIC_FORMAT_OVERRIDES),
            vibe_estilo_overrides=frozenset(DEFAULT_VIBE_ESTILO_OVERRIDES),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "EventCandidateCategoryConfig":
        """Strict parse — raises TypeError/ValueError on any malformed shape.

        The one caller that reads live admin config (`load_event_candidate_categories`)
        is the sole place that catches these and falls back to defaults, so this
        stays strict rather than swallowing bad input quietly.
        """
        if not isinstance(data, dict):
            raise TypeError("event candidate category config must be a JSON object")
        defaults = cls.defaults()

        def _string_set(key: str, fallback: frozenset) -> frozenset:
            if key not in data:
                return fallback
            value = data[key]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise TypeError(f"'{key}' must be a list of strings")
            return frozenset(value)

        allowed = _string_set("allowed_categories", defaults.allowed_categories)
        for cat in allowed:
            if cat.upper() not in CATEGORIES:
                raise ValueError(f"unknown category '{cat}' in allowed_categories")
        allowed = frozenset(c.upper() for c in allowed)
        music = _string_set(
            "vibe_music_format_overrides", defaults.vibe_music_format_overrides
        )
        estilo = _string_set("vibe_estilo_overrides", defaults.vibe_estilo_overrides)
        return cls(
            allowed_categories=allowed,
            vibe_music_format_overrides=music,
            vibe_estilo_overrides=estilo,
        )


def validate_event_candidate_categories_config(value: dict) -> dict:
    """Validate an admin write to admin_config:event_candidate_categories
    before persistence (AdminConfigService.set dispatches here). Raises on a
    malformed shape; returns the normalized dict actually persisted, so what
    is stored is exactly what `from_dict` would parse back."""
    cfg = EventCandidateCategoryConfig.from_dict(value)
    return {
        "allowed_categories": sorted(cfg.allowed_categories),
        "vibe_music_format_overrides": sorted(cfg.vibe_music_format_overrides),
        "vibe_estilo_overrides": sorted(cfg.vibe_estilo_overrides),
    }


def load_event_candidate_categories(
    redis_like,
) -> tuple[EventCandidateCategoryConfig, Optional[str]]:
    """Read the admin override, falling back to the shipped defaults on any
    problem. Returns (config, fallback_reason); fallback_reason is None unless
    the caller should count a fallback metric (a missing key is NOT a fallback
    — it is the expected pre-first-write state).
    """
    if redis_like is None:
        return EventCandidateCategoryConfig.defaults(), None
    try:
        raw = redis_like.get(ADMIN_CONFIG_EVENT_CANDIDATE_CATEGORIES_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[event_venue_targeting] config read failed, using defaults: {e}")
        return EventCandidateCategoryConfig.defaults(), "unreadable"
    if raw is None:
        return EventCandidateCategoryConfig.defaults(), None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning(f"[event_venue_targeting] config invalid JSON, using defaults: {e}")
        return EventCandidateCategoryConfig.defaults(), "invalid_json"
    try:
        return EventCandidateCategoryConfig.from_dict(data), None
    except (TypeError, ValueError) as e:
        logger.warning(f"[event_venue_targeting] config invalid shape, using defaults: {e}")
        return EventCandidateCategoryConfig.defaults(), "invalid_shape"


@dataclass(frozen=True)
class CategoryGateResult:
    passed: bool
    reason: str  # the category name, or "vibe:<label>" when a signal promoted it


def category_gate(
    category: Optional[str],
    vibe_profile: Optional[dict],
    cfg: Optional[EventCandidateCategoryConfig] = None,
) -> CategoryGateResult:
    """Stage 1: free category allow-list + vibe-signal override.

    `vibe_profile` is `{"music_format": [...], "estilo_do_lugar": [...]}` (plain
    label lists) or None. `OTHER` is excluded unless promoted by a vibe signal
    — never a default candidate.
    """
    cfg = cfg or EventCandidateCategoryConfig.defaults()
    cat = (category or "OTHER").upper()
    if cat in cfg.allowed_categories:
        return CategoryGateResult(True, cat)

    profile = vibe_profile or {}
    for label in profile.get("music_format") or []:
        if label in cfg.vibe_music_format_overrides:
            return CategoryGateResult(True, f"vibe:{label}")
    for label in profile.get("estilo_do_lugar") or []:
        if label in cfg.vibe_estilo_overrides:
            return CategoryGateResult(True, f"vibe:{label}")

    return CategoryGateResult(False, cat)


# ── stage 2: the bounded evidence gate ────────────────────────────────────────
# Raised from 25 (2026-08-07 RCA): a live run had 771 category-gate survivors
# against this bound, so 746 venues were never evaluated and
# evidence_confirmed came back 0 — extraction then had nothing to run on. The
# evidence gate makes ZERO model calls and ZERO external API calls
# (plans/260804_event-venue-targeting.md §B: everything it reads was already
# fetched by an earlier, separately-billed pipeline), so a bound sized as if
# this stage were expensive protected nothing and made the happy path
# unreachable. 1000 comfortably covers today's catalog with room to grow, and
# stays a real operator-tunable control rather than a silent floor.
DEFAULT_MAX_EVIDENCE_VENUES = 1000
DEFAULT_MIN_EVIDENCE_POSTS = 3
DEFAULT_LOOKBACK_DAYS = 60

# Passed to list_event_candidate_ids_by_priority to get the FULL priority
# ordering rather than a DB-side truncation. The real cap (max_evidence_venues)
# is applied afterwards, in-process, against the category survivors: a LIMIT
# here would truncate BEFORE the category filter and could silently drop true
# top-N event candidates whose priority trails a higher-priority non-candidate.
FULL_PRIORITY_LIMIT = 1_000_000


class InvalidEventTargetingConfig(ValueError):
    pass


def parse_event_targeting_config(config: Optional[dict]) -> dict:
    """Validate and normalize an event_targeting run configuration."""
    cfg = dict(config or {})

    def _non_negative_int(value, field: str, default: int) -> int:
        if value in (None, ""):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise InvalidEventTargetingConfig(f"{field} must be an integer, got {value!r}")
        if parsed < 0:
            raise InvalidEventTargetingConfig(f"{field} must be >= 0, got {parsed}")
        return parsed

    return {
        "max_evidence_venues": _non_negative_int(
            cfg.get("max_evidence_venues"), "max_evidence_venues",
            DEFAULT_MAX_EVIDENCE_VENUES,
        ),
        "min_evidence_posts": _non_negative_int(
            cfg.get("min_evidence_posts"), "min_evidence_posts",
            DEFAULT_MIN_EVIDENCE_POSTS,
        ),
        "lookback_days": _non_negative_int(
            cfg.get("lookback_days"), "lookback_days", DEFAULT_LOOKBACK_DAYS,
        ),
        "recompute": bool(cfg.get("recompute")),
        "dry_run": bool(cfg.get("dry_run")),
    }


def evidence_verdict(evidence_score: int, min_evidence_posts: int) -> str:
    return (
        TIER_EVIDENCE_CONFIRMED if evidence_score >= min_evidence_posts
        else TIER_EVIDENCE_REJECTED
    )


def _parse_timestamp(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def caption_evidence(
    posts: list, since: datetime, matcher=find_event_markers,
) -> tuple[int, list[str]]:
    """(matching_count, sample_excerpts) over `posts` within the lookback
    window. A post with no parseable timestamp is treated as within-window —
    the caption is cheap evidence either way and must not be silently dropped
    for a data gap that predates timestamps being captured.
    """
    count = 0
    samples: list[str] = []
    for post in posts or []:
        caption = post.get("caption") if isinstance(post, dict) else getattr(post, "caption", None)
        ts_raw = post.get("timestamp") if isinstance(post, dict) else getattr(post, "timestamp", None)
        ts = _parse_timestamp(ts_raw)
        if ts is not None and ts < since:
            continue
        if matcher(caption):
            count += 1
            if len(samples) < 3:
                samples.append((caption or "")[:140])
    return count, samples


class NullFlyerEvidenceSource:
    """No media archive wired — flyer evidence is always zero, never an error.
    The evidence gate still runs on captions alone."""

    async def flyer_count(self, venue_id: str, since: datetime) -> tuple[int, list[str]]:
        return 0, []


def _run_prefix_date(prefix: str) -> Optional[datetime]:
    """Best-effort UTC date parsed out of a run/day archive prefix's
    year=/month=/day= or legacy dt= segments, for lookback filtering without
    decoding the run id."""
    import re

    m = re.search(r"year=(\d{4})/month=(\d{2})/day=(\d{2})", prefix)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return datetime(y, mo, d, tzinfo=timezone.utc)
    m = re.search(r"dt=(\d{4})-(\d{2})-(\d{2})", prefix)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return datetime(y, mo, d, tzinfo=timezone.utc)
    return None


class ArchivedFlyerEvidenceSource:
    """Counts flyer-classified photos already archived to S3 — zero new
    provider calls, zero model calls. Walks `archive_source` run manifests
    whose date falls within the lookback window and sums each venue's
    already-computed `media.by_category['flyer']` count."""

    def __init__(self, media_store, archive_source: str):
        self.media_store = media_store
        self.archive_source = archive_source

    async def flyer_count(self, venue_id: str, since: datetime) -> tuple[int, list[str]]:
        if self.media_store is None:
            return 0, []
        try:
            prefixes = await self.media_store.list_run_prefixes(self.archive_source)
        except Exception as e:
            logger.warning(f"[event_venue_targeting] listing archive runs failed: {e}")
            return 0, []
        total = 0
        samples: list[str] = []
        for prefix in prefixes:
            run_date = _run_prefix_date(prefix)
            if run_date is not None and run_date < since:
                continue
            try:
                manifest = await self.media_store.read_manifest(prefix, venue_id)
            except Exception as e:
                logger.warning(f"[event_venue_targeting] manifest read failed: {e}")
                continue
            if not manifest:
                continue
            by_category = ((manifest.get("media") or {}).get("by_category") or {})
            count = int(by_category.get("flyer") or 0)
            total += count
            if count:
                samples.append(f"{count} flyer photo(s) in {prefix}venue_id={venue_id}/")
        return total, samples[:3]


def resolve_event_candidate_ids(
    venue_dao, *, include_category_candidates: bool = False,
) -> list[str]:
    """Resolve `eligibility.mode = event_candidates` to a venue id set — the
    single definition every event job (archive, extraction, ...) inherits via
    the shared run-config parser. Deterministic order: the cap that follows
    (max_venues) is what narrows the run, not this ordering.
    """
    tiers = [TIER_EVIDENCE_CONFIRMED]
    if include_category_candidates:
        tiers.append(TIER_CATEGORY_CANDIDATE)
    profiles = venue_dao.list_venue_event_profiles(tiers=tiers)
    return sorted(p["venue_id"] for p in profiles)


def _labels(taxonomy_category) -> list[str]:
    """Extract labels from a TaxonomyCategory model, a plain list, or None."""
    if taxonomy_category is None:
        return []
    if isinstance(taxonomy_category, dict):
        return list(taxonomy_category.get("labels") or [])
    return list(getattr(taxonomy_category, "labels", None) or taxonomy_category or [])


class EventVenueTargetingService:
    """Orchestrates the two-stage event venue targeting run."""

    def __init__(
        self,
        venue_dao,
        redis_client=None,
        flyer_evidence_source=None,
        now_provider=None,
    ):
        self.venue_dao = venue_dao
        self.redis_client = redis_client
        self.flyer_evidence_source = flyer_evidence_source or NullFlyerEvidenceSource()
        self._now = now_provider or (lambda: datetime.now(timezone.utc))

    def _category_for(self, venue_id: str, google_map: dict, besttime_map: dict) -> str:
        venue = self.venue_dao.get_venue(venue_id)
        vibe_attrs = self.venue_dao.get_vibe_attributes(venue_id)
        google_type = getattr(vibe_attrs, "google_primary_type", None) if vibe_attrs else None
        besttime_type = getattr(venue, "venue_type", None) if venue else None
        return resolve_category(
            google_type=google_type, besttime_type=besttime_type,
            google_map=google_map, besttime_map=besttime_map,
        )

    def _vibe_profile_dict(self, venue_id: str) -> dict:
        profile = self.venue_dao.get_venue_vibe_profile(venue_id)
        if profile is None:
            return {"music_format": [], "estilo_do_lugar": []}
        return {
            "music_format": _labels(getattr(profile, "music_format", None)),
            "estilo_do_lugar": _labels(getattr(profile, "estilo_do_lugar", None)),
        }

    def _has_instagram_handle(self, venue_id: str) -> bool:
        instagram = self.venue_dao.get_venue_instagram(venue_id)
        if instagram is None:
            return False
        has_method = getattr(instagram, "has_instagram", None)
        if callable(has_method):
            return bool(has_method())
        return bool(getattr(instagram, "instagram_handle", None))

    def _update_candidate_gauge(self) -> None:
        counts: dict[tuple, int] = {
            (tier, source): 0 for tier in ALL_TIERS for source in VENUE_SOURCES
        }
        for profile in self.venue_dao.list_venue_event_profiles():
            tier = profile.get("tier")
            source = profile.get("venue_source") or "besttime"
            key = (tier, source)
            counts[key] = counts.get(key, 0) + 1
        for (tier, source), count in counts.items():
            EVENT_CANDIDATE_VENUES.labels(tier=tier, venue_source=source).set(count)

    async def run(self, config: Optional[dict] = None) -> dict:
        cfg = parse_event_targeting_config(config)
        now = self._now()
        since = now - timedelta(days=cfg["lookback_days"])

        category_cfg, fallback_reason = load_event_candidate_categories(self.redis_client)
        if fallback_reason is not None:
            EVENT_TARGETING_CONFIG_FALLBACK_TOTAL.labels(reason=fallback_reason).inc()
        google_map, besttime_map = load_category_map(self.redis_client)

        catalog = list(self.venue_dao.list_servable_venue_ids() or [])
        category_pass_ids: set[str] = set()
        category_reason_by_id: dict[str, str] = {}
        tier_changes: list[dict] = []

        # ── Stage 1: free category gate over the whole servable catalog ──────
        for venue_id in catalog:
            existing = self.venue_dao.get_venue_event_profile(venue_id)
            category = self._category_for(venue_id, google_map, besttime_map)
            profile_dict = self._vibe_profile_dict(venue_id)
            gate = category_gate(category, profile_dict, category_cfg)
            EVENT_TARGETING_VENUES_TOTAL.labels(
                stage="category",
                verdict="category_pass" if gate.passed else TIER_EXCLUDED_CATEGORY,
            ).inc()

            if not gate.passed:
                tier_changes.append({"venue_id": venue_id, "tier": TIER_EXCLUDED_CATEGORY})
                if not cfg["dry_run"]:
                    self.venue_dao.upsert_venue_event_profile(
                        venue_id, tier=TIER_EXCLUDED_CATEGORY, category_pass=False,
                        category_reason=gate.reason, evidence_score=None,
                        evidence_sample=None, evaluated_at=None,
                    )
                continue

            category_pass_ids.add(venue_id)
            category_reason_by_id[venue_id] = gate.reason

            # A venue already past the evidence gate (confirmed/rejected) or
            # marked unevaluated keeps that tier here: re-running stage 1 (e.g.
            # after an admin allow-list edit) must never erase evidence stage-2
            # work already paid for. Only a fresh venue gets category_candidate.
            if existing and existing.get("tier") in (
                TIER_EVIDENCE_CONFIRMED, TIER_EVIDENCE_REJECTED, TIER_UNEVALUATED,
            ):
                continue

            tier_changes.append({"venue_id": venue_id, "tier": TIER_CATEGORY_CANDIDATE})
            if not cfg["dry_run"]:
                self.venue_dao.upsert_venue_event_profile(
                    venue_id, tier=TIER_CATEGORY_CANDIDATE, category_pass=True,
                    category_reason=gate.reason, evidence_score=None,
                    evidence_sample=None, evaluated_at=None,
                )

        # ── Stage 2: bounded evidence gate over the top N by priority ────────
        # No venue_source filter: a google_only venue (BestTime could not
        # forecast it) is eligible here even though it is excluded from the
        # BestTime-scoped priority selection.
        priority_ordered = self.venue_dao.list_event_candidate_ids_by_priority(
            FULL_PRIORITY_LIMIT
        )
        ordered_candidates = [vid for vid in priority_ordered if vid in category_pass_ids]

        needing_evaluation = []
        for vid in ordered_candidates:
            profile = self.venue_dao.get_venue_event_profile(vid)
            already_evaluated = bool(profile and profile.get("evaluated_at"))
            if already_evaluated and not cfg["recompute"]:
                continue
            needing_evaluation.append(vid)

        to_evaluate = needing_evaluation[: cfg["max_evidence_venues"]]

        for venue_id in to_evaluate:
            if not self._has_instagram_handle(venue_id):
                tier = TIER_UNEVALUATED
                score = None
                sample = {"reason": "no_instagram_handle"}
            else:
                flyer_count, flyer_samples = await self.flyer_evidence_source.flyer_count(
                    venue_id, since
                )
                ig_posts = self.venue_dao.get_venue_ig_posts(venue_id)
                posts = list(getattr(ig_posts, "posts", None) or []) if ig_posts else []
                caption_count, caption_samples = caption_evidence(posts, since)
                score = flyer_count + caption_count
                tier = evidence_verdict(score, cfg["min_evidence_posts"])
                sample = {
                    "flyer_count": flyer_count,
                    "caption_matches": caption_count,
                    "lookback_days": cfg["lookback_days"],
                    "samples": (flyer_samples + caption_samples)[:5],
                }

            EVENT_TARGETING_VENUES_TOTAL.labels(stage="evidence", verdict=tier).inc()
            tier_changes.append({"venue_id": venue_id, "tier": tier})
            if not cfg["dry_run"]:
                self.venue_dao.upsert_venue_event_profile(
                    venue_id, tier=tier, category_pass=True,
                    category_reason=category_reason_by_id.get(venue_id),
                    evidence_score=score, evidence_sample=sample, evaluated_at=now,
                )

        if not cfg["dry_run"]:
            self._update_candidate_gauge()

        return {
            "catalog_size": len(catalog),
            "category_pass": len(category_pass_ids),
            "evidence_evaluated": len(to_evaluate),
            "tier_changes": tier_changes,
            "dry_run": cfg["dry_run"],
            "config_fallback": fallback_reason,
        }
