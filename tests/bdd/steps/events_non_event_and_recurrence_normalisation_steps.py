"""Behave steps for
tests/bdd/persistence/events-non-event-and-recurrence-normalisation.feature
(plans/260907_events-non-event-and-recurrence-normalisation.md).

Reuses the SHARED harness environment.py builds per scenario — the RDS fake
(`context.rds_store`), the fakeredis-backed DAO (`context.redis_only_dao`)
and the REAL `RedisProjectionService`
(`context.redis_projection_service`) — plus
`events_serving_projection_steps.py`'s own venue/event fixtures
(`_venue_id_for`, `_insert_event`, `_now`), so a venue named here and a
venue named there are ONE row.

Three disciplines this file holds to, all of them recorded in the plan:

- **No scenario hand-seeds `events_index_v1:*` or `event_occurrence_v1:*`.**
  Every assertion below reads what the REAL projector wrote; a hand-seeded
  index goes green while production stays broken.
- **Prometheus counters are process-global**, so every counter assertion is
  a DELTA against a baseline taken before the scenario's first projection.
- **No step reaches OpenAI, S3, Apify or Instagram.** The amended `kind`
  precedence is asserted by pytest (both prompts carry the shared constant)
  and by a live eval outside this suite; here it appears only as its
  observable outcome — a row typed `menu`/`promotion` never reaches the
  projection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import parse as _parse
from behave import given, register_type, then, when  # type: ignore[import-untyped]

from app.config import settings
from app.metrics import (
    EVENTS_PROJECTION_CATEGORY_TOTAL,
    EVENTS_PROJECTION_ENDS_AT_TOTAL,
    EVENTS_PROJECTION_ERRORS_TOTAL,
    EVENTS_PROJECTION_RECURRENCE_TEXT_TOTAL,
)
from app.models.post_category import (
    ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY,
)
from app.services.event_date_resolver import RECIFE_TZ
from tests.bdd.steps.events_serving_projection_steps import (
    _insert_event,
    _now,
    _override_setting,
    _venue_id_for,
)


# ── parse types ───────────────────────────────────────────────────────────
# Constrained deliberately: without them behave's greedy default `{}` lets
# `... starting at {a} and ending at {b}` swallow the trailing `on <date>`
# of the sibling step and the two register as an ambiguous match.
@_parse.with_pattern(r"[^\"]+")
def _parse_bare(text):
    return text


@_parse.with_pattern(r"\d{2}:\d{2}")
def _parse_clock(text):
    return text


@_parse.with_pattern(r"\d{4}-\d{2}-\d{2}")
def _parse_isodate(text):
    return text


@_parse.with_pattern(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
def _parse_stamp(text):
    return text


register_type(EvBare=_parse_bare)
register_type(EvClock=_parse_clock)
register_type(EvDate=_parse_isodate)
register_type(EvStamp=_parse_stamp)


_DEFAULT_VENUE = "Cachaçaria Tradição"

_RECURRENCE_OUTCOMES = ("normalized", "unchanged", "absent")
_CATEGORY_OUTCOMES = ("canonicalized", "unchanged", "off_vocabulary", "absent")
_ENDS_AT_OUTCOMES = (
    "carried", "derived", "dropped_inverted", "dropped_zero",
    "dropped_implausible", "absent",
)
_COUNTERS = {
    "recurrence text": (EVENTS_PROJECTION_RECURRENCE_TEXT_TOTAL, _RECURRENCE_OUTCOMES),
    "category": (EVENTS_PROJECTION_CATEGORY_TOTAL, _CATEGORY_OUTCOMES),
    "end time": (EVENTS_PROJECTION_ENDS_AT_TOTAL, _ENDS_AT_OUTCOMES),
}

# The frozen `EventOccurrence` field set (plans/260905_events-serving-
# projection.md's contract table). Spelled out here rather than derived from
# the model — a derived list would agree with any change the model made and
# would prove nothing.
_CONTRACT_FIELDS = (
    "occurrence_id", "event_id", "occurrence_date", "starts_at", "ends_at",
    "time_known", "is_recurring", "recurrence_text", "title", "description",
    "category", "price_text", "ticket_info", "ticket_url", "lineup",
    "attractions", "flyer_url", "venue_id", "venue_name", "venue_neighborhood",
    "venue_lat", "venue_lng", "source_permalink", "source_handle", "city_slug",
    "status", "updated_at",
)

# A recurrence phrase this repo deliberately does not parse into weekdays
# (`event_date_resolver.weekdays_from_recurrence_text` returns None), so
# `expand_occurrences` takes the SINGLE-occurrence shape — the carried
# branch.
_UNPARSEABLE_RECURRENCE = "toda semana"
# A phrase that DOES parse, used wherever a scenario needs the re-derived
# branch but names no phrase of its own.
_PARSEABLE_RECURRENCE = "Toda quarta"


# ── metric baselines ──────────────────────────────────────────────────────
def _baseline(context) -> None:
    """Idempotent; taken by every Given that can precede a projection."""
    if hasattr(context, "evnr_baseline"):
        return
    context.evnr_baseline = {
        name: {
            outcome: counter.labels(outcome=outcome)._value.get()
            for outcome in outcomes
        }
        for name, (counter, outcomes) in _COUNTERS.items()
    }
    context.evnr_error_baseline = EVENTS_PROJECTION_ERRORS_TOTAL.labels(
        stage="event"
    )._value.get()


def _delta(context, counter_name: str, outcome: str) -> float:
    _baseline(context)
    counter, outcomes = _COUNTERS[counter_name]
    assert outcome in outcomes, f"{outcome!r} is not a declared {counter_name} outcome"
    now = counter.labels(outcome=outcome)._value.get()
    return now - context.evnr_baseline[counter_name][outcome]


# ── fixtures ──────────────────────────────────────────────────────────────
def _stale_start(context, *, hour=21, minute=0) -> datetime:
    """A deliberately STALE stored `starts_at` — 60 days before the
    scenario's clock — so a recurring row's projected days can only have
    come from its weekday PATTERN, never from this date (the discipline
    `event_occurrences`' own module docstring sets out)."""
    stale = (_now(context) - timedelta(days=60)).astimezone(RECIFE_TZ)
    return stale.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ).astimezone(timezone.utc)


