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
from app.services.event_date_resolver import RECIFE_TZ, weekdays_from_recurrence_text
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
# plans/260912_events-venue-night-duplication.md §E2: a THIRD reason token,
# carried on the existing `PairDecision` so the per-venue policy flows
# through the existing audit row, the existing metric labels and the
# existing reversal path with no new machinery.
REASON_SINGLE_NIGHT_VENUE = "single_night_venue"

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

ADMIN_CONFIG_SINGLE_NIGHT_VENUES_KEY = "admin_config:event_dedup_single_night_venues"
# plans/260912_events-venue-night-duplication.md §E2. EMPTY by default, and
# never a corpus-wide rule: `260812`'s own measured false-positive corpus
# contains `Bolinha do Cavaco` / `JB do Cavaco` at Casanova Ecobar — two
# different acts, one night, same venue, deliberately kept apart — which is
# the SAME SHAPE as Club Metrópole's five acts on one Saturday. Nothing in
# the rows tells the two situations apart; the difference is a fact about the
# VENUE (a club runs one night, a theatre runs a programme). A per-venue list
# is the only mechanism that respects both the operator's ask and the
# evidence that previously refused it, and it is fully deterministic and
# unit-testable.
DEFAULT_SINGLE_NIGHT_VENUES: tuple[str, ...] = ()

ADMIN_CONFIG_SINGLE_NIGHT_DEFAULT_ENABLED_KEY = (
    "admin_config:event_dedup_single_night_default_enabled"
)
# The CATALOG-WIDE form of §E2, added after the operator reviewed the
# per-venue list and chose the broader scope EXPLICITLY, with the tradeoff
# stated: every venue is treated as running one night, with NO exclusions.
#
# What that accepts, in the operator's own words: the `Bolinha do Cavaco` /
# `JB do Cavaco` shape (`260812`'s measured false-positive corpus — two
# different acts, one night, one venue, deliberately kept apart by a prior
# review) WILL now re-merge wherever it recurs, and any undiscovered
# "programme" venue running genuinely separate same-night events will have
# one silently absorbed into another until an operator notices it in
# `GET /admin/events/dedup-backlog` and dials the scope back.
#
# OFF by default, like every other flag in this plan: the deploy still
# provably changes no stored row, and turning this on is a separate,
# deliberate operator act.
#
# **Dialling back is not an exclusion list.** There is deliberately no
# "except these venues" key: the remedy is to set this flag FALSE and name
# the venues you DO want in `event_dedup_single_night_venues`. That is
# awkward once the catalog-wide behaviour has been on for a while (it means
# enumerating the whole catalog minus the exception), and an exclusion list
# is the obvious follow-up if this is ever dialled back in anger — recorded
# here so the next reader does not think the gap was missed.
DEFAULT_SINGLE_NIGHT_DEFAULT_ENABLED = False

ADMIN_CONFIG_RECURRING_WINDOW_ENABLED_KEY = "admin_config:event_dedup_recurring_window_enabled"
# plans/260912_events-venue-night-duplication.md §D, Defect 3. OFF by
# default: it strictly WIDENS the candidate set, and auto-merge is already
# `true` in production, so a deploy that widened it would begin merging on
# the very next crawl, unattended. Turning it on is an operator's deliberate
# act AFTER §F's attribution repair (a widened window over a mis-attributed
# corpus is how a Casa Forte weekly night gets absorbed into a Boa Viagem
# one).
DEFAULT_RECURRING_WINDOW_ENABLED = False

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


def validate_recurring_window_enabled_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("event dedup recurring-window flag must be a boolean")
    return value


def validate_single_night_default_enabled_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("event dedup single-night default flag must be a boolean")
    return value


