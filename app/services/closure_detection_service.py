"""Detect permanently-closed venues from the review evidence already in RDS.

A venue whose most recent review reports it closed must not be served as an
open, busy place. The evidence is already stored — `google_places.reviews` is an
RDS enrichment table projected to Redis for display — so detection is a read over
data we already hold, with no extra upstream fetch.

Design constraints, all load-bearing:

* **Only the newest review decides.** A closure phrase buried in history is
  stale gossip: venues change hands and reopen. If a more recent ordinary review
  exists, the venue is open.
* **Reversible, never destructive.** Closure is recorded as a signal, not a
  lifecycle change or a soft-delete, so a newer ordinary review restores the
  venue on the next cycle with no manual intervention. This mirrors how
  eligibility became a dynamic serving view rather than a soft-delete sweep
  (`migrations/versions/0009_eligibility_serving_view.py`).
* **Confidence gates serving.** Only `high` confidence excludes a venue, the
  same contract `venue_eligibility.EligibilityResult` uses. A closure claim
  contradicted by a near-contemporaneous ordinary review is `low`: recorded for
  an operator, but not acted on.
* **Per-venue isolation.** One poisoned payload must never abort the run or
  affect another venue.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - metrics are optional in unit-test harnesses
    from app.metrics import (
        CLOSURE_DETECTION_DURATION_SECONDS,
        CLOSURE_DETECTION_ERRORS_TOTAL,
        CLOSURE_DETECTION_RUNS_TOTAL,
        VENUES_CLOSED_FLAGGED,
    )
except ImportError:  # pragma: no cover
    CLOSURE_DETECTION_DURATION_SECONDS = None
    CLOSURE_DETECTION_ERRORS_TOTAL = None
    CLOSURE_DETECTION_RUNS_TOTAL = None
    VENUES_CLOSED_FLAGGED = None


def _count(metric, **labels) -> None:
    """Increment a counter. Observability must never break a run."""
    if metric is None:
        return
    try:
        (metric.labels(**labels) if labels else metric).inc()
    except Exception:  # pragma: no cover - defensive
        logger.debug("[Closure] counter write failed", exc_info=True)


def _set_gauge(metric, value, **labels) -> None:
    if metric is None:
        return
    try:
        (metric.labels(**labels) if labels else metric).set(value)
    except Exception:  # pragma: no cover - defensive
        logger.debug("[Closure] gauge write failed", exc_info=True)


def _observe_duration(metric, value) -> None:
    if metric is None:
        return
    try:
        metric.observe(value)
    except Exception:  # pragma: no cover - defensive
        logger.debug("[Closure] histogram write failed", exc_info=True)

REASON_REVIEW_REPORTS_CLOSED = "review_reports_closed"

# Phrases that assert permanent closure. Matched against accent-stripped,
# lowercased text, so "não existe mais" is written here without accents.
DEFAULT_CLOSURE_PHRASES = [
    "fechou",
    "fechado permanentemente",
    "permanentemente fechado",
    "encerrou as atividades",
    "encerrou suas atividades",
    "nao existe mais",
    "nao funciona mais",
    "nao abre mais",
]

# Qualifiers that turn a closure phrase into a temporary or speculative one.
# Checked against the window of text around the match, not the whole review, so
# an unrelated later sentence cannot suppress a genuine closure report.
DEFAULT_NEGATION_PHRASES = [
    "hoje",
    "para reforma",
    "por reforma",
    "para ferias",
    "temporariamente",
    "temporario",
    "vai fechar",
    "ia fechar",
    "quase fechou",
    "nao fechou",
    "reabriu",
]

# A contradicting ordinary review published within this window *after* the
# closure claim downgrades it to low confidence.
DEFAULT_CONTRADICTION_WINDOW_DAYS = 90

_NEGATION_WINDOW_CHARS = 40


def _normalize(text: Optional[str]) -> str:
    """Lowercase and strip accents so phrase matching is diacritic-insensitive."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", str(text).lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 publish time, tolerating a trailing Z and fractional
    seconds. Returns None for anything unorderable — such a review must never
    decide closure, because closure depends entirely on ordering."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    # Trim over-precise fractional seconds (Google emits 9 digits; fromisoformat
    # accepts at most 6 before 3.11 semantics settle).
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class ClosureSignal:
    """The recorded outcome of evaluating one venue's reviews."""

    venue_id: str
    closed: bool
    reason: Optional[str] = None
    confidence: str = "high"  # "high" | "low"
    evidence_publish_time: Optional[str] = None
    matched_phrase: Optional[str] = None

    def excludes_from_serving(self) -> bool:
        """Only a high-confidence closure removes a venue from serving."""
        return self.closed and self.confidence == "high"

    def to_dict(self) -> dict:
        return {
            "venue_id": self.venue_id,
            "closed": self.closed,
            "reason": self.reason,
            "confidence": self.confidence,
            "evidence_publish_time": self.evidence_publish_time,
            "matched_phrase": self.matched_phrase,
        }