def _recife(stamp: str) -> datetime:
    naive = datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=RECIFE_TZ).astimezone(timezone.utc)


def _insert(context, **fields) -> str:
    _baseline(context)
    fields.setdefault("venue_name", _DEFAULT_VENUE)
    return _insert_event(context, **fields)


def _set_last_seen(context, days: int, event_id=None) -> None:
    """Pokes the event's ONLY `event_source` row's `last_seen_at` — the
    established single-source convention in this repo's RDS fake. A
    recurring row's selectability is bounded by SOURCE FRESHNESS alone, so
    without this a recurring fixture's selection would ride on wall-clock
    luck."""
    context.rds_store.update_event(
        event_id or context.evs_last_event_id,
        {"last_seen_at": _now(context) - timedelta(days=days)},
    )


def _occurrences(context, event_id=None) -> list:
    """Every occurrence the projector actually WROTE for `event_id`, read
    back out of the city indexes it wrote — never out of a hand-seeded key."""
    event_id = event_id or context.evs_last_event_id
    found = []
    for city in context.rds_store.get_geo_fence().get("cities", []):
        for member in context.redis_only_dao.get_city_events_index(city["slug"]):
            occ = context.redis_only_dao.get_event_occurrence(member)
            if occ is not None and occ.event_id == event_id:
                found.append(occ)
    found.sort(key=lambda o: (o.occurrence_date, o.occurrence_id))
    return found


def _require_occurrences(context, event_id=None) -> list:
    occs = _occurrences(context, event_id)
    assert occs, "the projector wrote no occurrence for the scenario's event"
    _assert_expected_expansion(context, occs)
    return occs


def _assert_expected_expansion(context, occs=None) -> None:
    """Honours `that row expands to N occurrences`, which is a PRECONDITION
    the scenario states and must therefore be checked, not assumed: without
    it a "counted once per source row" assertion would pass vacuously on a
    row that expanded to exactly one occurrence."""
    expected = getattr(context, "evnr_expected_expansion", None)
    if expected is None:
        return
    occs = _occurrences(context) if occs is None else occs
    assert len(occs) == expected, (
        f"the scenario states the row expands to {expected} occurrences; "
        f"the projector wrote {len(occs)}"
    )


# ══════════════════════════════════════════════════════════════════════════
# Background
# ══════════════════════════════════════════════════════════════════════════
@given('a geo-fence city "{slug:EvBare}" is configured')
def step_given_geofence_city(context, slug):
    _baseline(context)
    _override_setting(context, "events_projection_enabled", True)
    fence = context.rds_store.get_geo_fence()
    slugs = {city["slug"] for city in fence.get("cities", [])}
    assert slug in slugs, (
        f"{slug!r} is not in the harness's default geo-fence: {sorted(slugs)}"
    )
    context.evnr_city_slug = slug


@given("the events projection horizon is {days:d} days")
def step_given_horizon_days(context, days):
    _override_setting(context, "events_projection_horizon_days", days)


@given("the current Recife local time is {stamp:EvStamp}")
def step_given_current_local_time(context, stamp):
    context.evs_now = _recife(stamp)


# ══════════════════════════════════════════════════════════════════════════
# The non-event class
# ══════════════════════════════════════════════════════════════════════════
@given('an accepted event row for venue "{venue:EvBare}" titled "{title:EvBare}"')
def step_given_event_row_for_venue_titled(context, venue, title):
    _insert(
        context, venue_name=venue, title=title,
        starts_at=_now(context) + timedelta(hours=2), time_known=True,
    )


@given('that row has post_type "{post_type:EvBare}"')
def step_given_row_post_type(context, post_type):
    context.rds_store.update_event(
        context.evs_last_event_id, {"post_type": post_type}
    )


@given('that row has category "{category:EvBare}"')
def step_given_row_category(context, category):
    context.rds_store.update_event(
        context.evs_last_event_id, {"category": category}
    )


