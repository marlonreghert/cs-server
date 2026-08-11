"""Behave steps for
tests/bdd/enrichment/merge-unresolved-into-resolved-sibling.feature.

See plans/260811_merge-unresolved-into-resolved-sibling.md. Every scenario
seeds its events directly through the DAO — the SAME pattern
one_event_many_posts_steps.py's own "no venue resolved"/"no date" scenarios
(Scenarios 7/8 there) and event_ticket_info_and_attractions_steps.py's
`_seed_existing_event` already establish for exactly this situation: what
these scenarios test is the MERGE (app.services.event_merge.
merge_touched_events), not a real post's own extraction (covered elsewhere),
and an unresolved item can never arise through the single-venue
EventExtractionService path at all — it always attributes to the posting
venue (see one_event_many_posts_steps.py's own docstring) — only a shared-
handle/promoter path produces one, which direct seeding stands in for
without needing the full crawl/resolution-ladder orchestration.

Reuses the `ee_` harness's "the event extraction pipeline is configured for
a known venue" Background, already registered by
event_ticket_info_and_attractions_steps.py — `context.ee_dao`/
`context.ee_venue_id`/`context.ee_handle` are set up by the time any Given
below runs; redefining that step text here would raise behave's
AmbiguousStep.

Every "Then" below names the SPECIFIC surviving event id (never just a bare
count) and asserts on `context.ee_venue_id` specifically — a prior round of
this exact feature shipped a scenario that passed BECAUSE of the bug it was
meant to catch, and a count-only assertion stayed green against a
deliberately reintroduced defect because both the buggy and fixed code paths
produced the same count. See the module docstring's own note in
app/services/event_merge.py for the structural (never order-dependent)
direction guarantee this file's "whichever arrives first" scenario proves.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.models.venue import Venue
from app.services.event_merge import merge_touched_events
from app.services.event_reconciliation import new_event_id
from app.services.event_venue_resolution import METHOD_SIBLING_MERGE, RESOLUTION_AUTO

_TITLE = "Oficina de Sorvete"
_DATE = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)
_OTHER_DATE = datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc)
_OTHER_HANDLE = "a_different_account"


def _start(context) -> None:
    """Every scenario's FIRST domain Given (never the shared Background)
    calls this to start a fresh touched-id list — `_seed` appends to it, and
    the shared "the posts are extracted" When reads it back, so a scenario
    never has to hand-maintain its own id bookkeeping."""
    context.muirs_touched_ids = []
    context.muirs_resolved_id = None
    context.muirs_unresolved_id = None


def _seed(context, shortcode: str, **fields) -> str:
    event_id = new_event_id()
    base = {
        "event_id": event_id, "source_shortcode": shortcode,
        "source_event_key": f"{shortcode}_key",
        "source_permalink": f"https://instagram.com/p/{shortcode}",
        "raw_extraction": {"time_known": True},
    }
    base.update(fields)
    context.ee_dao.insert_event(base)
    context.muirs_touched_ids.append(event_id)
    return event_id


def _seed_resolved(context, shortcode: str, *, venue_id=None, handle=None, starts_at=_DATE,
                    title=_TITLE, **overrides) -> str:
    fields = dict(
        venue_id=venue_id or context.ee_venue_id, source_kind="venue_post",
        source_handle=handle or context.ee_handle, title=title, starts_at=starts_at,
        status="accepted",
    )
    fields.update(overrides)
    return _seed(context, shortcode, **fields)


def _seed_unresolved(context, shortcode: str, *, handle=None, starts_at=_DATE,
                      title=_TITLE, **overrides) -> str:
    fields = dict(
        venue_id=None, source_kind="promoter_post",
        source_handle=handle or context.ee_handle, title=title, starts_at=starts_at,
        status="pending_review", review_reason="unresolved_venue",
    )
    fields.update(overrides)
    return _seed(context, shortcode, **fields)


# ── the pair: one resolved, one not (same account, same day) ────────────────
def _reset_pair_params(context) -> None:
    _start(context)
    context.muirs_resolved_handle = context.ee_handle
    context.muirs_unresolved_handle = context.ee_handle
    context.muirs_unresolved_date = _DATE


@given("two posts from one account announcing the same thing on the same day")
def step_given_two_posts_one_account_same_day(context):
    _reset_pair_params(context)


@given("two posts from different accounts announcing the same thing that day")
def step_given_two_posts_different_accounts(context):
    _reset_pair_params(context)
    context.muirs_unresolved_handle = _OTHER_HANDLE


@given("two posts from one account announcing the same thing on different days")
def step_given_two_posts_different_days(context):
    _reset_pair_params(context)
    context.muirs_unresolved_date = _OTHER_DATE


@given("only one of them resolved a venue")
def step_given_only_one_resolved_a_venue(context):
    # The UNRESOLVED item is seeded FIRST on purpose — its event_id (a
    # time-ordered ULID) therefore sorts BEFORE the resolved item's. A
    # canonical-selection bug that picks "the oldest id in the whole group"
    # instead of "the oldest id among the RESOLVED members" would silently
    # pick the unresolved item here and pass every OTHER fixture in this
    # file, since they all happen to create the resolved member first —
    # this is the one shaped to catch exactly that.
    context.muirs_unresolved_id = _seed_unresolved(
        context, "muirs_unresolved", handle=context.muirs_unresolved_handle,
        starts_at=context.muirs_unresolved_date,
    )
    context.muirs_resolved_id = _seed_resolved(
        context, "muirs_resolved", handle=context.muirs_resolved_handle,
    )
    assert context.muirs_unresolved_id < context.muirs_resolved_id, (
        "fixture sanity: the unresolved item's id must sort first for the "
        "direction proof below to mean anything"
    )


@given("the unresolved post's date was a collapsed range")
def step_given_unresolved_date_was_a_collapsed_range(context):
    context.ee_dao.update_event(context.muirs_unresolved_id, {
        "review_reason": "date_range; unresolved_venue",
    })


# ── refusing to guess: two resolved venues sharing one handle ───────────────
@given("a handle whose posts resolved to two different venues on one day")
def step_given_handle_two_venues(context):
    _start(context)
    second_venue_id = "muirs_venue_2"
    context.ee_dao.upsert_venue(Venue(
        venue_id=second_venue_id, venue_name="Entre Amigos O Bode Espinheiro",
        venue_lat=-8.06, venue_lng=-34.90,
    ))
    context.muirs_venue_a_id = _seed_resolved(context, "muirs_venue_a", venue_id=context.ee_venue_id)
    context.muirs_venue_b_id = _seed_resolved(context, "muirs_venue_b", venue_id=second_venue_id)


@given("an unresolved post announcing the same thing that day")
def step_given_unresolved_post_announcing_same_thing(context):
    context.muirs_unresolved_id = _seed_unresolved(context, "muirs_ambiguous_unresolved")


# ── refusing to guess: the unresolved item's own status is a decision ───────
@given("an unresolved item an operator has confirmed")
def step_given_unresolved_item_confirmed(context):
    _start(context)
    context.muirs_unresolved_id = _seed_unresolved(
        context, "muirs_confirmed_unresolved", status="confirmed", review_reason=None,
    )


@given("an unresolved item whose venue an operator has edited")
def step_given_unresolved_item_operator_edited_venue(context):
    _start(context)
    context.muirs_unresolved_id = _seed_unresolved(
        context, "muirs_edited_unresolved", operator_edited_fields=["venue_id"],
    )


@given("a resolved sibling from the same account on the same day")
def step_given_resolved_sibling_same_account_same_day(context):
    context.muirs_resolved_id = _seed_resolved(context, "muirs_protected_resolved")


# ── refusing to guess: no date, no handle identity ───────────────────────────
@given("an unresolved item with no date")
def step_given_unresolved_item_no_date(context):
    _start(context)
    context.muirs_unresolved_id = _seed_unresolved(context, "muirs_nodate_unresolved", starts_at=None)


@given("a resolved sibling from the same account")
def step_given_resolved_sibling_same_account(context):
    context.muirs_resolved_id = _seed_resolved(context, "muirs_nodate_resolved")


# ── the unchanged baseline: two already-resolved posts ───────────────────────
@given("two posts announcing the same thing at the same venue on one day")
def step_given_two_resolved_posts_same_venue(context):
    _start(context)
    context.muirs_resolved_id = _seed_resolved(context, "muirs_baseline_a")
    context.muirs_second_resolved_id = _seed_resolved(context, "muirs_baseline_b")


def _ordered(context, first_id) -> list[str]:
    """`context.muirs_touched_ids` in insertion order, with `first_id`
    (when set) moved to the front — PROCESSING order, deliberately
    independent of the CREATION order `_seed` recorded it in. The pair's
    creation order is separately pinned (unresolved first, see "only one of
    them resolved a venue") to stress the id-sorting shape of this bug;
    this function stresses the ITERATION-order shape on top of it, so a
    fix that only guards one of the two can never pass both."""
    ids = list(context.muirs_touched_ids)
    if first_id and first_id in ids:
        ids.remove(first_id)
        ids.insert(0, first_id)
    return ids


# ── When ──────────────────────────────────────────────────────────────────────
# "the posts are extracted" would collide with date_correctness_and_path_
# parity_steps.py's OWN identical text (bound to a real EventExtractionService
# run over `context.ee_post_source`/`context.ee_openai` — not what this file's
# direct-DAO-seeded scenarios need at all) — an AmbiguousStep collision.
# Reworded here, the same distinguishing-wording convention this suite's other
# sibling-feature step modules already document for this exact situation.
@when("the touched events are merged")
def step_when_the_touched_events_are_merged(context):
    ids = _ordered(context, context.muirs_resolved_id)
    merge_touched_events(context.ee_dao, ids, datetime.now(timezone.utc))


@when("the unresolved post is extracted first")
def step_when_unresolved_post_extracted_first(context):
    ids = _ordered(context, context.muirs_unresolved_id)
    merge_touched_events(context.ee_dao, ids, datetime.now(timezone.utc))


# ── Then ──────────────────────────────────────────────────────────────────────
@then("one item survives")
def step_then_one_item_survives(context):
    candidate_ids = [
        eid for eid in (
            context.muirs_resolved_id, getattr(context, "muirs_second_resolved_id", None),
            context.muirs_unresolved_id,
        ) if eid
    ]
    survivors = [eid for eid in candidate_ids if context.ee_dao.get_event(eid) is not None]
    assert len(survivors) == 1, f"expected exactly one survivor among {candidate_ids}; got {survivors}"
    assert survivors[0] == context.muirs_resolved_id, (
        f"expected the RESOLVED item {context.muirs_resolved_id!r} to be the survivor "
        f"(direction must never reverse); got {survivors[0]!r}"
    )
    if context.muirs_unresolved_id:
        assert context.ee_dao.get_event(context.muirs_unresolved_id) is None, (
            "the unresolved item must be absorbed (deleted), not merely left empty"
        )


@then("the surviving item is attributed to that venue")
def step_then_surviving_item_attributed_to_that_venue(context):
    row = context.ee_dao.get_event(context.muirs_resolved_id)
    assert row is not None, "the resolved sibling must survive"
    assert row["venue_id"] == context.ee_venue_id, row
    assert context.ee_dao.get_event(context.muirs_unresolved_id) is None, (
        "the unresolved item must have been absorbed into the survivor"
    )


@then("the unresolved item keeps no venue")
def step_then_unresolved_item_keeps_no_venue(context):
    row = context.ee_dao.get_event(context.muirs_unresolved_id)
    assert row is not None, "an ambiguous unresolved item must never be deleted"
    assert row["venue_id"] is None, row
    # Neither resolved sibling was dragged into this either — an ambiguous
    # group is left ENTIRELY alone, not just the unresolved item.
    assert context.ee_dao.get_event(context.muirs_venue_a_id) is not None
    assert context.ee_dao.get_event(context.muirs_venue_b_id) is not None


@then("the unresolved item awaits a human decision")
def step_then_unresolved_item_awaits_a_human_decision(context):
    queue_ids = {r["event_id"] for r in context.ee_dao.list_events_awaiting_decision()}
    assert context.muirs_unresolved_id in queue_ids, queue_ids


@then("the confirmed item is unchanged")
def step_then_confirmed_item_is_unchanged(context):
    row = context.ee_dao.get_event(context.muirs_unresolved_id)
    assert row is not None, "a confirmed item must never be deleted by a merge"
    assert row["status"] == "confirmed", row
    assert row["venue_id"] is None, row
    assert context.ee_dao.get_event(context.muirs_resolved_id) is not None, (
        "the resolved sibling must survive untouched — nothing merged at all"
    )


@then("the operator's venue is unchanged")
def step_then_operators_venue_is_unchanged(context):
    row = context.ee_dao.get_event(context.muirs_unresolved_id)
    assert row is not None, "an operator-edited item must never be deleted by a merge"
    assert row["venue_id"] is None, row
    assert row["operator_edited_fields"] == ["venue_id"], row
    assert context.ee_dao.get_event(context.muirs_resolved_id) is not None, (
        "the resolved sibling must survive untouched — nothing merged at all"
    )


@then("both items survive")
def step_then_both_items_survive(context):
    resolved = context.ee_dao.get_event(context.muirs_resolved_id)
    unresolved = context.ee_dao.get_event(context.muirs_unresolved_id)
    assert resolved is not None, "the resolved item must not have been deleted"
    assert unresolved is not None, "the unresolved item must not have been deleted"
    assert unresolved["venue_id"] is None, unresolved


@then("the surviving item no longer reports an unresolved venue")
def step_then_no_longer_reports_unresolved_venue(context):
    row = context.ee_dao.get_event(context.muirs_resolved_id)
    assert row is not None
    assert "unresolved_venue" not in (row.get("review_reason") or ""), row


@then("the surviving item still reports a collapsed date range")
def step_then_still_reports_collapsed_date_range(context):
    row = context.ee_dao.get_event(context.muirs_resolved_id)
    assert row is not None
    assert "date_range" in (row.get("review_reason") or ""), row


@then("the surviving item records that its venue was adopted from a sibling")
def step_then_records_venue_adopted_from_sibling(context):
    row = context.ee_dao.get_event(context.muirs_resolved_id)
    assert row is not None
    assert row.get("linked_by") == METHOD_SIBLING_MERGE, row
    assert row.get("location_resolution") == RESOLUTION_AUTO, row