def _matched_phrase(normalized_text: str, phrases, negations) -> Optional[str]:
    """The closure phrase asserted by this text, or None.

    A phrase preceded or followed by a temporary/speculative qualifier within a
    short window does not count — "fechado para reforma" and "vai fechar" are
    not permanent closure.
    """
    for phrase in phrases:
        norm_phrase = _normalize(phrase)
        start = normalized_text.find(norm_phrase)
        while start != -1:
            window_start = max(0, start - _NEGATION_WINDOW_CHARS)
            window_end = min(
                len(normalized_text),
                start + len(norm_phrase) + _NEGATION_WINDOW_CHARS,
            )
            window = normalized_text[window_start:window_end]
            if not any(_normalize(n) in window for n in negations):
                return phrase
            start = normalized_text.find(norm_phrase, start + 1)
    return None


def evaluate_reviews(
    venue_id: str,
    reviews: Any,
    *,
    phrases=None,
    negations=None,
    contradiction_window_days: int = DEFAULT_CONTRADICTION_WINDOW_DAYS,
) -> ClosureSignal:
    """Evaluate one venue's reviews for a permanent-closure claim.

    Pure: no I/O, no clock dependency beyond the reviews' own timestamps.
    """
    phrases = phrases if phrases is not None else DEFAULT_CLOSURE_PHRASES
    negations = negations if negations is not None else DEFAULT_NEGATION_PHRASES

    if not isinstance(reviews, (list, tuple)):
        raise TypeError(
            f"reviews payload for {venue_id} is {type(reviews).__name__}, expected a list"
        )

    # Only reviews we can order participate: closure is decided by recency.
    dated = []
    for review in reviews:
        if isinstance(review, dict):
            text, published = review.get("text"), review.get("publish_time")
        else:
            text, published = getattr(review, "text", None), getattr(
                review, "publish_time", None
            )
        parsed = _parse_time(published)
        if parsed is None:
            continue
        dated.append((parsed, text, published))

    if not dated:
        return ClosureSignal(venue_id=venue_id, closed=False)

    dated.sort(key=lambda entry: entry[0], reverse=True)
    newest_time, newest_text, newest_raw = dated[0]

    phrase = _matched_phrase(_normalize(newest_text), phrases, negations)
    if phrase is None:
        # The most recent word on this venue is not a closure report.
        return ClosureSignal(venue_id=venue_id, closed=False)

    # A near-contemporaneous ordinary review contradicts the claim. The closure
    # report is still the newest, so it stands — but only at low confidence.
    confidence = "high"
    for published, text, _raw in dated[1:]:
        age_days = (newest_time - published).total_seconds() / 86400.0
        if age_days <= contradiction_window_days and _matched_phrase(
            _normalize(text), phrases, negations
        ) is None:
            confidence = "low"
            break

    return ClosureSignal(
        venue_id=venue_id,
        closed=True,
        reason=REASON_REVIEW_REPORTS_CLOSED,
        confidence=confidence,
        evidence_publish_time=str(newest_raw),
        matched_phrase=phrase,
    )