@given('that row is recurring with recurrence text "{text:EvBare}"')
def step_given_row_recurring_with_text(context, text):
    """Sets the cadence and NOTHING else. Deliberately leaves the row's own
    `starts_at`/`ends_at` alone: a scenario that stated a stored pair
    (`... starting at 22:00 and ending at 23:00 on 2026-08-12`) is asserting
    on that exact pair, and expansion re-derives an occurrence's DAY from
    the weekday pattern regardless of what date the stored `starts_at`
    carries."""
    context.rds_store.update_event(context.evs_last_event_id, {
        "is_recurring": True,
        "recurrence_text": text,
    })


@given("that row was last seen {days:d} days ago")
def step_given_row_last_seen(context, days):
    _set_last_seen(context, days)


@given('a recurring event row titled "{title:EvBare}" with post_type "{post_type:EvBare}"')
def step_given_recurring_row_titled_with_post_type(context, title, post_type):
    _insert(
        context, title=title, post_type=post_type, is_recurring=True,
        recurrence_text=_PARSEABLE_RECURRENCE, starts_at=_stale_start(context),
        time_known=True,
    )
    _set_last_seen(context, 3)


@given('an accepted recurring event row titled "{title:EvBare}" with post_type "{post_type:EvBare}"')
def step_given_accepted_recurring_row_titled_with_post_type(context, title, post_type):
    # "Terça a Domingo" is the live buffet row's OWN recurrence phrase, and
    # `weekdays_from_recurrence_text` reads it as {Tuesday, Sunday} — which
    # is exactly why that row contributes 6 occurrences to a 22-day window,
    # the number the scenario asserts.
    _insert(
        context, title=title, post_type=post_type, is_recurring=True,
        recurrence_text="Terça a Domingo", starts_at=_stale_start(context),
        time_known=True,
    )
    _set_last_seen(context, 3)


@given("an accepted recurring event row whose post_type an operator set to \"{post_type:EvBare}\"")
def step_given_recurring_row_operator_set_post_type(context, post_type):
    _insert(
        context, is_recurring=True, recurrence_text=_PARSEABLE_RECURRENCE,
        starts_at=_stale_start(context), time_known=True,
    )
    _set_last_seen(context, 3)
    context.rds_store.update_event(
        context.evs_last_event_id, {"post_type": post_type}
    )


@given("the events projection has already run")
def step_given_projection_has_already_run(context):
    _baseline(context)
    context.evs_summary = context.redis_projection_service.project_events(
        now=_now(context)
    )


@when('an operator sets that event\'s post_type to "{post_type:EvBare}"')
def step_when_operator_sets_post_type(context, post_type):
    context.rds_store.update_event(
        context.evs_last_event_id, {"post_type": post_type}
    )


@given("no occurrence key exists for that event")
@then("no occurrence key exists for that event")
def step_no_occurrence_key(context):
    occs = _occurrences(context)
    assert not occs, [o.occurrence_id for o in occs]
    assert context.redis_only_dao.get_event_occurrence(
        context.evs_last_event_id
    ) is None


@then("an occurrence key exists for that event")
def step_then_occurrence_key_exists(context):
    assert _occurrences(context), "no occurrence key written for that event"


@then('the city events index for "{slug:EvBare}" holds no occurrence of that event')
def step_then_city_index_holds_none(context, slug):
    members = context.redis_only_dao.get_city_events_index(slug)
    event_id = context.evs_last_event_id
    stray = [
        m for m in members
        if (occ := context.redis_only_dao.get_event_occurrence(m)) is not None
        and occ.event_id == event_id
    ]
    assert not stray, stray


@then('the city events index for "{slug:EvBare}" holds at least one occurrence of that event')
def step_then_city_index_holds_some(context, slug):
    members = context.redis_only_dao.get_city_events_index(slug)
    event_id = context.evs_last_event_id
    hits = [
        m for m in members
        if (occ := context.redis_only_dao.get_event_occurrence(m)) is not None
        and occ.event_id == event_id
    ]
    assert hits, f"{slug} index holds no occurrence of {event_id}"


@then('the city events index for "{slug:EvBare}" holds {count:d} occurrences of that event')
def step_then_city_index_holds_count(context, slug, count):
    members = context.redis_only_dao.get_city_events_index(slug)
    event_id = context.evs_last_event_id
    hits = [
        m for m in members
        if (occ := context.redis_only_dao.get_event_occurrence(m)) is not None
        and occ.event_id == event_id
    ]
    assert len(hits) == count, (len(hits), count, hits)


@given('the city events index for "{slug:EvBare}" holds {count:d} occurrences of that event')
def step_given_city_index_holds_count(context, slug, count):
    step_then_city_index_holds_count(context, slug, count)


@then('the venue events index for "{venue:EvBare}" holds no occurrence of that event')
def step_then_venue_index_holds_none(context, venue):
    venue_id = context.evs_venue_ids[venue]
    _assert_venue_index_empty_of_event(context, venue_id)


@then("the venue events index for that event's venue holds no occurrence of it")
def step_then_that_venues_index_holds_none(context):
    row = context.rds_store.get_event(context.evs_last_event_id)
    _assert_venue_index_empty_of_event(context, row["venue_id"])


