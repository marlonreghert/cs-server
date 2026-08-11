"""Post-item category vocabulary.

See plans/260811_post-items-and-categories.md §C. A post-item's `category`
is free text from the extraction model, steered (not confined) toward a
known vocabulary — the operator's own example is that a model left
unsteered will write "rock", "rock-n-roll" and "Rock And Roll" for one
genre, and this repo has already been bitten by the inverse
(plans/260806_filter-label-casing.md: exact-matching an LLM-produced value
against a configured label zeroes the result when casing differs).

The vocabulary is ADMIN CONFIG, not a code constant — venue types
(app.models.venue_category) and busyness labels are already runtime-
configurable in this project for the same reason: the seeded list will be
wrong on contact with real data, and growing it must not need a deploy.
Mirrors the admin-config Redis-mirror-with-fallback shape
app.services.event_venue_targeting already established for
`admin_config:event_candidate_categories`.

Canonicalisation is case-insensitive via `casefold()`, never `.lower()` —
this is pt-BR text, and casefold is the correct Unicode-aware fold for it.
An off-vocabulary answer is NEVER rejected: the point of free text is to
let the vocabulary grow from evidence, so a miss is counted
(`record_off_vocabulary_category`), not refused.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY = "admin_config:post_category_vocabulary"

# Seeded from what Recife actually has (the plan's own Evidence) — a
# starting point, not a ceiling. Order is display order; matching is
# case-insensitive and order-independent.
DEFAULT_CATEGORY_VOCABULARY: tuple[str, ...] = (
    "live music", "DJ / club night", "samba / pagode", "forró", "rock",
    "MPB", "jazz", "sertanejo", "funk", "karaoke", "comedy", "quiz / trivia",
    "kids / family", "workshop", "food festival", "tasting",
    "sports screening", "party",
)

# Cardinality cap for the off-vocabulary counter's `category` label — an
# unbounded Prometheus label fed directly by model output is a cardinality
# bomb (CLAUDE.md; this repo's own reliability guardrails). The first
# `_MAX_TRACKED_OFF_VOCABULARY_LABELS` DISTINCT raw values seen by this
# process get their own label; everything after that buckets under
# `OFF_VOCABULARY_OVERFLOW_LABEL`. In-memory only (a process restart resets
# the set) — this bounds the METRIC's cardinality, not a durable audit log;
# the raw text itself is already persisted verbatim on the post_item's own
# `category` column for real investigation, so nothing is lost by the cap.
_MAX_TRACKED_OFF_VOCABULARY_LABELS = 50
OFF_VOCABULARY_OVERFLOW_LABEL = "_overflow"
_seen_off_vocabulary_labels: set[str] = set()


def _normalize_text(raw) -> Optional[str]:
    """Trim + collapse internal whitespace. None/blank -> None — absent is
    absent, never a value to canonicalize or count."""
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    return text or None


def validate_post_category_vocabulary_config(value) -> list[str]:
    """Validate an admin write to `admin_config:post_category_vocabulary`
    before persistence (AdminConfigService.set dispatches here). Body shape:
    a JSON array of non-empty strings. Raises TypeError/ValueError on a
    malformed shape; returns the normalized list actually persisted
    (whitespace-normalized, de-duplicated case-insensitively via casefold —
    first spelling wins, exactly what `canonicalize_category` will store on
    a match)."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TypeError("post category vocabulary must be a list of strings")
    seen: set[str] = set()
    result: list[str] = []
    for raw in value:
        text = _normalize_text(raw)
        if not text:
            raise ValueError("post category vocabulary entries must not be blank")
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    if not result:
        raise ValueError("post category vocabulary must not be empty")
    return result


def load_post_category_vocabulary(redis_like) -> tuple[list[str], Optional[str]]:
    """Read the admin override, falling back to the shipped defaults on any
    problem. Returns `(vocabulary, fallback_reason)`; `fallback_reason` is
    None unless the caller should count a fallback metric — a missing key is
    NOT a fallback, it is the expected pre-first-write state."""
    if redis_like is None:
        return list(DEFAULT_CATEGORY_VOCABULARY), None
    try:
        raw = redis_like.get(ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[post_category] config read failed, using defaults: {e}")
        return list(DEFAULT_CATEGORY_VOCABULARY), "unreadable"
    if raw is None:
        return list(DEFAULT_CATEGORY_VOCABULARY), None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning(f"[post_category] config invalid JSON, using defaults: {e}")
        return list(DEFAULT_CATEGORY_VOCABULARY), "invalid_json"
    try:
        return validate_post_category_vocabulary_config(data), None
    except (TypeError, ValueError) as e:
        logger.warning(f"[post_category] config invalid shape, using defaults: {e}")
        return list(DEFAULT_CATEGORY_VOCABULARY), "invalid_shape"


def canonicalize_category(raw, vocabulary) -> Optional[str]:
    """Normalise a model-returned category against `vocabulary`: trim +
    collapse internal whitespace, then match case-insensitively via
    `casefold()` — never `.lower()`, which is not the correct Unicode fold
    for pt-BR text. A match stores the VOCABULARY's own spelling (so "rock",
    "Rock" and "ROCK" converge on one stored value); a genuine miss stores
    the model's own (whitespace-normalized) text, UNCHANGED otherwise —
    never rejected. Returns None for a missing/blank input.
    """
    text = _normalize_text(raw)
    if text is None:
        return None
    folded = text.casefold()
    for known in vocabulary:
        if known.casefold() == folded:
            return known
    return text


def is_in_vocabulary(category, vocabulary) -> bool:
    """Whether `category` matches a vocabulary entry case-insensitively.
    Used to decide whether a stored category should count as off-vocabulary
    (see `record_off_vocabulary_category`)."""
    text = _normalize_text(category)
    if text is None:
        return False
    folded = text.casefold()
    return any(known.casefold() == folded for known in vocabulary)


def record_off_vocabulary_category(raw_category: str) -> None:
    """Count one off-vocabulary category answer on
    `POST_CATEGORY_OFF_VOCABULARY_TOTAL`, capping the label cardinality this
    creates (see `_MAX_TRACKED_OFF_VOCABULARY_LABELS` above): the first N
    distinct raw values get their own label; every value after that buckets
    under `OFF_VOCABULARY_OVERFLOW_LABEL`. Imported lazily to avoid a
    module-import cycle with app.metrics."""
    from app.metrics import POST_CATEGORY_OFF_VOCABULARY_TOTAL

    if raw_category in _seen_off_vocabulary_labels:
        label = raw_category
    elif len(_seen_off_vocabulary_labels) < _MAX_TRACKED_OFF_VOCABULARY_LABELS:
        _seen_off_vocabulary_labels.add(raw_category)
        label = raw_category
    else:
        label = OFF_VOCABULARY_OVERFLOW_LABEL
    POST_CATEGORY_OFF_VOCABULARY_TOTAL.labels(category=label).inc()


__all__ = [
    "ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY", "DEFAULT_CATEGORY_VOCABULARY",
    "OFF_VOCABULARY_OVERFLOW_LABEL",
    "validate_post_category_vocabulary_config", "load_post_category_vocabulary",
    "canonicalize_category", "is_in_vocabulary", "record_off_vocabulary_category",
]
