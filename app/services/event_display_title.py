"""Give a merged venue-night one display title the app can read.

See plans/260912_events-venue-night-duplication.md §G. When five posts about
five acts collapse into one listing, the surviving `title` is whichever
source `merge_event_fields` last saw — an arbitrary pick for the one string
a reader actually looks at.

## The model is ARCHITECTURALLY excluded from the merge decision

The merge path is SYNCHRONOUS (`merge_touched_events` ->
`run_title_similarity_pass` -> `_absorb_title_similarity`) and
`OpenAIEventExtractionClient` is ASYNC. A title call cannot be made inside
the merge without making the merge async, which would be a large refactor of
a path three migrations replay. That constraint is a gift: it forces the
model out of the decision path entirely. This module runs as a SEPARATE pass,
AFTER the merge, over the event ids a run touched.

## Why this does not contradict `260812_event-dedup-fuzzy-title.md` §C

§C rejected an LLM in the merge path on two stated grounds: "a suggestion
that changes between runs re-opens a decision the operator already closed",
and "a non-deterministic merge cannot be unit-tested against the
false-positive pairs above, which is the only evidence that the bar is safe."
Both are arguments about the MERGE DECISION. Neither applies here:

1. **The decision is untouched.** Whether two rows merge is still
   `evaluate_pair` + `choose_canonical` + the guards — pure, deterministic,
   still pinned by `tests/test_event_dedup.py`'s production-string band
   tests. The model is consulted strictly AFTER a merge has happened and can
   never cause, prevent or reverse one.
2. **Even the decision to CALL is deterministic.** `obvious_canonical_title`
   is a pure predicate over source titles, unit-tested on the production
   strings. Only the chosen STRING is non-deterministic.
3. **Nothing an operator closed is re-opened.** A model answer never becomes
   a suggestion, never re-enters the queue, and never overrides an
   operator-edited title (short-circuited before the call).
4. **The blast radius is one presentation field that tolerates variance.** A
   run-to-run difference changes the words on a card — not which rows exist,
   not what is served, not what is merged. No logic computes on
   `display_title`; it is read exactly once, by the projection, as
   `display_title or title`.

## The validation gate is what keeps a model answer harmless

A returned string is written ONLY if it is non-empty after
`normalize_title`, at most 120 characters, and its distinctive set is a
SUBSET OF THE UNION of the group's source distinctive sets — the model may
select, shorten or recombine what the posts said, and may never introduce a
proper noun no post contained. On ANY failure (validation, API error,
timeout, malformed JSON) nothing is written, the outcome is counted, and the
row serves its existing `title` exactly as today.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.metrics import EVENT_DISPLAY_TITLE_TOTAL
from app.services.event_dedup import (
    DedupConfig,
    _load_validated_config,  # noqa: F401 — the SAME validated-read path every other key in this plan uses; never coerced.
    _tokenize,
    distinctive_set,
    load_dedup_config,
    venue_name_tokens,
)
from app.services.event_identity import normalize_title

logger = logging.getLogger(__name__)

ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY = "admin_config:event_display_title_enabled"
# OFF by default, like every other flag in this plan: the deploy of this
# branch must provably change no stored row, and this pass WRITES one.
DEFAULT_DISPLAY_TITLE_ENABLED = False

# The longest string this will ever write. A display title is a card
# headline, not a description; a model that answers with a paragraph has
# misunderstood the task, and truncating it would invent a title nobody
# wrote.
MAX_DISPLAY_TITLE_LENGTH = 120

OUTCOME_OBVIOUS = "obvious"
OUTCOME_LLM_ACCEPTED = "llm_accepted"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"
OUTCOME_SKIPPED_OPERATOR_EDITED = "skipped_operator_edited"


def validate_display_title_enabled_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("event display-title flag must be a boolean")
    return value


def load_display_title_enabled(redis_like) -> bool:
    return _load_validated_config(
        redis_like, ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY, DEFAULT_DISPLAY_TITLE_ENABLED,
        validator=validate_display_title_enabled_config, module_tag="event_display_title",
    )


def _sets_for(titles, *, venue_tokens, config: DedupConfig) -> list:
    return [
        distinctive_set(
            title, venue_tokens=venue_tokens,
            generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
        )
        for title in titles
    ]


def obvious_canonical_title(
    source_titles: list, *, venue_name: Optional[str], config: DedupConfig,
) -> Optional[str]:
    """The display title when ONE source title obviously wins, else `None`
    (which is the caller's signal to spend exactly one model call).

    A pure predicate over the group's SOURCE titles — which survive the merge
    on `post_item_source.raw_extraction["title"]` — never over the single
    surviving `title`, which is the arbitrary pick this whole feature exists
    to replace. Reuses `distinctive_set` and `normalize_title`, never a second
    notion of "the same title", and is symmetric, order-independent (except
    for the tie-break below) and total.

    `source_titles` must arrive OLDEST-FIRST, which is exactly what
    `list_event_sources` returns; the only place order matters is the
    tie-break, which takes the oldest of several equally-maximal titles.

    The rule:
      - take every title whose distinctive set is NON-EMPTY (an empty set is
        a subset of everything, so a generic `SEXTOU NO …` post can never be
        the maximum — it never forces a call and never wins);
      - keep the MAXIMAL ones: those whose set is not a PROPER subset of any
        other's;
      - if every maximal title shares one `normalize_title`, that is the
        obvious pick (returned in the oldest source's own raw spelling);
      - otherwise there is no obvious pick.

    `Rodolpho` / `Rodolpho Produções` / `31º Rodolpho Produções` has a unique
    maximum -> no call. Club Metrópole's five acts have five mutually
    non-comparable sets -> one call.
    """
    titles = [t for t in source_titles if t]
    if not titles:
        return None
    venue_tokens = venue_name_tokens(venue_name)
    sets = _sets_for(titles, venue_tokens=venue_tokens, config=config)

    candidates = [(t, s) for t, s in zip(titles, sets) if s]
    if not candidates:
        # Every title in the group is entirely generic. There is nothing for
        # a model to choose BETWEEN either, so spending a call on it would
        # buy nothing: the oldest post's own words are as good an answer as
        # exists, and they are at least a string some human actually wrote.
        return titles[0]

    maximal = [
        (title, own)
        for title, own in candidates
        if not any(own < other for _other_title, other in candidates)
    ]
    if not maximal:  # pragma: no cover - a proper-subset cycle is impossible
        return None
    normalized = {normalize_title(title) for title, _own in maximal}
    if len(normalized) != 1:
        return None
    return maximal[0][0]


def validate_display_title(
    candidate: Optional[str], source_titles: list, *, venue_name: Optional[str],
    config: DedupConfig,
) -> Optional[str]:
    """The deterministic gate every model answer goes through. Returns the
    accepted string, or `None` — and `None` means WRITE NOTHING, never
    "write something slightly different"."""
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    if not candidate:
        return None
    # At least one real WORD, not merely a non-empty string: `normalize_title`
    # deliberately keeps punctuation (260812 measured that stripping it
    # re-keys the corpus), so "!!! ---" is non-empty after normalisation and
    # has an empty distinctive set, which would otherwise sail through the
    # subset check below as "it invents nothing". It is not a headline.
    if not _tokenize(normalize_title(candidate)):
        return None
    if len(candidate) > MAX_DISPLAY_TITLE_LENGTH:
        return None
    venue_tokens = venue_name_tokens(venue_name)
    allowed: set = set()
    for source_set in _sets_for(
        [t for t in source_titles if t], venue_tokens=venue_tokens, config=config,
    ):
        allowed |= set(source_set)
    chosen = distinctive_set(
        candidate, venue_tokens=venue_tokens,
        generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
    )
    # The model may SELECT, SHORTEN or RECOMBINE what the posts said. It may
    # never introduce a proper noun no post contained — that is a fabricated
    # act name on a card a reader will believe.
    if not chosen <= allowed:
        return None
    return candidate


@dataclass(frozen=True)
class DisplayTitleGroup:
    """One canonical event's inputs, as the pass sees them."""

    event_id: str
    venue_name: Optional[str]
    local_date: Optional[str]
    source_titles: tuple
    lineup: tuple


class EventDisplayTitleService:
    """The post-merge pass. Walks the canonical rows a run touched and gives
    each merged listing one display title.

    At most ONE model call per merge GROUP — never per pair, never per post —
    and zero calls for a group with an obvious title, a single source, an
    operator-edited title, or a display title it already carries.
    """

    def __init__(self, venue_dao, openai_client, *, redis_client=None):
        self.venue_dao = venue_dao
        self.openai_client = openai_client
        self.redis_client = redis_client

    def _venue_name(self, venue_id: Optional[str]) -> Optional[str]:
        if not venue_id:
            return None
        venue = self.venue_dao.get_venue(venue_id)
        if venue is None:
            return None
        if isinstance(venue, dict):
            return venue.get("venue_name")
        return getattr(venue, "venue_name", None)

    def _source_titles(self, event_id: str) -> list:
        """Each announcing post's OWN extracted title, oldest first. The
        merge folds the rows but never the sources, so this is still exactly
        what each post said — the single surviving `title` is not."""
        titles = []
        for source in self.venue_dao.list_event_sources(event_id):
            raw = source.get("raw_extraction")
            title = raw.get("title") if isinstance(raw, dict) else None
            if title:
                titles.append(title)
        return titles

    async def run_for_events(self, event_ids, *, config: Optional[DedupConfig] = None) -> dict:
        """Returns a small outcome-count dict for the caller's own report.
        NEVER raises: an OpenAI failure in this pass must not fail the
        extraction run that triggered it."""
        if not load_display_title_enabled(self.redis_client):
            return {}
        if config is None:
            config = load_dedup_config(self.redis_client)

        counts: dict = {}

        def _bump(outcome: str) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1
            EVENT_DISPLAY_TITLE_TOTAL.labels(outcome=outcome).inc()

        for event_id in dict.fromkeys(event_ids):
            row = self.venue_dao.get_event(event_id)
            if row is None or row.get("status") == "superseded":
                continue
            if row.get("display_title"):
                # Already chosen, and still valid: `_finish_absorption`
                # clears it the moment the group's membership changes, so a
                # non-null value here is never stale.
                continue
            if "title" in (row.get("operator_edited_fields") or []):
                _bump(OUTCOME_SKIPPED_OPERATOR_EDITED)
                continue

            source_titles = self._source_titles(event_id)
            if len(source_titles) < 2:
                continue

            venue_name = self._venue_name(row.get("venue_id"))
            obvious = obvious_canonical_title(
                source_titles, venue_name=venue_name, config=config,
            )
            if obvious is not None:
                self.venue_dao.update_event(event_id, {"display_title": obvious})
                _bump(OUTCOME_OBVIOUS)
                continue

            chosen = await self._pick_with_model(
                row, source_titles, venue_name=venue_name, config=config, bump=_bump,
            )
            if chosen is not None:
                self.venue_dao.update_event(event_id, {"display_title": chosen})
        return counts

    async def _pick_with_model(self, row, source_titles, *, venue_name, config, bump):
        if self.openai_client is None:
            bump(OUTCOME_ERROR)
            return None
        starts_at = row.get("starts_at")
        local_date = starts_at.date().isoformat() if starts_at is not None else None
        try:
            raw = await self.openai_client.pick_display_title(
                venue_name=venue_name, local_date=local_date,
                source_titles=list(source_titles), lineup=list(row.get("lineup") or []),
            )
        except Exception as e:
            # An availability problem, never a data problem: the fallback
            # writes nothing and the row keeps its existing title.
            logger.warning(
                f"[EventDisplayTitle] title pick failed for {row['event_id']}: {e}"
            )
            bump(OUTCOME_ERROR)
            return None

        candidate = parse_display_title_response(raw)
        accepted = validate_display_title(
            candidate, source_titles, venue_name=venue_name, config=config,
        )
        if accepted is None:
            # Log the rejected STRING (never the raw model payload) — a
            # climbing `rejected` means the gate is doing its job and the
            # prompt needs work.
            logger.info(
                "[EventDisplayTitle] rejected display title for %s: %r",
                row["event_id"], candidate,
            )
            bump(OUTCOME_REJECTED)
            return None
        bump(OUTCOME_LLM_ACCEPTED)
        return accepted


def parse_display_title_response(raw_text) -> Optional[str]:
    """`{"display_title": str}` -> the string, or `None` for anything else.
    Pure, and deliberately forgiving of shape but not of content: a malformed
    reply is a rejection, never a partial write."""
    import json

    if not raw_text:
        return None
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("display_title")
    return value if isinstance(value, str) else None


__all__ = [
    "ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY", "DEFAULT_DISPLAY_TITLE_ENABLED",
    "MAX_DISPLAY_TITLE_LENGTH",
    "OUTCOME_OBVIOUS", "OUTCOME_LLM_ACCEPTED", "OUTCOME_REJECTED", "OUTCOME_ERROR",
    "OUTCOME_SKIPPED_OPERATOR_EDITED",
    "validate_display_title_enabled_config", "load_display_title_enabled",
    "obvious_canonical_title", "validate_display_title",
    "parse_display_title_response", "DisplayTitleGroup", "EventDisplayTitleService",
]