def _assert_venue_index_empty_of_event(context, venue_id: str) -> None:
    event_id = context.evs_last_event_id
    stray = [
        m for m in context.redis_only_dao.get_venue_events_index(venue_id)
        if (occ := context.redis_only_dao.get_event_occurrence(m)) is not None
        and occ.event_id == event_id
    ]
    assert not stray, stray


@then('the event row still exists in the system of record with post_type "{post_type:EvBare}"')
def step_then_row_still_exists_with_post_type(context, post_type):
    row = context.rds_store.get_event(context.evs_last_event_id)
    assert row is not None, "the row was deleted; this repo reclassifies, never deletes"
    assert row.get("post_type") == post_type, row.get("post_type")


# ══════════════════════════════════════════════════════════════════════════
# recurrence_text
# ══════════════════════════════════════════════════════════════════════════
@given('an accepted recurring event row with recurrence text "{text:EvBare}"')
def step_given_recurring_row_with_recurrence_text(context, text):
    _insert(
        context, is_recurring=True, recurrence_text=text,
        starts_at=_stale_start(context, hour=22), time_known=True,
    )
    _set_last_seen(context, 3)


@given('an accepted recurring event row "{name:EvBare}" with recurrence text "{text:EvBare}"')
def step_given_named_recurring_row_with_recurrence_text(context, name, text):
    event_id = _insert(
        context, is_recurring=True, recurrence_text=text,
        starts_at=_stale_start(context, hour=22), time_known=True,
    )
    if not hasattr(context, "evnr_named_events"):
        context.evnr_named_events = {}
    context.evnr_named_events[name] = event_id


@given("both rows were last seen {days:d} days ago")
def step_given_both_rows_last_seen(context, days):
    for event_id in context.evnr_named_events.values():
        _set_last_seen(context, days, event_id=event_id)


@given("an accepted non-recurring event row with no recurrence text")
def step_given_non_recurring_row_no_recurrence_text(context):
    _insert(
        context, starts_at=_now(context) + timedelta(hours=2), time_known=True,
    )


@then('every projected occurrence of that event carries recurrence text "{text:EvBare}"')
def step_then_every_occurrence_recurrence_text(context, text):
    for occ in _require_occurrences(context):
        assert occ.recurrence_text == text, (occ.occurrence_id, occ.recurrence_text)


@then("every projected occurrence of that event carries a null recurrence text")
def step_then_every_occurrence_null_recurrence_text(context):
    for occ in _require_occurrences(context):
        assert occ.recurrence_text is None, (occ.occurrence_id, occ.recurrence_text)


@then('event "{name:EvBare}" is projected with recurrence text "{text:EvBare}"')
def step_then_named_event_recurrence_text(context, name, text):
    event_id = context.evnr_named_events[name]
    occs = _occurrences(context, event_id)
    assert occs, f"no occurrence projected for event {name}"
    for occ in occs:
        assert occ.recurrence_text == text, (name, occ.recurrence_text)
    if not hasattr(context, "evnr_named_projections"):
        context.evnr_named_projections = {}
    context.evnr_named_projections[name] = occs[0].recurrence_text


@then("the two projected recurrence texts are different")
def step_then_two_recurrence_texts_differ(context):
    values = list(context.evnr_named_projections.values())
    assert len(values) == 2, values
    assert values[0] != values[1], values


@then('the event row in the system of record still has recurrence text "{text:EvBare}"')
def step_then_rds_recurrence_text_verbatim(context, text):
    row = context.rds_store.get_event(context.evs_last_event_id)
    assert row.get("recurrence_text") == text, row.get("recurrence_text")


# ══════════════════════════════════════════════════════════════════════════
# category
# ══════════════════════════════════════════════════════════════════════════
@given('the admin post category vocabulary is configured as "{labels:EvBare}"')
def step_given_vocabulary_configured(context, labels):
    import json

    entries = [label.strip() for label in labels.split(",") if label.strip()]
    context.fake_redis.set(
        ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY, json.dumps(entries)
    )


@given("no admin post category vocabulary is configured")
def step_given_no_vocabulary_configured(context):
    context.fake_redis.delete(ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY)


@given("the admin post category vocabulary read fails")
def step_given_vocabulary_read_fails(context):
    """Makes the REAL admin-config read raise for THIS key only, leaving
    every other Redis read the projector performs untouched — a blanket
    outage would prove the cycle survived a different failure than the one
    the scenario names."""
    client = context.redis_only_dao.client
    original_get = client.get

    def _failing_get(key, *args, **kwargs):
        if key == ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY:
            raise RuntimeError("admin config read failed")
        return original_get(key, *args, **kwargs)

    client.get = _failing_get
    context.evnr_restore_get = (client, original_get)


@given('an accepted event row with category "{category:EvBare}"')
def step_given_event_row_with_category(context, category):
    _insert(
        context, category=category,
        starts_at=_now(context) + timedelta(hours=2), time_known=True,
    )


@given("an accepted event row with no category")
def step_given_event_row_no_category(context):
    _insert(context, starts_at=_now(context) + timedelta(hours=2), time_known=True)


