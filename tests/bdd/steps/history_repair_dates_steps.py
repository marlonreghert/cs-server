"""Behave steps for tests/bdd/enrichment/history-repair-dates.feature.

See plans/260812_history-repair-dates.md.

Drives `scripts.repair_event_dates.run_repair` directly against an in-memory
RDS fake (tests/rds_fake.py) wrapped in the SAME `VenueRepository` production
wiring uses — CLAUDE.md forbids live RDS/Apify/OpenAI in BDD, and this
script's own module docstring proves by construction that it never imports a
network client. Fixture rows are inserted directly via `venue_dao.
insert_event`, mirroring what a real crawl would have written BEFORE the
forward fixes (plans/260812_event-attribution-and-dates.md,
plans/260813_review-gate-and-date-vocabulary.md) landed — never via a live
extraction, since this feature repairs already-persisted history.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.dao.venue_repository import VenueRepository
from app.models.event_kind import KIND_EVENT
from app.models.venue import Venue
from app.services.event_date_resolver import RECIFE_TZ
from app.services.event_identity import compute_source_event_key
from scripts.repair_event_dates import date_text_shape, run_repair
from tests.rds_fake import InMemoryRdsVenueStore

_DEFAULT_UPLOADED_AT = datetime(2026, 8, 7, 12, 0, tzinfo=RECIFE_TZ)
_FIRST_SEEN_FLOOR = datetime(2020, 1, 1, tzinfo=timezone.utc)


class _Missing:
    __slots__ = ()


_MISSING = _Missing()  # "argument not passed" -- distinct from an explicit None


# ── harness setup ────────────────────────────────────────────────────────────
def _ensure_red(context) -> None:
    if getattr(context, "red_dao", None) is not None:
        return
    context.red_store = InMemoryRdsVenueStore()
    context.red_dao = VenueRepository(client=None, rds_store=context.red_store)
    context.red_items: list = []
    context.red_counter = 0
    context.red_report = None
    context.red_exception = None
    context.red_venue_id = "v_red_default"
    context.red_dao.upsert_venue(Venue(
        venue_id=context.red_venue_id, venue_name="Red Test Venue",
        venue_lat=-8.05, venue_lng=-34.88, venue_address="",
    ))


def _insert_item(
    context, *, date_text=_MISSING, time_text=None, starts_at=None,
    status: str = "pending_review", review_reason=None, operator_edited_fields=None,
    uploaded_at=_MISSING, title=None, venue_id=_MISSING, confidence: float = 0.9,
    post_type: str = KIND_EVENT, is_recurring=None, recurrence_text=None,
    date_interpretation=None, source_handle=None, source_shortcode=None,
) -> dict:
    """Inserts one `post_item` + its single `post_item_source` row, mirroring
    what a real extraction would have stored. `date_text`/`uploaded_at`/
    `venue_id` default to a sensible non-None value (matching the
    Background's "every source carries the post's own upload timestamp") but
    accept an EXPLICIT None to model "no date text", "no anchor", or "no
    venue" — a bare Python default of None could not tell those apart from
    "not mentioned by this scenario at all"."""
    _ensure_red(context)
    context.red_counter += 1
    n = context.red_counter
    title = title or f"Evento {n}"
    handle = source_handle or f"handle_{n}"
    shortcode = source_shortcode or f"post_{n}"
    resolved_date_text = None if date_text is _MISSING else date_text
    resolved_uploaded_at = _DEFAULT_UPLOADED_AT if uploaded_at is _MISSING else uploaded_at
    resolved_venue_id = context.red_venue_id if venue_id is _MISSING else venue_id
    event_id = f"evt_red_{n:04d}"
    key = compute_source_event_key(title, starts_at)
    seen_at = resolved_uploaded_at or _FIRST_SEEN_FLOOR

    fields = {
        "event_id": event_id, "venue_id": resolved_venue_id, "starts_at": starts_at,
        "ends_at": None, "is_recurring": bool(is_recurring), "recurrence_text": recurrence_text,
        "title": title, "status": status, "review_reason": review_reason,
        "operator_edited_fields": operator_edited_fields, "confidence": confidence,
        "post_type": post_type, "time_known": False,
        "source_kind": "venue_post", "source_handle": handle, "source_shortcode": shortcode,
        "source_permalink": f"https://instagram.com/p/{shortcode}",
        "source_event_key": key, "source_event_index": 1,
        "raw_extraction": {
            "date_text": resolved_date_text, "time_text": time_text,
            "is_recurring": is_recurring, "recurrence_text": recurrence_text,
        },
        "date_interpretation": date_interpretation,
        "source_uploaded_at": resolved_uploaded_at,
        "first_seen_at": seen_at, "last_seen_at": seen_at,
    }
    context.red_dao.insert_event(fields)
    sources = context.red_dao.list_event_sources(event_id)
    entry = {
        "event_id": event_id, "source_id": sources[0]["id"], "handle": handle,
        "shortcode": shortcode, "title": title, "original_starts_at": starts_at,
    }
    context.red_items.append(entry)
    return entry


def _last(context) -> dict:
    return context.red_items[-1]


def _row(context, entry=None) -> dict:
    entry = entry or _last(context)
    row = context.red_dao.get_event(entry["event_id"])
    assert row is not None, entry
    return row


def _set_uploaded_at(context, dt, entry=None) -> None:
    entry = entry or _last(context)
    context.red_dao.update_event(entry["event_id"], {
        "source_uploaded_at": dt,
        "source_handle": entry["handle"], "source_shortcode": entry["shortcode"],
    })


def _set_date_text(context, text, entry=None) -> None:
    entry = entry or _last(context)
    row = _row(context, entry)
    raw = dict(row.get("raw_extraction") or {})
    raw["date_text"] = text
    context.red_dao.update_event(entry["event_id"], {
        "raw_extraction": raw,
        "source_handle": entry["handle"], "source_shortcode": entry["shortcode"],
    })


def _last_decision(context):
    entry = _last(context)
    report = context.red_report
    assert report is not None, context.red_exception
    return next(d for d in report.rows if d.source_id == entry["source_id"])


def _parse_date_only(date_str: str):
    return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()


def _parse_date_to_datetime(date_str: str) -> datetime:
    d = _parse_date_only(date_str)
    return datetime(d.year, d.month, d.day, 20, 0, tzinfo=RECIFE_TZ)


def _run_repair(context, *, apply: bool, since_id=None) -> None:
    _ensure_red(context)
    context.red_exception = None
    try:
        context.red_report = run_repair(context.red_dao, apply=apply, since_id=since_id)
    except Exception as exc:  # noqa: BLE001 - captured for Then steps to inspect
        context.red_exception = exc
        context.red_report = getattr(exc, "report", None)


# ── Background ────────────────────────────────────────────────────────────────
@given("the date repair script is available")
def step_given_script_available(context):
    _ensure_red(context)


@given("every source carries the post's own upload timestamp")
def step_given_sources_carry_upload_timestamp(context):
    # Documents the DEFAULT `_insert_item` already applies (`uploaded_at`
    # defaults to `_DEFAULT_UPLOADED_AT`, never None) -- nothing further to
    # set up here. A later per-item override ("its post was uploaded on
    # ...", "...whose source has no upload timestamp") always follows this
    # step and changes ONE item, not the harness-wide default.
    _ensure_red(context)


# ── item fixtures ────────────────────────────────────────────────────────────
@given('a stored item with date text "{date_text}" and no start date')
def step_given_item_date_text_no_start(context, date_text):
    _insert_item(context, date_text=date_text, starts_at=None)


@given('a stored item with date text "{date_text}" starting on {date}')
def step_given_item_date_text_starting_on(context, date_text, date):
    _insert_item(context, date_text=date_text, starts_at=_parse_date_to_datetime(date))


@given("its post was uploaded on {date}")
def step_given_its_post_uploaded_on(context, date):
    _set_uploaded_at(context, _parse_date_to_datetime(date))


@given("a stored item with no date text and no start date")
def step_given_item_no_date_text_no_start(context):
    _insert_item(context, date_text=None, starts_at=None, review_reason="missing_date")


@given('a stored item with date text "{date_text}" whose source has no upload timestamp')
def step_given_item_no_upload_timestamp(context, date_text):
    _insert_item(context, date_text=date_text, starts_at=None, uploaded_at=None)


@given("a stored item whose start date was edited by an operator")
def step_given_item_operator_edited_start(context):
    fixed = datetime(2026, 9, 1, 20, 0, tzinfo=RECIFE_TZ)
    _insert_item(
        context, date_text="É HOJE", starts_at=fixed, status="accepted",
        operator_edited_fields=["starts_at"],
    )


@given("a confirmed item whose stored date the resolver now disagrees with")
def step_given_confirmed_item_disagrees(context):
    # Anchored on the Background's default upload timestamp (2026-08-07):
    # "É HOJE" resolves to 2026-08-07, which genuinely disagrees with the
    # confirmed row's own stored (unrelated) date below.
    fixed = datetime(2026, 9, 1, 20, 0, tzinfo=RECIFE_TZ)
    _insert_item(context, date_text="É HOJE", starts_at=fixed, status="confirmed")


@given("a superseded item whose date the resolver can now read")
def step_given_superseded_item_resolvable(context):
    _insert_item(context, date_text="É HOJE", starts_at=None, status="superseded")


@given('a stored item queued for review with reason "{reason}"')
def step_given_item_queued_with_reason(context, reason):
    _insert_item(context, date_text=None, starts_at=None, review_reason=reason)


@given('a stored item queued for review with reasons "{reason1}" and "{reason2}"')
def step_given_item_queued_with_two_reasons(context, reason1, reason2):
    _insert_item(
        context, date_text=None, starts_at=None, review_reason=f"{reason1}; {reason2}",
        venue_id=None,
    )


@given("its date text can now be resolved")
def step_given_date_text_now_resolvable(context):
    _set_date_text(context, "É HOJE")


@given("a stored item whose date text states no year")
def step_given_item_date_text_states_no_year(context):
    _insert_item(context, date_text="08/08", starts_at=None)


@given("resolving it requires rolling the year forward")
def step_given_requires_year_roll(context):
    # 2026-11-01 is more than the 60-day grace window past 08/08's own
    # candidate (2026-08-08) -- the year MUST roll to 2027, not merely keep
    # the anchor's year, so year_inferred fires unambiguously.
    _set_uploaded_at(context, datetime(2026, 11, 1, 12, 0, tzinfo=RECIFE_TZ))


@given("a stored item whose date the repair will change")
def step_given_item_date_will_change(context):
    _insert_item(context, date_text="É HOJE", starts_at=None)


@given("two sources of one post whose repaired dates give them the same content identity")
def step_given_two_sources_collide(context):
    shared_title = "Show de Sexta"
    handle, shortcode = "colisaobar", "colisao_post"
    anchor = _DEFAULT_UPLOADED_AT
    first = _insert_item(
        context, date_text="É HOJE", starts_at=None, title=shared_title,
        source_handle=handle, source_shortcode=shortcode, uploaded_at=anchor,
    )
    second = _insert_item(
        # "07/08" against the SAME 2026-08-07 anchor resolves to the exact
        # same instant "É HOJE" does (2026-08-07, midnight) -- so once both
        # are repaired they share (title, date), the content identity
        # source_event_key hashes, even though their ORIGINAL stored dates
        # (None vs 2027-01-01) kept their ORIGINAL keys apart.
        context, date_text="07/08", starts_at=datetime(2027, 1, 1, tzinfo=RECIFE_TZ),
        title=shared_title, source_handle=handle, source_shortcode=shortcode, uploaded_at=anchor,
    )
    context.red_collide_pair = (first, second)


@given("several stored items whose dates the repair would change")
def step_given_several_items_would_change(context):
    _insert_item(context, date_text="É HOJE", starts_at=None, title="Repairable A")
    _insert_item(
        context, date_text="08/08", starts_at=datetime(2027, 8, 9, tzinfo=RECIFE_TZ),
        title="Repairable B", uploaded_at=datetime(2026, 8, 12, 12, 0, tzinfo=RECIFE_TZ),
    )
    _insert_item(
        context, date_text="De 06 a 09 de fevereiro",
        starts_at=datetime(2027, 2, 9, tzinfo=RECIFE_TZ), title="Repairable C",
    )


@given("stored items with several different date text shapes")
def step_given_items_several_shapes(context):
    _insert_item(context, date_text="É HOJE", starts_at=None, title="Shape Relative")
    _insert_item(context, date_text="08/08", starts_at=None, title="Shape Numeric")
    _insert_item(
        context, date_text="De 06 a 09 de fevereiro", starts_at=None, title="Shape Range",
    )


@given("the repair has already been applied")
def step_given_repair_already_applied(context):
    _ensure_red(context)
    if not context.red_items:
        _insert_item(context, date_text="É HOJE", starts_at=None, title="Idempotent Item")
    _run_repair(context, apply=True)
    assert context.red_exception is None, context.red_exception


# ── running the repair ───────────────────────────────────────────────────────
@when("the repair runs with apply")
def step_when_repair_runs_with_apply(context):
    _run_repair(context, apply=True)


@when("the repair runs without apply")
def step_when_repair_runs_without_apply(context):
    _run_repair(context, apply=False)


@when("the repair runs with apply again")
def step_when_repair_runs_with_apply_again(context):
    _run_repair(context, apply=True)


# ── assertions ─────────────────────────────────────────────────────────────────
@then("the item starts on {date}")
def step_then_item_starts_on(context, date):
    row = _row(context)
    expected = _parse_date_only(date)
    assert row.get("starts_at") is not None, row
    assert row["starts_at"].date() == expected, (row["starts_at"], expected)


@then("the item is flagged as a collapsed range")
def step_then_item_flagged_collapsed_range(context):
    row = _row(context)
    assert "date_range" in (row.get("review_reason") or ""), row


@then("the item still has no start date")
def step_then_item_still_no_start_date(context):
    row = _row(context)
    assert row.get("starts_at") is None, row


@then('the item is still queued for review with reason "{reason}"')
def step_then_item_still_queued_review_reason(context, reason):
    row = _row(context)
    assert row.get("status") == "pending_review", row
    assert reason in (row.get("review_reason") or ""), row


@then("the report records it as skipped for a missing anchor")
def step_then_report_skipped_missing_anchor(context):
    decision = _last_decision(context)
    assert decision.action == "skipped_no_anchor", decision


@then("the item's start date is unchanged")
def step_then_item_start_date_unchanged(context):
    entry = _last(context)
    row = _row(context, entry)
    assert row.get("starts_at") == entry["original_starts_at"], (
        row.get("starts_at"), entry["original_starts_at"],
    )


@then("the report records it as skipped for an operator edit")
def step_then_report_skipped_operator_edit(context):
    decision = _last_decision(context)
    assert decision.action == "skipped_operator", decision


@then("the report lists it as a confirmed conflict")
def step_then_report_confirmed_conflict(context):
    decision = _last_decision(context)
    assert decision.action == "confirmed_conflict", decision


@then('the item is no longer queued for "{reason}"')
def step_then_item_no_longer_queued_for(context, reason):
    row = _row(context)
    assert reason not in (row.get("review_reason") or ""), row


@then('the item is still queued for "{reason}"')
def step_then_item_still_queued_for(context, reason):
    row = _row(context)
    assert reason in (row.get("review_reason") or ""), row


@then('the item is queued for review with reason "{reason}"')
def step_then_item_queued_review_reason(context, reason):
    row = _row(context)
    assert reason in (row.get("review_reason") or ""), row


@then("the source event key equals the key a fresh extraction would compute")
def step_then_key_equals_fresh_extraction(context):
    row = _row(context)
    expected = compute_source_event_key(row.get("title"), row.get("starts_at"))
    assert row.get("source_event_key") == expected, (row.get("source_event_key"), expected)


@then("the key and the start date were written in one transaction")
def step_then_key_and_date_one_transaction(context):
    # Structural: `run_repair` never issues a separate `update_event` call
    # for a source's key -- the SAME write_fields dict this run actually
    # sent carries both keys whenever the date changed, which is exactly
    # what makes them land in one `venue_dao.update_event` call (and
    # therefore one real SQL transaction; see RdsVenueStore.update_event's
    # own `engine.begin()` block).
    decision = _last_decision(context)
    assert "source_event_key" in decision.write_fields, decision.write_fields
    assert "starts_at" in decision.write_fields, decision.write_fields


@then("the repair does not raise")
def step_then_repair_does_not_raise(context):
    assert context.red_exception is None, context.red_exception


@then("the pair is reported as a merge candidate")
def step_then_pair_reported_merge_candidate(context):
    report = context.red_report
    assert report is not None, context.red_exception
    first, second = context.red_collide_pair
    ids = {first["source_id"], second["source_id"]}
    found = any({sid, owner} <= ids for sid, owner, _key in report.collisions)
    assert found, (ids, report.collisions)


@then("no item's start date changes")
def step_then_no_item_start_date_changes(context):
    for entry in context.red_items:
        row = _row(context, entry)
        assert row.get("starts_at") == entry["original_starts_at"], (entry, row)


@then("the report lists each item's stored date and resolved date")
def step_then_report_lists_stored_and_resolved(context):
    report = context.red_report
    assert report is not None, context.red_exception
    for entry in context.red_items:
        decision = next(d for d in report.rows if d.source_id == entry["source_id"])
        assert decision.old_starts_at == entry["original_starts_at"], decision
        # Every fixture in this scenario is a genuine repair candidate: the
        # resolved date must actually differ from what is stored, or the
        # scenario would not be proving the report shows BOTH values.
        assert decision.new_starts_at != decision.old_starts_at, decision


@then("the report groups the changes by date text shape")
def step_then_report_groups_by_shape(context):
    report = context.red_report
    assert report is not None, context.red_exception
    expected_shapes = {date_text_shape(entry.get("date_text")) for entry in [
        {"date_text": "É HOJE"}, {"date_text": "08/08"}, {"date_text": "De 06 a 09 de fevereiro"},
    ]}
    assert len(expected_shapes) >= 3, expected_shapes  # sanity: genuinely different shapes
    for shape in expected_shapes:
        assert shape in report.by_shape, (shape, sorted(report.by_shape))
        assert sum(report.by_shape[shape].values()) >= 1, report.by_shape[shape]


@then("no item changes")
def step_then_no_item_changes(context):
    report = context.red_report
    assert report is not None, context.red_exception
    assert report.by_disposition.get("repaired", 0) == 0, dict(report.by_disposition)