def validate_single_night_venues_config(value) -> list[str]:
    """A list of venue_ids — NEVER normalised, lowercased or otherwise
    massaged: a venue_id is an opaque key, and "helpfully" transforming it
    here would silently stop matching the ids the merge pass compares
    against."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise TypeError("event dedup single-night venues must be a list of non-empty venue ids")
    return [v.strip() for v in value]


@dataclass(frozen=True)
class DedupConfig:
    generic_vocabulary: tuple[str, ...]
    stopwords: tuple[str, ...]
    lineup_threshold: int
    candidate_window_hours: int
    undated_window_days: int
    auto_merge_enabled: bool
    # plans/260912_events-venue-night-duplication.md §D/§E. Both DEFAULT to
    # today's behaviour and are declared with defaults deliberately: every
    # existing construction of this dataclass (tests, the measurement
    # script, `load_dedup_config` before §D/§E wire their loaders) keeps
    # working unchanged, and a deploy that sets neither key widens nothing.
    #
    # §D: whether two RECURRING rows whose weekday patterns intersect are
    # candidates for the same night whatever their stored dates say. See
    # `in_candidate_window_for_rows`.
    recurring_window_enabled: bool = False
    # §E2: venue_ids an operator has declared run ONE night rather than a
    # programme. Empty by default — see `evaluate_pair`'s
    # `single_night_venue` argument, and `is_single_night_venue` below for
    # how this combines with the catalog-wide flag.
    single_night_venues: tuple[str, ...] = ()
    # §E2, catalog-wide: treat EVERY venue as running one night. Strictly
    # broader than the list above, and OFF by default.
    single_night_default_enabled: bool = False

    def is_single_night_venue(self, venue_id: Optional[str]) -> bool:
        """Whether §E2's single-night bypass applies to `venue_id`.

        The two keys are ORed, and the catalog-wide flag is strictly the
        broader of the two — so when both are somehow set, the flag decides
        and the list is simply redundant, never restrictive. Stated
        explicitly because the opposite reading ("the list narrows the
        default") is the intuitive one and is WRONG: there is no exclusion
        semantics here at all, and a venue cannot be taken off the
        catalog-wide behaviour by omitting it from the list. Dialling the
        scope back means setting the flag false and naming the venues you DO
        want.

        No catalog lookup, no membership check, no I/O: with the flag on this
        is a constant `True`, so turning it on cannot change which rows are
        even CONSIDERED — only which of the already-windowed same-venue pairs
        reach the auto band.
        """
        if self.single_night_default_enabled:
            return True
        return bool(venue_id) and venue_id in (self.single_night_venues or ())


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
    recurring_window = _load_validated_config(
        redis_like, ADMIN_CONFIG_RECURRING_WINDOW_ENABLED_KEY, DEFAULT_RECURRING_WINDOW_ENABLED,
        validator=validate_recurring_window_enabled_config, module_tag="event_dedup",
    )
    single_night = _load_validated_config(
        redis_like, ADMIN_CONFIG_SINGLE_NIGHT_VENUES_KEY, list(DEFAULT_SINGLE_NIGHT_VENUES),
        validator=validate_single_night_venues_config, module_tag="event_dedup",
    )
    single_night_default = _load_validated_config(
        redis_like, ADMIN_CONFIG_SINGLE_NIGHT_DEFAULT_ENABLED_KEY,
        DEFAULT_SINGLE_NIGHT_DEFAULT_ENABLED,
        validator=validate_single_night_default_enabled_config, module_tag="event_dedup",
    )
    return DedupConfig(
        generic_vocabulary=tuple(generic), stopwords=tuple(stopwords),
        lineup_threshold=threshold, candidate_window_hours=window_hours,
        undated_window_days=undated_days, auto_merge_enabled=auto_enabled,
        recurring_window_enabled=recurring_window,
        single_night_venues=tuple(single_night),
        single_night_default_enabled=single_night_default,
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


def _recurring_weekdays(row: dict) -> frozenset:
    """The nights this row actually SERVES, from
    `event_date_resolver.weekdays_from_recurrence_text` — the SAME function
    `event_occurrences.expand_occurrences` uses, IMPORTED rather than
    re-parsed. If this window and the expansion could ever disagree about
    which nights a row serves, the window would stop being evidence (the
    identical argument this module's own docstring makes about
    `evaluate_pair` having one caller for measurement and one for merging).

    Empty for a non-recurring row and for recurrence prose this repo does
    not parse ("toda semana", "sempre") — an empty set intersects nothing,
    so both cases simply never widen the window, which is the conservative
    answer in each.
    """
    if not row.get("is_recurring"):
        return frozenset()
    return frozenset(weekdays_from_recurrence_text(row.get("recurrence_text")) or ())


def in_candidate_window_for_rows(
    a: dict, b: dict, *, window_hours: int, recurring_window_enabled: bool,
) -> bool:
    """plans/260912_events-venue-night-duplication.md §D (Defect 3), the
    candidate window over two ROWS rather than two instants.

    `in_candidate_window` above stays EXACTLY as it is — signature and
    behaviour — so its existing unit tests keep meaning what they mean; this
    is a sibling that calls it, never a replacement that redefines it.

    `expand_occurrences` re-derives a RECURRING row's served days from its
    weekday pattern and DISCARDS its stored date entirely
    (`event_occurrences.py`'s own docstring says so, and that is deliberate
    and load-bearing). Both merge gates, however, key on that same stored
    date — so two posts about the same weekly night stored three weeks apart
    are never compared, yet expand onto the same served dates. Confirmed on
    real data: two live rows, identical title `Aula de FORRÓ na Sala de
    Reboco`, identical `recurrence_text = 'Toda QUARTA'`, stored 21 days
    apart, expanding onto three shared future dates each.

    Three deliberate choices a future reader will want to undo:

    - **Both sides must be recurring.** A weekly row and a one-off row that
      collide on one expanded date are a real but UNMEASURED case, and
      pairing them risks a one-off being absorbed into a weekly identity.
      Out of scope on purpose (the plan's own Non-goals).
    - **INTERSECTION, not equality, of weekday sets.** `Toda QUARTA` and
      `Quartas e sextas` collide on every Wednesday they both serve;
      requiring equal sets would miss exactly the collision this exists to
      catch. It is also the faithful analogue of the existing rule, whose
      same-local-date branch already admits two rows on one date regardless
      of their clock times.
    - **No clock-time condition.** Same reason: the `window_hours` half
      exists to bridge a LOCAL-DATE BOUNDARY, not to separate a 19:00 show
      from a 23:00 party on one date. The distinctive-set predicate is what
      separates those.

    A merged pair of recurring rows cannot move a weekly night: because
    `expand_occurrences` ignores a recurring row's stored date entirely, the
    survivor's `starts_at` DATE has no effect on which nights it serves —
    only its clock time does, and `merge_event_fields` already carries
    `time_known` alongside whichever `starts_at` wins.
    """
    if in_candidate_window(
        a.get("starts_at"), b.get("starts_at"), window_hours=window_hours,
    ):
        return True
    if not recurring_window_enabled:
        return False
    return bool(_recurring_weekdays(a) & _recurring_weekdays(b))


# ── the combined pairwise verdict ───────────────────────────────────────────
@dataclass(frozen=True)
class PairDecision:
    band: str  # BAND_AUTO | BAND_SUGGEST — a BAND_REFUSE pair is `None`, never this
    reasons: tuple  # any of (REASON_TITLE,), (REASON_LINEUP,), or both
    event_distinctive_words: tuple
    candidate_distinctive_words: tuple
    shared_lineup_names: tuple


def evaluate_pair(
    event_a: dict, event_b: dict, *, venue_name, config: DedupConfig,
    single_night_venue: bool = False,
) -> Optional[PairDecision]:
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
    # §E2: at a venue an operator has said runs ONE NIGHT rather than a
    # programme, two live event rows in the same candidate window are the
    # same night regardless of what their titles or lineups say. An
    # INDEPENDENT sufficient condition, exactly like the shared-lineup rule
    # — never a tie-break on either of the other two, and never a
    # corpus-wide rule (the caller decides, per venue, from a list that is
    # empty by default). It changes only the BAND: every absorption guard
    # (`_is_protected`, `_edge_blocked_for_absorption`, `choose_canonical`
    # returning None for two protected members, the non-event guard) still
    # applies unchanged in `app.services.event_merge`, which is the only
    # thing that turns a band into a write.
    if single_night_venue:
        reasons.append(REASON_SINGLE_NIGHT_VENUE)

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
    "BAND_AUTO", "BAND_SUGGEST", "BAND_REFUSE", "REASON_TITLE", "REASON_LINEUP", "REASON_SINGLE_NIGHT_VENUE",
    "ADMIN_CONFIG_GENERIC_VOCABULARY_KEY", "DEFAULT_GENERIC_VOCABULARY",
    "ADMIN_CONFIG_STOPWORDS_KEY", "DEFAULT_STOPWORDS",
    "ADMIN_CONFIG_LINEUP_THRESHOLD_KEY", "DEFAULT_LINEUP_THRESHOLD",
    "ADMIN_CONFIG_CANDIDATE_WINDOW_HOURS_KEY", "DEFAULT_CANDIDATE_WINDOW_HOURS",
    "ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY", "DEFAULT_UNDATED_WINDOW_DAYS",
    "ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY", "DEFAULT_AUTO_MERGE_ENABLED",
    "ADMIN_CONFIG_RECURRING_WINDOW_ENABLED_KEY", "DEFAULT_RECURRING_WINDOW_ENABLED",
    "ADMIN_CONFIG_SINGLE_NIGHT_VENUES_KEY", "DEFAULT_SINGLE_NIGHT_VENUES",
    "ADMIN_CONFIG_SINGLE_NIGHT_DEFAULT_ENABLED_KEY", "DEFAULT_SINGLE_NIGHT_DEFAULT_ENABLED",
    "validate_generic_vocabulary_config", "validate_stopwords_config",
    "validate_lineup_threshold_config", "validate_candidate_window_hours_config",
    "validate_undated_window_days_config", "validate_auto_merge_enabled_config",
    "validate_recurring_window_enabled_config", "validate_single_night_venues_config",
    "validate_single_night_default_enabled_config",
    "DedupConfig", "load_dedup_config",
    "venue_name_tokens", "distinctive_set", "band_for_distinctive_sets",
    "lineup_name_set", "shared_lineup_names", "lineup_reaches_auto",
    "in_candidate_window", "in_candidate_window_for_rows", "PairDecision", "evaluate_pair",
]