@then('every projected occurrence of that event carries category "{category:EvBare}"')
def step_then_every_occurrence_category(context, category):
    for occ in _require_occurrences(context):
        assert occ.category == category, (occ.occurrence_id, occ.category)


@then("every projected occurrence of that event carries a null category")
def step_then_every_occurrence_null_category(context):
    for occ in _require_occurrences(context):
        assert occ.category is None, (occ.occurrence_id, occ.category)


@then("every projected occurrence of that event is still written")
def step_then_every_occurrence_still_written(context):
    assert _require_occurrences(context)


@then("the projection reports no event-stage errors")
def step_then_no_event_stage_errors(context):
    _baseline(context)
    now = EVENTS_PROJECTION_ERRORS_TOTAL.labels(stage="event")._value.get()
    assert now == context.evnr_error_baseline, (
        now, context.evnr_error_baseline, context.evs_summary
    )
    assert context.evs_summary["errors"] == 0, context.evs_summary


@then('the {counter:EvBare} outcome counter records outcome "{outcome:EvBare}"')
def step_then_counter_records_outcome(context, counter, outcome):
    assert counter in _COUNTERS, f"unrecognised counter: {counter!r}"
    _assert_expected_expansion(context)
    delta = _delta(context, counter, outcome)
    assert delta >= 1, f"{counter} outcome {outcome!r}: expected >= 1, got {delta}"


@then('it does not record outcome "{outcome:EvBare}" for that row')
def step_then_category_counter_does_not_record(context, outcome):
    delta = _delta(context, "category", outcome)
    assert delta == 0, f"category outcome {outcome!r}: expected 0, got {delta}"


# ══════════════════════════════════════════════════════════════════════════
# ends_at
# ══════════════════════════════════════════════════════════════════════════
@given("an accepted recurring event row starting at {start:EvClock} and ending at {end:EvClock} on {day:EvDate}")
def step_given_recurring_row_clock_pair_on_date(context, start, end, day):
    _insert(
        context, is_recurring=True,
        starts_at=_recife(f"{day} {start}"), ends_at=_recife(f"{day} {end}"),
        time_known=True,
    )


@given("an accepted recurring event row starting at {start:EvStamp} and ending at {end:EvStamp}")
def step_given_recurring_row_stamp_pair(context, start, end):
    # A PARSEABLE recurrence phrase on purpose: this row must reach the
    # RE-DERIVED branch, which is where an inverted stored pair is the live
    # defect. The carried branch's own inverted case is a separate scenario.
    _insert(
        context, is_recurring=True, recurrence_text=_PARSEABLE_RECURRENCE,
        starts_at=_recife(start), ends_at=_recife(end), time_known=True,
    )


@given("that row's stored end equals its stored start")
def step_given_stored_end_equals_start(context):
    row = context.rds_store.get_event(context.evs_last_event_id)
    context.rds_store.update_event(
        context.evs_last_event_id, {"ends_at": row["starts_at"]}
    )


@given("an accepted recurring event row whose stored end is {hours:d} hours after its stored start")
def step_given_recurring_row_end_hours_after(context, hours):
    starts_at = _stale_start(context, hour=22)
    _insert(
        context, is_recurring=True, recurrence_text=_PARSEABLE_RECURRENCE,
        starts_at=starts_at, ends_at=starts_at + timedelta(hours=hours),
        time_known=True,
    )


@given("an accepted recurring event row with no stored end time")
def step_given_recurring_row_no_stored_end(context):
    _insert(
        context, is_recurring=True, recurrence_text=_PARSEABLE_RECURRENCE,
        starts_at=_stale_start(context, hour=22), time_known=True,
    )


@given("an accepted non-recurring event row starting at {start:EvStamp} and ending at {end:EvStamp}")
def step_given_non_recurring_row_stamp_pair(context, start, end):
    _insert(
        context, starts_at=_recife(start), ends_at=_recife(end), time_known=True,
    )


@given("an accepted non-recurring event row whose stored end equals its stored start")
def step_given_non_recurring_row_zero_length(context):
    starts_at = _now(context) + timedelta(hours=2)
    _insert(context, starts_at=starts_at, ends_at=starts_at, time_known=True)


@given('an accepted event row that is recurring with recurrence text "{text:EvBare}"')
def step_given_event_row_recurring_with_text(context, text):
    _insert(
        context, is_recurring=True, recurrence_text=text,
        starts_at=_stale_start(context, hour=22), time_known=True,
    )


@given("that row starts at {start:EvStamp} and ends at {end:EvStamp}")
def step_given_row_starts_and_ends(context, start, end):
    context.rds_store.update_event(context.evs_last_event_id, {
        "starts_at": _recife(start), "ends_at": _recife(end),
    })


@then("every projected occurrence of that event ends exactly {hours:d} hour after its own start")
def step_then_every_occurrence_ends_hours_after_start(context, hours):
    occs = _require_occurrences(context)
    for occ in occs:
        assert occ.ends_at is not None, occ.occurrence_id
        assert occ.ends_at - occ.starts_at == timedelta(hours=hours), (
            occ.occurrence_id, occ.starts_at, occ.ends_at
        )