class ClosureDetectionService:
    """Scans stored reviews and records a closure signal per venue.

    Reads reviews in bulk (one query for the whole servable set, the same
    pattern `RedisProjectionService` uses) and writes signals through the store.
    Idempotent: re-running over unchanged reviews produces the same signals.
    """

    def __init__(
        self,
        rds_store,
        admin_config_service=None,
        enabled: Optional[Callable[[], bool]] = None,
    ):
        self.rds_store = rds_store
        self.admin_config_service = admin_config_service
        self._enabled = enabled

    # ── config ────────────────────────────────────────────────────────────────
    def _config(self) -> dict:
        """Admin overrides for phrases/window, falling back to the defaults.

        A malformed or unreadable override must never break the run — closure
        detection degrades to its hardcoded defaults, matching how
        `load_eligibility_config` treats a bad write.
        """
        defaults = {
            "enabled": False,
            "phrases": DEFAULT_CLOSURE_PHRASES,
            "negations": DEFAULT_NEGATION_PHRASES,
            "contradiction_window_days": DEFAULT_CONTRADICTION_WINDOW_DAYS,
        }
        if self.admin_config_service is None:
            return defaults
        try:
            override = self.admin_config_service.get("closure_detection")
        except Exception:
            logger.warning("[Closure] config read failed; using defaults")
            return defaults
        if not isinstance(override, dict):
            return defaults
        merged = dict(defaults)
        for key in defaults:
            if key in override and override[key] is not None:
                merged[key] = override[key]
        return merged

    def is_enabled(self) -> bool:
        """Detection is opt-in: an explicit callable wins, else admin config."""
        if self._enabled is not None:
            return bool(self._enabled())
        return bool(self._config().get("enabled", False))

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        """Evaluate every active venue and persist the signals.

        Returns a summary; never raises. A per-venue failure is isolated and
        named in `error_venues` so an operator can find the poisoned row.
        """
        summary = {"evaluated": 0, "flagged": 0, "cleared": 0, "errors": 0,
                   "error_venues": []}
        if not self.is_enabled():
            summary["skipped"] = "disabled"
            _count(CLOSURE_DETECTION_RUNS_TOTAL, outcome="disabled")
            return summary

        started = time.perf_counter()
        config = self._config()
        try:
            venue_ids = list(self.rds_store.list_active_venue_ids())
        except Exception as exc:
            logger.error("[Closure] active-venue read failed; aborting cycle: %s", exc)
            summary["errors"] += 1
            self._finish(summary, started, "aborted")
            return summary

        try:
            reviews_by_venue = self.rds_store.get_enrichment_bulk(
                "google_places.reviews", venue_ids
            )
        except Exception as exc:
            logger.error("[Closure] bulk review read failed; aborting cycle: %s", exc)
            summary["errors"] += 1
            self._finish(summary, started, "aborted")
            return summary

        for venue_id in venue_ids:
            try:
                record = reviews_by_venue.get(venue_id) or {}
                payload = record.get("payload", record) or {}
                signal = evaluate_reviews(
                    venue_id,
                    payload.get("reviews", []),
                    phrases=config.get("phrases"),
                    negations=config.get("negations"),
                    contradiction_window_days=config.get(
                        "contradiction_window_days", DEFAULT_CONTRADICTION_WINDOW_DAYS
                    ),
                )
                summary["evaluated"] += 1
                previous = self.rds_store.get_closure_signal(venue_id)
                was_closed = bool(previous and previous.get("closed"))
                if signal.closed:
                    if not was_closed:
                        summary["flagged"] += 1
                        logger.info(
                            "[Closure] venue=%s flagged closed confidence=%s "
                            "phrase=%r evidence=%s",
                            venue_id, signal.confidence, signal.matched_phrase,
                            signal.evidence_publish_time,
                        )
                    self.rds_store.set_closure_signal(venue_id, signal.to_dict())
                else:
                    if was_closed:
                        summary["cleared"] += 1
                        logger.info("[Closure] venue=%s cleared (newer evidence)", venue_id)
                    self.rds_store.clear_closure_signal(venue_id)
            except Exception as exc:
                # Isolation boundary: a poisoned payload degrades to "this venue
                # waits for the next cycle" and never aborts the run.
                summary["errors"] += 1
                summary["error_venues"].append(venue_id)
                _count(CLOSURE_DETECTION_ERRORS_TOTAL)
                logger.warning(
                    "[Closure] venue=%s evaluation failed: %s", venue_id, exc
                )

        self._finish(summary, started, "ok" if not summary["errors"] else "partial")
        return summary

    def _finish(self, summary: dict, started: float, outcome: str) -> None:
        """Record run duration, outcome, and the current flagged population."""
        _observe_duration(CLOSURE_DETECTION_DURATION_SECONDS,
                          time.perf_counter() - started)
        _count(CLOSURE_DETECTION_RUNS_TOTAL, outcome=outcome)
        counts = {"high": 0, "low": 0}
        try:
            for row in self.rds_store.list_closure_signals():
                if row.get("closed"):
                    key = row.get("confidence", "high")
                    counts[key] = counts.get(key, 0) + 1
        except Exception:  # pragma: no cover - defensive
            return
        for confidence, count in counts.items():
            _set_gauge(VENUES_CLOSED_FLAGGED, count, confidence=confidence)

    def list_signals(self) -> dict:
        """Current signals keyed by venue id, as `ClosureSignal` objects."""
        try:
            rows = self.rds_store.list_closure_signals()
        except Exception:
            logger.warning("[Closure] signal listing failed")
            return {}
        out = {}
        for row in rows:
            out[row["venue_id"]] = ClosureSignal(
                venue_id=row["venue_id"],
                closed=bool(row.get("closed")),
                reason=row.get("reason"),
                confidence=row.get("confidence", "high"),
                evidence_publish_time=row.get("evidence_publish_time"),
                matched_phrase=row.get("matched_phrase"),
            )
        return out
