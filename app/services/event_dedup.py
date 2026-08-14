"""Distinctive-token title containment and shared-lineup: the two signals
that decide whether two ALREADY-PERSISTED events at one venue describe one
real night. See plans/260812_event-dedup-fuzzy-title.md §A/§B/§B2/§D.

This module is a MERGE LAYER, never an identity change: nothing here touches
`app.services.event_identity.compute_source_event_key` or
`app.services.event_merge.compute_event_identity`, and nothing here is
imported by either — see `tests/test_event_dedup.py`'s own
`compute_source_event_key`/`compute_event_identity`-unchanged guard, the
cheapest possible regression test against §A being quietly undone.

Every function here is PURE: given event dicts and a config, it answers
"could/should these merge", but never touches a DAO. `app.services.
event_merge` is the one caller that turns an "auto" answer into an actual
absorption (choosing a canonical, folding fields, reattaching sources) and
`scripts/measure_event_dedup.py` is the one caller that turns every answer
into a report — both import the SAME `evaluate_pair`, so the measurement and
the runtime/sweep merge can never disagree about a single pair (plan §C2's
own requirement).

## Why not `app.services.instagram_cascade_service.name_similarity`

That function answers "are these two strings naming the same VENUE" — it
strips venue-type words, short-circuits containment to a flat 0.95, and is
tuned so a specific handful of venue-name pairs score above a chosen
threshold. This module answers a different question ("do these two EVENT
titles describe the same NIGHT") on evidence that broke a draft of THIS
design: on this corpus a plain string ratio ranks `'Ação Leitura: ... com
Marcelino Freire'` against `'... Jeferson Tenório'` ABOVE `'Rodolpho'`
against `'Rodolpho Produções'` — reusing `name_similarity` here would buy
consistency between two questions that were never the same question. See
the plan's §B for the full argument.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.metrics import EVENT_DEDUP_CONFIG_TYPE_FALLBACK_TOTAL
from app.services.event_date_resolver import RECIFE_TZ
from app.services.event_identity import normalize_title

logger = logging.getLogger(__name__)

BAND_AUTO = "auto"
BAND_SUGGEST = "suggest"
# BAND_REFUSE is never persisted or returned as a value — a refused pair is
# `None` (plan §C: "nothing is recorded and nothing is shown"). The name is
# kept as a constant purely for readability at call sites that reason about
# the three bands together (e.g. tests, the measurement script's counters).
BAND_REFUSE = "refuse"

REASON_TITLE = "title_containment"
REASON_LINEUP = "shared_lineup"

# ── admin config: generic-event vocabulary (plan §B, "runtime-configurable,
# matching menu_expiry_days, the post-category vocabulary and the busyness
# labels") ────────────────────────────────────────────────────────────────
ADMIN_CONFIG_GENERIC_VOCABULARY_KEY = "admin_config:event_dedup_generic_vocabulary"
# Seeded from the plan's own §B evidence — a starting point, not a ceiling.
# Already casefold/accent-normalised (matching what `normalize_title` will
# produce from any raw spelling) so a straight set-membership check is
# correct without re-normalising this list at compare time.
DEFAULT_GENERIC_VOCABULARY: tuple[str, ...] = (
    "festa", "noite", "show", "baile", "sextou", "domingou", "aniversario",
    "oficina", "especial", "edicao", "semana", "anos", "open", "bar",
)

ADMIN_CONFIG_STOPWORDS_KEY = "admin_config:event_dedup_stopwords"
# Portuguese function words (plan §B: "de, do, da, no, na, com, e, o, a, …").
DEFAULT_STOPWORDS: tuple[str, ...] = (
    "de", "do", "da", "dos", "das", "no", "na", "nos", "nas", "com", "e",
    "o", "a", "os", "as", "em", "um", "uma", "uns", "umas", "ao", "aos",
    "para", "por", "que",
)

ADMIN_CONFIG_LINEUP_THRESHOLD_KEY = "admin_config:event_dedup_lineup_threshold"
DEFAULT_LINEUP_THRESHOLD = 2

ADMIN_CONFIG_CANDIDATE_WINDOW_HOURS_KEY = "admin_config:event_dedup_candidate_window_hours"
DEFAULT_CANDIDATE_WINDOW_HOURS = 8

ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY = "admin_config:event_dedup_undated_window_days"
DEFAULT_UNDATED_WINDOW_DAYS = 14

ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY = "admin_config:event_dedup_auto_merge_enabled"
# Plan §C, "the six things most likely to go wrong" #1: OFF by default.
# Deploying this feature must change no data — flipping this is a
# deliberate, separately-reviewed operator act, never a side effect of
# whenever this branch happens to deploy.
DEFAULT_AUTO_MERGE_ENABLED = False


def _load_json_config(redis_like, key: str, default, *, module_tag: str):
    """Shared read for every scalar/list config in this module — the SAME
    Redis-mirror-with-fallback shape `app.models.menu_lifecycle.
    load_menu_expiry_days` and siblings already established. Returns
    `(value_or_default, fallback_reason)`; `fallback_reason` is None unless
    the caller should count a fallback metric."""
    if redis_like is None:
        return default, None
    try:
        raw = redis_like.get(key)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[{module_tag}] config read failed for {key}, using default: {e}")
        return default, "unreadable"
    if raw is None:
        return default, None
    try:
        return json.loads(raw), None
    except (TypeError, ValueError) as e:
        logger.warning(f"[{module_tag}] config invalid JSON for {key}, using default: {e}")
        return default, "invalid_json"


def validate_generic_vocabulary_config(value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise TypeError("event dedup generic vocabulary must be a list of non-empty strings")
    return [normalize_title(v) for v in value]


def validate_stopwords_config(value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise TypeError("event dedup stopwords must be a list of non-empty strings")
    return [normalize_title(v) for v in value]


def validate_lineup_threshold_config(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("event dedup lineup threshold must be an integer")
    if value < 1:
        raise ValueError("event dedup lineup threshold must be at least 1")
    return value


def validate_candidate_window_hours_config(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("event dedup candidate window must be an integer number of hours")
    if value < 0:
        raise ValueError("event dedup candidate window must not be negative")
    return value


def validate_undated_window_days_config(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("event dedup undated window must be an integer number of days")
    if value < 0:
        raise ValueError("event dedup undated window must not be negative")
    return value


def validate_auto_merge_enabled_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("event dedup auto-merge flag must be a boolean")
    return value


@dataclass(frozen=True)
class DedupConfig:
    generic_vocabulary: tuple[str, ...]
    stopwords: tuple[str, ...]
    lineup_threshold: int
    candidate_window_hours: int
    undated_window_days: int
    auto_merge_enabled: bool


def _load_validated_config(redis_like, key: str, default, *, validator, module_tag: str):
    """Read one key, then TRUST the validated value instead of coercing it
    (plan §D — `bool("false")` was the coercion this replaces: storing the
    string `"false"` through the generic CRUD route used to yield
    `bool("false")` -> True, silently ENABLING auto-merge).

    `_load_json_config` (unchanged) already falls back to `default` when
    Redis is unreadable, the key is absent, or the mirror JSON is corrupt —
    those cases return `default` BY IDENTITY (the same object this call
    passed in), so they are recognised here with `is` rather than re-run
    through `validator`, which may legitimately require a different runtime
    shape than the shipped default happens to be stored in (e.g. a
    list-only validator against a `tuple` default).

    A value that WAS read (valid JSON, present) but fails its own
    registered validator — the wrong type, e.g. a stored string where a
    bool belongs — is the specific case this function exists for: it is
    never coerced into a plausible-looking answer. It falls back to
    `default` and counts `EVENT_DEDUP_CONFIG_TYPE_FALLBACK_TOTAL` by key, so
    a bad stored value is visible on a dashboard instead of silently
    changing behaviour.
    """
    raw, reason = _load_json_config(redis_like, key, default, module_tag=module_tag)
    if reason is not None or raw is default:
        return default
    try:
        return validator(raw)
    except (TypeError, ValueError) as e:
        logger.warning(
            f"[{module_tag}] stored value for {key} failed its validator on "
            f"read ({e}); using the shipped default instead of coercing it"
        )
        EVENT_DEDUP_CONFIG_TYPE_FALLBACK_TOTAL.labels(key=key).inc()
        return default


def load_dedup_config(redis_like) -> DedupConfig:
    """Read every event-dedup admin-config override, falling back to the
    shipped defaults independently per key — one key's bad value never
    disables another's override. `redis_like=None` (a caller with no wired
    Redis client, e.g. a bare unit test) returns every shipped default,
    matching every sibling loader in this codebase.

    Every value is TRUSTED as whatever its own validator returns (§D) —
    never re-derived with `bool()`/`int()`/`tuple()` on a value whose type
    was never actually confirmed. `generic_vocabulary`/`stopwords` still
    become tuples at the end for `DedupConfig`'s declared shape — that is
    adapting an ALREADY-validated list to the dataclass's field type, not
    coercing an unknown one."""
    generic = _load_validated_config(
        redis_like, ADMIN_CONFIG_GENERIC_VOCABULARY_KEY, list(DEFAULT_GENERIC_VOCABULARY),
        validator=validate_generic_vocabulary_config, module_tag="event_dedup",
    )
    stopwords = _load_validated_config(
        redis_like, ADMIN_CONFIG_STOPWORDS_KEY, list(DEFAULT_STOPWORDS),
        validator=validate_stopwords_config, module_tag="event_dedup",
    )
    threshold = _load_validated_config(
        redis_like, ADMIN_CONFIG_LINEUP_THRESHOLD_KEY, DEFAULT_LINEUP_THRESHOLD,
        validator=validate_lineup_threshold_config, module_tag="event_dedup",
    )
    window_hours = _load_validated_config(
        redis_like, ADMIN_CONFIG_CANDIDATE_WINDOW_HOURS_KEY, DEFAULT_CANDIDATE_WINDOW_HOURS,
        validator=validate_candidate_window_hours_config, module_tag="event_dedup",
    )
    undated_days = _load_validated_config(
        redis_like, ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY, DEFAULT_UNDATED_WINDOW_DAYS,
        validator=validate_undated_window_days_config, module_tag="event_dedup",
    )
    auto_enabled = _load_validated_config(
        redis_like, ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, DEFAULT_AUTO_MERGE_ENABLED,
        validator=validate_auto_merge_enabled_config, module_tag="event_dedup",
    )
    return DedupConfig(
        generic_vocabulary=tuple(generic), stopwords=tuple(stopwords),
        lineup_threshold=threshold, candidate_window_hours=window_hours,
        undated_window_days=undated_days, auto_merge_enabled=auto_enabled,
    )


# ── §B: distinctive-token containment ───────────────────────────────────────
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+")


def _tokenize(normalized_text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT_RE.split(normalized_text) if t]


def venue_name_tokens(venue_name: Optional[str]) -> frozenset:
    """Every token of the venue's own name, normalised the SAME way a title
    is (`normalize_title`, never a second normalisation) — dropped from a
    title's distinctive set so a venue's own name mentioned in its own post
    is never mistaken for distinctive content (plan §B's `'SEXTOU NO
    CONCHITTAS BAR!'` example)."""
    if not venue_name:
        return frozenset()
    return frozenset(_tokenize(normalize_title(venue_name)))


def distinctive_set(
    title: Optional[str], *, venue_tokens: frozenset, generic_vocabulary, stopwords,
) -> frozenset:
    """A title's distinctive set (plan §B): `normalize_title`, split on any
    run of non-alphanumeric characters, then drop — in any order, since
    these are independent per-token exclusions over static sets, not a
    pipeline where order changes the outcome — Portuguese function words,
    the generic-event vocabulary, every token of the venue's own name, and
    any token shorter than two characters UNLESS it is numeric.

    Two rules pinned by measurement (plan §B, "three rules that look like
    details and are not"): bare numerals are NEVER stripped (`'31'` in
    `'31 Anos'` stays, and `'1'..'4'` in a `Semana N` programme stay
    mutually distinct); the minimum token length is TWO, not three (a
    three-char floor collapses `'JB do Cavaco'`'s `{cavaco}` into
    `'Bolinha do Cavaco'`'s `{bolinha, cavaco}` as a false subset — do not
    reintroduce either)."""
    generic = {normalize_title(w) for w in generic_vocabulary}
    stop = {normalize_title(w) for w in stopwords}
    tokens = _tokenize(normalize_title(title))
    out = set()
    for tok in tokens:
        if tok in stop or tok in generic or tok in venue_tokens:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        out.add(tok)
    return frozenset(out)


def band_for_distinctive_sets(a: frozenset, b: frozenset) -> str:
    """Plan §B's set predicate: both non-empty and one a subset of the
    other (equal counts as a subset of itself) -> auto; both non-empty and
    intersecting without containment -> suggest; either empty, or disjoint
    -> refuse. Symmetric in `a`/`b` by construction — the band for `(a, b)`
    is always the band for `(b, a)`."""
    if not a or not b:
        return BAND_REFUSE
    if a <= b or b <= a:
        return BAND_AUTO
    if a & b:
        return BAND_SUGGEST
    return BAND_REFUSE


# ── §B2: shared lineup ───────────────────────────────────────────────────────
def lineup_name_set(lineup) -> frozenset:
    """Every performer name in `lineup`, normalised with the SAME
    casefold/accent-strip/whitespace treatment a title token gets
    (`normalize_title`, never a second normalisation) — `'DAYANNE'` and
    `'Dayanne'` collapse to one name; `'Dayanne'` and `'Dayanne Henrique'`
    stay two, because they are two (plan §B2's own boundary test)."""
    return frozenset(
        normalize_title(name) for name in (lineup or []) if normalize_title(name)
    )


def shared_lineup_names(lineup_a, lineup_b) -> frozenset:
    return lineup_name_set(lineup_a) & lineup_name_set(lineup_b)


def lineup_reaches_auto(lineup_a, lineup_b, *, threshold: int) -> bool:
    """Plan §B2: two rows sharing at least `threshold` (default 2, the
    measured floor) normalised lineup names auto-merge — an INDEPENDENT
    sufficient condition, never a tie-break on the title rule. A pair where
    EITHER side's lineup is empty never reaches auto on lineup alone (no
    lineup is no evidence, not weak evidence)."""
    if not lineup_a or not lineup_b:
        return False
    return len(shared_lineup_names(lineup_a, lineup_b)) >= threshold


# ── §D: the candidate window ────────────────────────────────────────────────
def _recife_date(dt: Optional[datetime]):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(RECIFE_TZ).date()


def in_candidate_window(
    a_starts_at: Optional[datetime], b_starts_at: Optional[datetime], *, window_hours: int,
) -> bool:
    """Same Recife local date, OR within `window_hours` of each other (plan
    §D) — the disjunction does real work: the Conchittas pair is 5 hours
    apart across a local-date boundary (caught by the window, missed by the
    date); the Sala de Reboco `Homenagem` pair is 21 hours apart on ONE
    local date (caught by the date, missed by the window). Either side
    missing its `starts_at` never matches — no date is no evidence of the
    same night, exactly like an empty distinctive set is no evidence of the
    same title."""
    if a_starts_at is None or b_starts_at is None:
        return False
    if _recife_date(a_starts_at) == _recife_date(b_starts_at):
        return True
    delta_hours = abs((a_starts_at - b_starts_at).total_seconds()) / 3600.0
    return delta_hours <= window_hours


# ── the combined pairwise verdict ───────────────────────────────────────────
@dataclass(frozen=True)
class PairDecision:
    band: str  # BAND_AUTO | BAND_SUGGEST — a BAND_REFUSE pair is `None`, never this
    reasons: tuple  # any of (REASON_TITLE,), (REASON_LINEUP,), or both
    event_distinctive_words: tuple
    candidate_distinctive_words: tuple
    shared_lineup_names: tuple


def evaluate_pair(event_a: dict, event_b: dict, *, venue_name, config: DedupConfig) -> Optional[PairDecision]:
    """The pairwise verdict for two ALREADY-CANDIDATE-WINDOWED events at one
    venue (the caller applies `in_candidate_window` and the same-`venue_id`
    restriction BEFORE calling this — this function does not re-check
    either). `None` means refuse: nothing recorded, nothing shown (plan
    §C). Never inspects `status`/`operator_edited_fields`/protection —
    those gate ABSORPTION, a decision only the caller (which knows which
    side would become the duplicate) can make; see
    `app.services.event_merge`'s guard functions."""
    venue_tokens = venue_name_tokens(venue_name)
    set_a = distinctive_set(
        event_a.get("title"), venue_tokens=venue_tokens,
        generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
    )
    set_b = distinctive_set(
        event_b.get("title"), venue_tokens=venue_tokens,
        generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
    )
    title_band = band_for_distinctive_sets(set_a, set_b)
    lineup_auto = lineup_reaches_auto(
        event_a.get("lineup"), event_b.get("lineup"), threshold=config.lineup_threshold,
    )
    shared_names = shared_lineup_names(event_a.get("lineup"), event_b.get("lineup"))

    reasons: list = []
    if lineup_auto:
        reasons.append(REASON_LINEUP)
    if title_band == BAND_AUTO:
        reasons.append(REASON_TITLE)

    if reasons:
        band = BAND_AUTO
    elif title_band == BAND_SUGGEST:
        band = BAND_SUGGEST
        reasons = [REASON_TITLE]
    else:
        return None

    return PairDecision(
        band=band, reasons=tuple(reasons),
        event_distinctive_words=tuple(sorted(set_a)),
        candidate_distinctive_words=tuple(sorted(set_b)),
        shared_lineup_names=tuple(sorted(shared_names)),
    )


__all__ = [
    "BAND_AUTO", "BAND_SUGGEST", "BAND_REFUSE", "REASON_TITLE", "REASON_LINEUP",
    "ADMIN_CONFIG_GENERIC_VOCABULARY_KEY", "DEFAULT_GENERIC_VOCABULARY",
    "ADMIN_CONFIG_STOPWORDS_KEY", "DEFAULT_STOPWORDS",
    "ADMIN_CONFIG_LINEUP_THRESHOLD_KEY", "DEFAULT_LINEUP_THRESHOLD",
    "ADMIN_CONFIG_CANDIDATE_WINDOW_HOURS_KEY", "DEFAULT_CANDIDATE_WINDOW_HOURS",
    "ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY", "DEFAULT_UNDATED_WINDOW_DAYS",
    "ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY", "DEFAULT_AUTO_MERGE_ENABLED",
    "validate_generic_vocabulary_config", "validate_stopwords_config",
    "validate_lineup_threshold_config", "validate_candidate_window_hours_config",
    "validate_undated_window_days_config", "validate_auto_merge_enabled_config",
    "DedupConfig", "load_dedup_config",
    "venue_name_tokens", "distinctive_set", "band_for_distinctive_sets",
    "lineup_name_set", "shared_lineup_names", "lineup_reaches_auto",
    "in_candidate_window", "PairDecision", "evaluate_pair",
]