@then("no projected occurrence of that event ends before its own start")
def step_then_no_occurrence_ends_before_start(context):
    for occ in _require_occurrences(context):
        if occ.ends_at is not None:
            assert occ.ends_at >= occ.starts_at, (
                occ.occurrence_id, occ.starts_at, occ.ends_at
            )


@then("every projected occurrence of that event carries a null end time")
def step_then_every_occurrence_null_end(context):
    for occ in _require_occurrences(context):
        assert occ.ends_at is None, (occ.occurrence_id, occ.ends_at)


@then("the projected occurrence of that event carries a null end time")
def step_then_the_occurrence_null_end(context):
    occs = _require_occurrences(context)
    assert len(occs) == 1, [o.occurrence_id for o in occs]
    assert occs[0].ends_at is None, (occs[0].occurrence_id, occs[0].ends_at)


@then("the projected occurrence of that event ends at {end:EvStamp}")
def step_then_the_occurrence_ends_at(context, end):
    occs = _require_occurrences(context)
    assert len(occs) == 1, [o.occurrence_id for o in occs]
    assert occs[0].ends_at == _recife(end), occs[0].ends_at


@then("the projected occurrence of that event carries an end time equal to its start")
def step_then_occurrence_end_equals_start(context):
    occs = _require_occurrences(context)
    assert len(occs) == 1, [o.occurrence_id for o in occs]
    assert occs[0].ends_at == occs[0].starts_at, (occs[0].starts_at, occs[0].ends_at)
    context.evnr_asserted_end = occs[0].ends_at


@then("that end time is not null")
def step_then_that_end_time_not_null(context):
    assert context.evnr_asserted_end is not None


@then("that event is projected as exactly one occurrence")
def step_then_exactly_one_occurrence(context):
    occs = _require_occurrences(context)
    assert len(occs) == 1, [o.occurrence_id for o in occs]
    context.evnr_single = occs[0]


@then("that occurrence's id is the bare event id")
def step_then_occurrence_id_is_bare(context):
    occ = context.evnr_single
    assert occ.occurrence_id == occ.event_id, occ.occurrence_id


@then("that occurrence starts at {start:EvStamp}")
def step_then_occurrence_starts_at(context, start):
    assert context.evnr_single.starts_at == _recife(start), (
        context.evnr_single.starts_at
    )


@then("that occurrence ends at {end:EvStamp}")
def step_then_occurrence_ends_at(context, end):
    occ = getattr(context, "evnr_single", None)
    if occ is None:
        occs = _require_occurrences(context)
        assert len(occs) == 1, [o.occurrence_id for o in occs]
        occ = context.evnr_single = occs[0]
    assert occ.ends_at == _recife(end), occ.ends_at


@then("that occurrence's end time is more than {hours:d} hours after its start")
def step_then_occurrence_end_more_than_hours(context, hours):
    occ = context.evnr_single
    assert occ.ends_at is not None
    assert occ.ends_at - occ.starts_at > timedelta(hours=hours), (
        occ.starts_at, occ.ends_at
    )


# ── the whole-cycle, exhaustive assertions ────────────────────────────────
# The production census of 2026-09-07: 21 selected rows over the shapes the
# plan measured — the nine distinct recurrence phrases, the four rows whose
# stored `ends_at` is inverted against their own stored `starts_at`, a
# non-recurring row, and (added by its own step) the recurring row whose
# recurrence prose this repo cannot parse.
_CENSUS_ROWS = (
    # (title, recurrence_text, is_recurring, start_hour, end_offset_hours)
    ("Buffet de Terça a Domingo", "Terça a Domingo", True, 11, -1296),
    ("Aula de FORRÓ (iniciados)", "Toda QUARTA", True, 19, -600),
    ("Aula de FORRÓ (iniciantes)", "Toda QUARTA", True, 20, -168),
    ("Residência drag da Metrópole", "todos os sábados", True, 0, -504),
    ("Sambinha Downtown", "TODOS OS DOMINGOS", True, 16, None),
    ("Lovezinho", "Toda sexta, às 21 horas", True, 21, None),
    ("FORRÓ DOS PAIS", "Aos domingos", True, 20, None),
    ("Drag Ataque", "toda quarta", True, 22, None),
    ("Baile Dançante", "TODOS OS SÁBADOS", True, 21, None),
    ("QUINTA É DIA DE HAPPY HOUR", "Toda quinta", True, 22, None),
    ("Oktoberfest BeerDock", None, False, 18, 5),
    ("FERIADÃO METRÓPOLE", None, False, 0, None),
    ("37ª REFENO", None, False, 19, None),
)


@given("the projection source holds every event row from the production census")
def step_given_census_rows(context):
    _baseline(context)
    context.evnr_census_ids = []
    for title, recurrence, is_recurring, hour, end_offset in _CENSUS_ROWS:
        if is_recurring:
            starts_at = _stale_start(context, hour=hour)
        else:
            starts_at = (_now(context) + timedelta(days=5)).astimezone(
                RECIFE_TZ
            ).replace(hour=hour, minute=0, second=0, microsecond=0).astimezone(
                timezone.utc
            )
        ends_at = (
            starts_at + timedelta(hours=end_offset) if end_offset is not None
            else None
        )
        event_id = _insert(
            context, title=title, is_recurring=is_recurring,
            recurrence_text=recurrence, starts_at=starts_at, ends_at=ends_at,
            time_known=True,
        )
        _set_last_seen(context, 3, event_id=event_id)
        context.evnr_census_ids.append(event_id)


