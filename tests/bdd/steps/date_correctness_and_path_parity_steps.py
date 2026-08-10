"""Behave steps for
tests/bdd/enrichment/date-correctness-and-path-parity.feature.

See plans/260810_date-correctness-review-reasons-and-path-parity.md. Two
harnesses, reused verbatim from their owning modules rather than duplicated
(the same cross-file reuse discipline every sibling enrichment feature in
this suite already follows — see e.g. post_kind_and_post_extraction_
attribution_steps.py's own docstring):

  - §A/§B/§C's "any post that produces a queued event" case: the REAL
    `EventExtractionService`, over `context.ee_*`
    (tests.bdd.steps.instagram_event_extraction_steps). The Background
    ("the event extraction pipeline is configured for a known venue") and
    "the post is extracted" are ALREADY registered by
    event_ticket_info_and_attractions_steps.py — reused, never redefined
    (redefining either would raise behave's AmbiguousStep).

  - §C/§D's shared-handle scenarios: the REAL `ScheduledInstagramCrawl-
    Service`/`InstagramCrawlChainer`/`PromoterCrawlService`, over
    `context.ic_*` (tests.bdd.steps.scheduled_incremental_instagram_crawl_
    steps), reusing stream_dedupe_and_venue_attribution_steps.py's own
    two-venue-handle fixture (`_setup_two_venue_handle`/`_replace_post`) —
    the SAME setup that module already built for the identical "a crawl
    target whose handle belongs to two venues" Given text, which is ALSO
    reused here verbatim (already registered there).

Two Then/When steps are worded slightly off the feature file's most obvious
phrasing specifically to avoid AmbiguousStep against steps already registered
for a DIFFERENT context (`context.ee_dao` vs `context.ic_dao`) under the
identical literal text — the same collision-avoidance pattern
date_resolution_correctness_steps.py already documents and applies:
  - "the event awaits a human decision" (stream_dedupe_and_venue_
    attribution_steps.py, hardwired to `context.ic_dao`) would silently read
    the WRONG store for this feature's ee_-based scenario, so the feature
    file says "the event is not auto-accepted" instead — same assertion,
    different words.
  - "the review queue is listed" (post_kind_and_post_extraction_attribution_
    steps.py, hardwired to `context.ee_dao`) has the same problem for this
    feature's ic_-based scenario, so the feature file says "the shared-handle
    review queue is listed" instead.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _run_extraction,
    _stored_event,
)
from tests.bdd.steps.post_kind_and_post_extraction_attribution_steps import _kind_metric
from tests.bdd.steps.scheduled_incremental_instagram_crawl_steps import (
    _post as _ic_post,
    _program_posts,
)
from tests.bdd.steps.stream_dedupe_and_venue_attribution_steps import (
    _BRANCH_CAPTION,
    _NEITHER_CAPTION,
    _replace_post,
    _sole_event,
)

RECIFE = ZoneInfo("America/Recife")


def _metric(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _event_json(**overrides) -> dict:
    payload = {
        "kind": "event", "title": "Festa", "description": None, "date_text": None,
        "time_text": None, "is_recurring": False, "recurrence_text": None,
        "lineup": [], "ticket_url": None, "price_text": None, "location_text": None,
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


# ── §A: a guessed year is a guess (ee_ harness) ──────────────────────────────
@given("a post announcing a date earlier in the year than the post itself")
def step_given_date_earlier_in_the_year(context):
    # December anchor, an August date with no year stated: before the
    # anchor, so it rolls forward a year (app.services.event_date_resolver.
    # _roll_forward) — plans/260807_date-resolution-correctness.md's own
    # "no year across a year boundary" case, now also flagged as a guess.
    ts = datetime(2026, 12, 1, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_rolled", timestamp=ts)
    context.ee_openai.program(json.dumps({"events": [
        _event_json(date_text="15/08", time_text="20h"),
    ]}))


@given("a post announcing a date later than the post itself")
def step_given_date_later_in_the_year(context):
    ts = datetime(2026, 7, 1, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_future", timestamp=ts)
    context.ee_openai.program(json.dumps({"events": [
        _event_json(date_text="15/08", time_text="20h"),
    ]}))


@then("the event is flagged as having an inferred year")
def step_then_flagged_year_inferred(context):
    row = _stored_event(context)
    assert "year_inferred" in (row.get("review_reason") or ""), row


@then("the event is not auto-accepted")
def step_then_not_auto_accepted(context):
    row = _stored_event(context)
    assert row["status"] != "accepted", row
    assert row["status"] == "pending_review", row


@then("the event is not flagged as having an inferred year")
def step_then_not_flagged_year_inferred(context):
    row = _stored_event(context)
    assert "year_inferred" not in (row.get("review_reason") or ""), row


# ── Siblings from one flyer agree ────────────────────────────────────────────
# The real production case (plans/260810_date-correctness-review-reasons-and-
# path-parity.md §Evidence A): one flyer, four weekly dates, resolved against
# a mid-programme post timestamp. Weeks 1-2 individually roll forward a year;
# weeks 3-4 resolve natively within the post's own year. All four must land
# on the SAME year once siblings vote.
_FERIAS_DATE_TEXTS = [
    "01, 02 e 03 de julho",
    "08, 09 e 10 de julho",
    "15, 16 e 17 de julho",
    "22, 23 e 24 de julho",
]


@given("one post announcing four weekly dates, two of them already past")
def step_given_ferias_amigos_park(context):
    # The anchor that actually matches the plan's own Evidence section:
    # "weeks 1 AND 2 had already happened" (both) means the anchor sits
    # strictly between week 2's last day (10 July) and week 3's first day
    # (15 July) -- 13 July. That produces a genuine 2-2 SPLIT by year
    # (2027, 2027, 2026, 2026), a TIE by raw count, not a 3-1 majority. An
    # earlier draft of this fixture used 5 July, which produces a 3-1
    # majority and made the test pass without ever exercising the tie
    # rule -- the anchor that matches production is the one that must be
    # tested.
    ts = datetime(2026, 7, 13, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_ferias", timestamp=ts)
    events = [
        _event_json(title=f"FERIAS AMIGOS PARK -- semana {i + 1}", date_text=text, time_text="16h")
        for i, text in enumerate(_FERIAS_DATE_TEXTS)
    ]
    context.ee_openai.program(json.dumps({"events": events}))


def _ferias_events(context) -> list[dict]:
    rows = [
        r for r in context.ee_dao.list_events(venue_id=context.ee_venue_id)
        if r["source_shortcode"] == "dcpp_ferias"
    ]
    assert len(rows) == 4, rows
    return rows


@then("every event resolves to the same year")
def step_then_every_event_same_year(context):
    years = {r["starts_at"].year for r in _ferias_events(context)}
    assert years == {2026}, years


@then("no event is dated a year later than its siblings")
def step_then_no_event_dated_later_than_siblings(context):
    years = [r["starts_at"].year for r in _ferias_events(context)]
    assert max(years) == min(years), years


# ── Never across posts ───────────────────────────────────────────────────────
@given("two unrelated posts each announcing one date")
def step_given_two_unrelated_posts(context):
    # Post A: rolls (its date is before ITS OWN anchor) -- alone in its own
    # post, so nothing to vote against.
    ts_a = datetime(2026, 12, 1, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_unrelated_a", timestamp=ts_a)
    context.ee_openai.program(json.dumps({"events": [
        _event_json(title="Post A", date_text="15/08", time_text="20h"),
    ]}))
    # Post B: resolves natively within its own year -- an UNRELATED event
    # that must never drag Post A's roll back, nor be dragged by it.
    ts_b = datetime(2026, 3, 1, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_unrelated_b", timestamp=ts_b)
    context.ee_openai.program(json.dumps({"events": [
        _event_json(title="Post B", date_text="20/06", time_text="20h"),
    ]}))


@when("the posts are extracted")
def step_when_the_posts_are_extracted(context):
    _run_extraction(context)


@then("each event resolves from its own post only")
def step_then_each_event_resolves_from_own_post(context):
    row_a = context.ee_dao.get_event_by_source(context.ee_handle, "dcpp_unrelated_a")
    row_b = context.ee_dao.get_event_by_source(context.ee_handle, "dcpp_unrelated_b")
    assert row_a is not None and row_b is not None, (row_a, row_b)
    # Post A's date rolled forward -- its own siblings (none) never pulled it
    # back, and Post B's unrelated, correctly-resolved date never dragged it
    # either.
    assert row_a["starts_at"].date().isoformat() == "2027-08-15", row_a
    assert "year_inferred" in (row_a["review_reason"] or ""), row_a
    # Post B never rolled and is untouched by Post A's inference.
    assert row_b["starts_at"].date().isoformat() == "2026-06-20", row_b
    assert "year_inferred" not in (row_b["review_reason"] or ""), row_b


# ── A tie refuses to guess ───────────────────────────────────────────────────
@given("one post whose dates split evenly between two years")
def step_given_tie_split_years(context):
    # Sibling voting is scoped to ONE calendar month (a post can genuinely
    # span a year boundary, e.g. a December/January programme -- see the
    # unit test coverage in test_event_date_resolver.py). So a "split
    # evenly between two years" that is genuinely UNRESOLVABLE, not just a
    # different-months no-op, needs two DISAGREEING explicitly-stated years
    # within the SAME month, plus a third, bare date that rolls to a THIRD
    # year -- a 1-1-1 tie with no unique trusted answer to defer to.
    # `_roll_forward` always rolls every inferred sibling in one post to
    # the SAME target year, so two inferred siblings can never disagree
    # with each other; the only way a same-month tie is genuinely
    # ambiguous is exactly this: two different non-inferred years both
    # present at once.
    ts = datetime(2026, 12, 1, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_tie", timestamp=ts)
    events = [
        _event_json(title="Tie A (explicit 2025)", date_text="20/08/2025", time_text="20h"),
        _event_json(title="Tie B (explicit 2028)", date_text="22/08/2028", time_text="20h"),
        _event_json(title="Tie C (rolls to 2027)", date_text="25/08", time_text="20h"),
    ]
    context.ee_openai.program(json.dumps({"events": events}))


@then("the events are flagged as having an inferred year")
def step_then_tie_events_flagged(context):
    rows = [
        r for r in context.ee_dao.list_events(venue_id=context.ee_venue_id)
        if r["source_shortcode"] == "dcpp_tie"
    ]
    assert len(rows) == 3, rows
    rolled = [r for r in rows if "year_inferred" in (r.get("review_reason") or "")]
    assert len(rolled) == 1, rows
    # The tie was refused, not guessed: all three events keep their own,
    # individually-resolved year rather than being forced to agree.
    years = {r["starts_at"].year for r in rows}
    assert years == {2025, 2027, 2028}, rows


# ── §B: a range starts on its first day ──────────────────────────────────────
@given("a post announcing an event across three consecutive days")
def step_given_three_day_range(context):
    ts = datetime(2026, 6, 1, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_range", timestamp=ts)
    context.ee_openai.program(json.dumps({"events": [
        _event_json(date_text="01, 02 e 03 de julho", time_text="16h"),
    ]}))


@given("a post announcing an event on one date")
def step_given_single_date_event(context):
    ts = datetime(2026, 6, 1, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_single_date", timestamp=ts)
    context.ee_openai.program(json.dumps({"events": [
        _event_json(date_text="15/08", time_text="20h"),
    ]}))


@then("the event starts on the first of those days")
def step_then_starts_on_first_day(context):
    row = _stored_event(context)
    assert row["starts_at"].date().isoformat() == "2026-07-01", row


@then("the event is flagged as having a collapsed date range")
def step_then_flagged_date_range(context):
    row = _stored_event(context)
    assert "date_range" in (row.get("review_reason") or ""), row


@then("the event is not flagged as having a collapsed date range")
def step_then_not_flagged_date_range(context):
    row = _stored_event(context)
    assert "date_range" not in (row.get("review_reason") or ""), row


# ── §C: every queued event says why (ee_ harness) ────────────────────────────
@given("any post that produces a queued event")
def step_given_any_post_that_queues(context):
    # Deliberately NOT low_confidence: that reason was already stated by the
    # pre-existing (pre-260810) code path and proves nothing new. A date
    # RANGE is otherwise clean (confidence high, venue known, a real
    # resolved date) -- under the OLD reconciliation code (no `date_range`
    # concept, no central review-reason invariant) this event was silently
    # ACCEPTED, not queued at all. Only §C's central enforcement (in
    # app.services.event_reconciliation.reconcile_post_events) keeps it out
    # of auto-accept AND states why.
    ts = datetime(2026, 6, 1, 20, 0, tzinfo=RECIFE)
    _add_post(context, "dcpp_queued", timestamp=ts)
    context.ee_openai.program(json.dumps({"events": [
        _event_json(date_text="01, 02 e 03 de julho", time_text="16h", confidence=0.9),
    ]}))


@then("that event states a reason")
def step_then_states_a_reason(context):
    row = _stored_event(context)
    assert row["status"] == "pending_review", row
    assert row.get("review_reason"), row


# ── §C/§D: the shared-handle path (ic_ harness) ──────────────────────────────
@given("a post naming neither venue")
def step_given_post_naming_neither_venue(context):
    _replace_post(context, caption=_NEITHER_CAPTION)


@then("the event states that its venue could not be resolved")
def step_then_states_venue_unresolved(context):
    row = _sole_event(context)
    assert "unresolved_venue" in (row.get("review_reason") or ""), row


@when("the shared-handle review queue is listed")
def step_when_shared_handle_queue_listed(context):
    context.dcpp_ic_queue = context.ic_dao.list_events_awaiting_decision() or []


@then("that event is present in the queue")
def step_then_present_in_queue(context):
    row = _sole_event(context)
    ids = {r["event_id"] for r in context.dcpp_ic_queue}
    assert row["event_id"] in ids, (row, ids)
    # Not just "present" -- present AS a venue post. Pre-fix, this same
    # event was ALSO in the queue (by accident: the old predicate keyed on
    # the WRONG label, `promoter_post`, which is exactly what this path
    # used to stamp). Queue membership alone cannot tell the fix apart from
    # the bug it replaces; the label is what changed.
    assert row["source_kind"] == "venue_post", row


@then("each event records that it came from a venue post")
def step_then_each_event_is_a_venue_post(context):
    rows = context.ic_dao.list_events_by_source(context.ic_handle, context.sdva_shortcode)
    assert rows, "expected at least one event"
    for row in rows:
        assert row["source_kind"] == "venue_post", row


@given("one of its posts is a menu item")
def step_given_one_post_is_a_menu_item(context):
    # `context.pk_kind_before` -- the SAME attribute
    # post_kind_and_post_extraction_attribution_steps.py's ALREADY-registered
    # "the extraction is counted as a menu item" Then step reads (reused
    # verbatim below rather than redefined, which would collide) -- that
    # step only touches the global Prometheus REGISTRY and this attribute,
    # never context.ee_dao/context.ic_dao, so it is safe to reuse from a
    # shared-handle (ic_) scenario too.
    context.pk_kind_before = _kind_metric("not_an_event", "menu")
    _program_posts(context, context.ic_handle, "posts", [
        _ic_post(
            context.sdva_shortcode, "2026-08-05T20:00:00.000Z",
            # Must clear `matches_event_marker`'s caption pre-filter (a
            # ticketing term does it) so the post reaches the model at all —
            # the model's OWN answer, not the caption, is what says "menu".
            caption="Ingressos e cardapio especial disponiveis, confira!",
        ),
    ])
    context.ic_openai._responses = []
    # This fake (scheduled_incremental_instagram_crawl_steps._FakeOpenAIClient)
    # always wraps ONE flat event dict into {"events": [...]} itself -- unlike
    # instagram_event_extraction_steps.py's fake, it does not accept an
    # already-wrapped multi-event payload.
    context.ic_openai.program(json.dumps(
        _event_json(kind="menu", title="Cardapio", date_text=None, time_text=None),
    ))


@given("one post naming a venue and one naming neither")
def step_given_one_post_names_venue_one_names_neither(context):
    resolved_shortcode = "dcpp_resolved_post"
    ambiguous_shortcode = "dcpp_ambiguous_post"
    _program_posts(context, context.ic_handle, "posts", [
        _ic_post(resolved_shortcode, "2026-08-05T20:00:00.000Z", caption=_BRANCH_CAPTION),
        _ic_post(ambiguous_shortcode, "2026-08-06T20:00:00.000Z", caption=_NEITHER_CAPTION),
    ])
    context.ic_openai._responses = []
    context.ic_openai.program(json.dumps(_event_json()))
    context.ic_openai.program(json.dumps(_event_json()))
    context.dcpp_resolved_before = _metric("crawl_venue_attribution_total", {"outcome": "resolved"})
    context.dcpp_ambiguous_before = _metric("crawl_venue_attribution_total", {"outcome": "ambiguous"})


@then("one attribution is counted as resolved")
def step_then_one_attribution_resolved(context):
    after = _metric("crawl_venue_attribution_total", {"outcome": "resolved"})
    assert after - context.dcpp_resolved_before == 1.0, (context.dcpp_resolved_before, after)


@then("one attribution is counted as ambiguous")
def step_then_one_attribution_ambiguous(context):
    after = _metric("crawl_venue_attribution_total", {"outcome": "ambiguous"})
    assert after - context.dcpp_ambiguous_before == 1.0, (context.dcpp_ambiguous_before, after)


# ── shared Given already registered elsewhere, reused verbatim ──────────────
# "a crawl target whose handle belongs to two venues" ->
#   stream_dedupe_and_venue_attribution_steps.step_given_two_venue_handle
# "its scheduled crawl runs" ->
#   scheduled_incremental_instagram_crawl_steps's own When step
# "the event extraction pipeline is configured for a known venue" / "the
#   post is extracted" -> event_ticket_info_and_attractions_steps.py