@given("one of those rows is recurring with recurrence text this repo cannot parse")
def step_given_census_holds_unparseable_row(context):
    event_id = _insert(
        context, title="Festival de quinta a domingo", is_recurring=True,
        recurrence_text=_UNPARSEABLE_RECURRENCE,
        starts_at=_recife("2026-10-01 18:00"), ends_at=_recife("2026-10-04 04:00"),
        time_known=True,
    )
    _set_last_seen(context, 3, event_id=event_id)
    context.evnr_census_ids.append(event_id)


def _all_written_occurrences(context) -> list:
    occs = []
    for city in context.rds_store.get_geo_fence().get("cities", []):
        for member in context.redis_only_dao.get_city_events_index(city["slug"]):
            occ = context.redis_only_dao.get_event_occurrence(member)
            if occ is not None:
                occs.append(occ)
    return occs


@then("no occurrence written by that cycle has an end time before its own start time")
def step_then_no_written_occurrence_inverted(context):
    occs = _all_written_occurrences(context)
    assert occs, "the cycle wrote no occurrences at all"
    bad = [
        (o.occurrence_id, o.starts_at, o.ends_at) for o in occs
        if o.ends_at is not None and o.starts_at is not None
        and o.ends_at < o.starts_at
    ]
    assert not bad, bad


@then("every occurrence whose id is a bare event id carries its row's stored end time unchanged")
def step_then_carried_occurrences_keep_stored_end(context):
    occs = [o for o in _all_written_occurrences(context) if o.occurrence_id == o.event_id]
    assert occs, "the cycle wrote no carried-branch occurrence at all"
    checked = 0
    for occ in occs:
        row = context.rds_store.get_event(occ.event_id)
        stored_end = row.get("ends_at")
        if stored_end is None:
            assert occ.ends_at is None, (occ.occurrence_id, occ.ends_at)
            continue
        stored_start = row.get("starts_at")
        if stored_start is not None and stored_end < stored_start:
            # The one exception the contract names: cs-server never emits an
            # occurrence that ends before it starts, on either branch.
            assert occ.ends_at is None, (occ.occurrence_id, occ.ends_at)
            continue
        assert occ.ends_at == stored_end, (occ.occurrence_id, occ.ends_at, stored_end)
        checked += 1
    assert checked, "no carried-branch occurrence carried a stored end time"


# ══════════════════════════════════════════════════════════════════════════
# the pinned contract
# ══════════════════════════════════════════════════════════════════════════
@given("an accepted recurring event row with every contract field populated")
def step_given_row_with_every_contract_field(context):
    _insert(
        context, is_recurring=True, recurrence_text="Toda QUARTA",
        starts_at=_stale_start(context, hour=22),
        ends_at=_stale_start(context, hour=22) + timedelta(hours=3),
        time_known=True, title="Contrato completo",
        description="Uma noite de teste com todos os campos preenchidos.",
        category="forró", price_text="R$30 antecipado",
        ticket_info="Link na bio", ticket_url="evenyx.com/lote2",
        lineup=["DJ Teste"],
        attractions=[{"name": "DJ Teste", "type": "dj", "stage": None, "styles": ["forro"]}],
        flyer_url="https://media.vibesense.example/flyers/test.jpg",
    )


@then("every projected occurrence carries exactly the pinned occurrence field set")
def step_then_pinned_field_set(context):
    for occ in _require_occurrences(context):
        assert tuple(occ.model_dump().keys()) == _CONTRACT_FIELDS, (
            sorted(set(occ.model_dump()) ^ set(_CONTRACT_FIELDS))
        )


@then("no field has been added, removed or renamed")
def step_then_no_field_added_removed_renamed(context):
    from app.models.event_occurrence import EventOccurrence

    assert tuple(EventOccurrence.model_fields.keys()) == _CONTRACT_FIELDS, (
        sorted(set(EventOccurrence.model_fields) ^ set(_CONTRACT_FIELDS))
    )


# ══════════════════════════════════════════════════════════════════════════
# observability
# ══════════════════════════════════════════════════════════════════════════
@given('an accepted recurring event row with recurrence text "{text:EvBare}" and category "{category:EvBare}"')
def step_given_recurring_row_with_text_and_category(context, text, category):
    _insert(
        context, is_recurring=True, recurrence_text=text, category=category,
        starts_at=_stale_start(context, hour=22), time_known=True,
    )
    _set_last_seen(context, 3)


@given("that row expands to {count:d} occurrences")
def step_given_row_expands_to(context, count):
    """Widens the horizon until the scenario's own row yields exactly
    `count` occurrences — asserted after the cycle by
    `_assert_expected_expansion`, never assumed."""
    row = context.rds_store.get_event(context.evs_last_event_id)
    from app.services.event_occurrences import expand_occurrences

    for horizon in range(1, 400):
        occs = expand_occurrences(
            row, horizon_days=horizon, reference_time=_now(context)
        )
        if len(occs) == count:
            _override_setting(context, "events_projection_horizon_days", horizon)
            context.evnr_expected_expansion = count
            return
        if len(occs) > count:
            break
    raise AssertionError(
        f"no horizon makes {row.get('recurrence_text')!r} expand to exactly "
        f"{count} occurrences"
    )


@then('the {counter:EvBare} outcome counter records exactly {count:d} observation for that cycle')
def step_then_counter_records_exactly(context, counter, count):
    _assert_expected_expansion(context)
    total = sum(_delta(context, counter, outcome) for outcome in _COUNTERS[counter][1])
    assert total == count, f"{counter}: expected {count} observation(s), got {total}"


# ── the outcome outline's fixture dispatch ────────────────────────────────
def _case_recurrence_normalized(context):
    _insert(
        context, is_recurring=True, recurrence_text="Toda QUARTA",
        starts_at=_stale_start(context, hour=22), time_known=True,
    )
    _set_last_seen(context, 3)


def _case_recurrence_unchanged(context):
    _insert(
        context, is_recurring=True, recurrence_text="Toda quarta",
        starts_at=_stale_start(context, hour=22), time_known=True,
    )
    _set_last_seen(context, 3)


def _case_recurrence_absent(context):
    _insert(context, starts_at=_now(context) + timedelta(hours=2), time_known=True)


def _configure_vocabulary(context, *labels):
    import json

    context.fake_redis.set(
        ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY, json.dumps(list(labels))
    )


def _case_category_canonicalized(context):
    _configure_vocabulary(context, "Forró", "Party", "Live Music")
    _insert(
        context, category="forró",
        starts_at=_now(context) + timedelta(hours=2), time_known=True,
    )


def _case_category_unchanged(context):
    _configure_vocabulary(context, "Forró", "Party", "Live Music")
    _insert(
        context, category="Forró",
        starts_at=_now(context) + timedelta(hours=2), time_known=True,
    )


def _case_category_off_vocabulary(context):
    _configure_vocabulary(context, "Forró", "Party", "Live Music")
    _insert(
        context, category="brega",
        starts_at=_now(context) + timedelta(hours=2), time_known=True,
    )


def _case_category_absent(context):
    _insert(context, starts_at=_now(context) + timedelta(hours=2), time_known=True)


def _derived_row(context, *, end_offset_hours):
    starts_at = _stale_start(context, hour=22)
    ends_at = (
        starts_at + timedelta(hours=end_offset_hours)
        if end_offset_hours is not None else None
    )
    _insert(
        context, is_recurring=True, recurrence_text=_PARSEABLE_RECURRENCE,
        starts_at=starts_at, ends_at=ends_at, time_known=True,
    )
    _set_last_seen(context, 3)


def _carried_row(context, *, end_offset_hours):
    starts_at = _now(context) + timedelta(hours=2)
    ends_at = (
        starts_at + timedelta(hours=end_offset_hours)
        if end_offset_hours is not None else None
    )
    _insert(context, starts_at=starts_at, ends_at=ends_at, time_known=True)


_CASES = {
    "a recurrence phrase changed by normalising": _case_recurrence_normalized,
    "a recurrence phrase already normalised": _case_recurrence_unchanged,
    "no recurrence phrase": _case_recurrence_absent,
    "a category matching the live vocabulary": _case_category_canonicalized,
    "a category already in the vocabulary spelling": _case_category_unchanged,
    "a category off the live vocabulary": _case_category_off_vocabulary,
    "no category": _case_category_absent,
    "a re-derived occurrence with a usable duration":
        lambda c: _derived_row(c, end_offset_hours=1),
    "a re-derived occurrence with an inverted pair":
        lambda c: _derived_row(c, end_offset_hours=-3),
    "a re-derived occurrence with a zero duration":
        lambda c: _derived_row(c, end_offset_hours=0),
    "a re-derived occurrence with a 30 hour duration":
        lambda c: _derived_row(c, end_offset_hours=30),
    "a carried occurrence with a stored end":
        lambda c: _carried_row(c, end_offset_hours=5),
    "a carried occurrence with a 3 day stored end":
        lambda c: _carried_row(c, end_offset_hours=72),
    "a carried occurrence with an inverted pair":
        lambda c: _carried_row(c, end_offset_hours=-3),
    "a row with no stored end":
        lambda c: _carried_row(c, end_offset_hours=None),
}


@given('an accepted event row matching "{case:EvBare}"')
def step_given_event_row_matching_case(context, case):
    _baseline(context)
    builder = _CASES.get(case)
    assert builder is not None, f"unrecognised outline case: {case!r}"
    builder(context)


@then('the "{counter:EvBare}" counter records outcome "{outcome:EvBare}"')
def step_then_named_counter_records_outcome(context, counter, outcome):
    assert counter in _COUNTERS, f"unrecognised counter: {counter!r}"
    delta = _delta(context, counter, outcome)
    assert delta >= 1, f"{counter} outcome {outcome!r}: expected >= 1, got {delta}"
