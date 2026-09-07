"""Behave steps for
tests/bdd/persistence/events-venue-bairro-and-ticket-url.feature
(plans/260906_events-venue-bairro-and-ticket-url.md).

Reuses the SHARED harness environment.py builds per scenario — the RDS fake
(`context.rds_store`), the repository hop the service actually calls
(`context.repository`), the fakeredis-backed DAO
(`context.redis_only_dao`), the REAL `RedisProjectionService`
(`context.redis_projection_service`) and the REAL
`VenueAddressBackfillService` (`context.venue_address_backfill_service`).

Two disciplines this file holds to, both of them review findings the plan
records:

- **No scenario hand-seeds `events_index_v1:*` or `event_occurrence_v1:*`.**
  Every index/payload assertion below reads what the real projector wrote
  (F01) — a hand-seeded index is exactly what would go green while
  production stayed broken.
- **The backfill's `mode` travels through the REPOSITORY**, never straight
  into the store (F17): `venue_address_backfill_service.py:183` calls
  `self.venue_repository.list_address_backfill_candidates(...)`, so a test
  that skipped that hop would pass while production still selected the
  wrong population.

Venue identity is shared with `events_serving_projection_steps.py` via its
own `_venue_id_for` memo (`context.evs_venue_ids`), so the Background's
`"Downtown Beer Garden" is a servable venue in Recife` and this file's
bairro steps address the SAME venue row.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import parse as _parse
import redis
from behave import given, register_type, then, when  # type: ignore[import-untyped]
from unittest.mock import AsyncMock

from app.metrics import (
    EVENTS_PROJECTION_NIGHTLIFE_ROLLBACK_TOTAL,
    EVENTS_PROJECTION_TICKET_URL_TOTAL,
    VENUE_ADDRESS_GAUGES_REFRESHED_TIMESTAMP_SECONDS,
    VENUE_ADDRESS_NEIGHBORHOOD_EQUALS_CITY,
    VENUE_ADDRESS_SOURCE_ROWS,
    VENUE_GEOCODING_REQUESTS_TOTAL,
)
from app.models.event_occurrence import EventOccurrence
from app.models.venue import Venue
from app.models.vibe_attributes import VibeAttributes
from app.services.event_date_resolver import RECIFE_TZ
from app.services.venue_address_backfill_service import (
    ADMIN_CONFIG_GEOCODING_ENABLED_KEY,
)
from tests.bdd.steps.events_serving_projection_steps import (
    _insert_event,
    _now,
    _parse_recife,
    _venue_id_for,
)

@_parse.with_pattern(r'[^"]+')
def _parse_no_quote(text):
    """A quoted field that stops at the next double quote. Without it,
    behave's default greedy `{}` makes
    `a servable venue whose stored city is "{city}"` swallow the whole of
    `"Recife" sourced "google"` and the two steps register as ambiguous."""
    return text


register_type(NoQuote=_parse_no_quote)


_DEFAULT_VENUE = "Downtown Beer Garden"
_TICKET_OUTCOMES = ("absent", "passthrough", "scheme_added", "rejected")
_ADDRESS_SOURCES = ("operator", "google", "parsed", "none")
_ADDRESS_FIELDS = ("street", "neighborhood", "city", "postal_code")

# The frozen `EventOccurrence` field set (plans/260905_events-serving-
# backend.md's contract table). This plan is forbidden from adding,
# removing, renaming or retyping any of them, so the list is spelled out
# here rather than derived from the model — a derived list would agree with
# any change the model made and prove nothing.
_CONTRACT_FIELDS = (
    "occurrence_id", "event_id", "occurrence_date", "starts_at", "ends_at",
    "time_known", "is_recurring", "recurrence_text", "title", "description",
    "category", "price_text", "ticket_info", "ticket_url", "lineup",
    "attractions", "flyer_url", "venue_id", "venue_name", "venue_neighborhood",
    "venue_lat", "venue_lng", "source_permalink", "source_handle", "city_slug",
    "status", "updated_at",
)


# ── metric baselines ──────────────────────────────────────────────────────
# Prometheus counters are process-global and shared across scenarios, so
# every "counted once" assertion is measured against a baseline taken
# BEFORE the first projection of this scenario. Every Given in this file
# that can precede a projection takes it (idempotently).
def _baseline(context) -> None:
    if hasattr(context, "evbt_ticket_baseline"):
        return
    context.evbt_ticket_baseline = {
        outcome: EVENTS_PROJECTION_TICKET_URL_TOTAL.labels(outcome=outcome)._value.get()
        for outcome in _TICKET_OUTCOMES
    }
    context.evbt_rollback_baseline = EVENTS_PROJECTION_NIGHTLIFE_ROLLBACK_TOTAL._value.get()


def _ticket_delta(context, outcome: str) -> float:
    _baseline(context)
    now = EVENTS_PROJECTION_TICKET_URL_TOTAL.labels(outcome=outcome)._value.get()
    return now - context.evbt_ticket_baseline[outcome]


def _address_row(context, venue_id: str) -> dict:
    addr = context.rds_store.get_address(venue_id)
    assert addr is not None, f"no venues.address row for {venue_id}"
    return addr


def _current_venue_id(context) -> str:
    assert getattr(context, "evbt_current_venue_id", None), (
        "no 'that venue' in scope — a Given must name one first"
    )
    return context.evbt_current_venue_id


def _make_venue(context, name: str, *, address: str = "R. Test, 1 - Recife PE 50000-000 Brazil") -> str:
    """A servable Recife venue created through the repository (the
    production write path), memoised under the SAME `context.evs_venue_ids`
    map events_serving_projection_steps.py uses, so the Background's venue
    and this file's bairro steps are one row."""
    if not hasattr(context, "evs_venue_ids"):
        context.evs_venue_ids = {}
    if name in context.evs_venue_ids:
        venue_id = context.evs_venue_ids[name]
    else:
        venue_id = f"evbt_{uuid.uuid4().hex[:12]}"
        context.evs_venue_ids[name] = venue_id
        context.repository.upsert_venue(
            Venue(
                venue_id=venue_id, venue_name=name, venue_address=address,
                venue_lat=-8.05, venue_lng=-34.88,
            )
        )
    context.evbt_current_venue_id = venue_id
    return venue_id


def _google_component(text: str, *types: str) -> dict:
    """Place Details' own `addressComponents` wire shape."""
    return {"longText": text, "types": list(types)}


def _program_google(context, components) -> None:
    """Program `fetch_address_components` — the method production actually
    calls — and give the venue in scope a place_id, without which
    `_maybe_fetch_google_address` short-circuits on `no_place_id` and the
    scenario would pass vacuously."""
    venue_id = _current_venue_id(context)
    context.repository.set_vibe_attributes(
        VibeAttributes(venue_id=venue_id, google_place_id=f"ChIJ_{venue_id}")
    )
    context.google_places_client.fetch_address_components = AsyncMock(
        return_value=components
    )


# ══════════════════════════════════════════════════════════════════════════
# ticket_url
# ══════════════════════════════════════════════════════════════════════════
@given('an accepted event at "{venue:NoQuote}" whose stored ticket url is "{url:NoQuote}"')
def step_given_event_at_venue_with_ticket_url(context, venue, url):
    _baseline(context)
    _insert_event(
        context, venue_name=venue, starts_at=_now(context) + timedelta(hours=2),
        time_known=True, ticket_url=url,
    )


@given('an accepted event whose stored ticket url is "{url:NoQuote}"')
def step_given_event_with_ticket_url(context, url):
    step_given_event_at_venue_with_ticket_url(context, _DEFAULT_VENUE, url)


@given("an accepted event with no stored ticket url")
def step_given_event_without_ticket_url(context):
    _baseline(context)
    _insert_event(
        context, venue_name=_DEFAULT_VENUE,
        starts_at=_now(context) + timedelta(hours=2), time_known=True,
    )


@given('an accepted recurring event whose recurrence text is "{text:NoQuote}" and whose stored ticket url is "{url:NoQuote}"')
def step_given_recurring_event_with_ticket_url(context, text, url):
    _baseline(context)
    stale = (_now(context) - timedelta(days=60)).astimezone(RECIFE_TZ)
    _insert_event(
        context, venue_name=_DEFAULT_VENUE, is_recurring=True, recurrence_text=text,
        starts_at=stale.replace(hour=22, minute=0, second=0, microsecond=0).astimezone(timezone.utc),
        time_known=True, ticket_url=url,
    )


@given("the events projection has already run once")
def step_given_projection_already_ran_once(context):
    _baseline(context)
    context.evbt_rows_before = {
        eid: dict(row) for eid, row in context.rds_store.events.items()
    }
    context.evs_summary = context.redis_projection_service.project_events(now=_now(context))


@when("the events projection runs again")
def step_when_projection_runs_again(context):
    context.evs_summary = context.redis_projection_service.project_events(now=_now(context))


def _occurrences_for_last_event(context) -> list[EventOccurrence]:
    """Every occurrence the projector actually wrote for the scenario's
    last inserted event, read back out of the city indexes it wrote."""
    event_id = context.evs_last_event_id
    found: list[EventOccurrence] = []
    for city in context.rds_store.get_geo_fence().get("cities", []):
        for member in context.redis_only_dao.get_city_events_index(city["slug"]):
            occ = context.redis_only_dao.get_event_occurrence(member)
            if occ is not None and occ.event_id == event_id:
                found.append(occ)
    found.sort(key=lambda o: o.occurrence_date)
    return found


@then('the occurrence payload carries the ticket url "{url:NoQuote}"')
def step_then_payload_ticket_url(context, url):
    occs = _occurrences_for_last_event(context)
    assert occs, "no occurrence projected for the scenario's event"
    for occ in occs:
        assert occ.ticket_url == url, (occ.occurrence_id, occ.ticket_url, url)


@then("the occurrence payload carries no ticket url")
def step_then_payload_no_ticket_url(context):
    occs = _occurrences_for_last_event(context)
    assert occs, "no occurrence projected for the scenario's event"
    for occ in occs:
        assert occ.ticket_url is None, (occ.occurrence_id, occ.ticket_url)


@then('the ticket url normalisation outcome "{outcome:NoQuote}" is counted once')
def step_then_ticket_outcome_counted_once(context, outcome):
    delta = _ticket_delta(context, outcome)
    assert delta == 1, f"{outcome}: expected exactly 1 increment, got {delta}"


@then('the ticket url normalisation outcome "{outcome:NoQuote}" is counted twice in total')
def step_then_ticket_outcome_counted_twice(context, outcome):
    delta = _ticket_delta(context, outcome)
    assert delta == 2, f"{outcome}: expected exactly 2 increments, got {delta}"


@then('the event row still holds the ticket url "{url:NoQuote}"')
def step_then_event_row_ticket_url_verbatim(context, url):
    row = context.rds_store.get_event(context.evs_last_event_id)
    assert row is not None
    assert row.get("ticket_url") == url, row.get("ticket_url")


@then("no event row was written during either cycle")
def step_then_no_event_row_written(context):
    before = context.evbt_rows_before
    after = {eid: dict(row) for eid, row in context.rds_store.events.items()}
    assert set(before) == set(after), (sorted(before), sorted(after))
    for eid, row in after.items():
        assert row == before[eid], (eid, before[eid], row)


@then("more than one occurrence is projected for that event")
def step_then_more_than_one_occurrence(context):
    occs = _occurrences_for_last_event(context)
    assert len(occs) > 1, f"expected a multi-occurrence expansion, got {len(occs)}"
    context.evbt_recurring_occurrences = occs


@then("every one of those occurrence payloads carries no ticket url")
def step_then_every_occurrence_no_ticket_url(context):
    for occ in context.evbt_recurring_occurrences:
        assert occ.ticket_url is None, (occ.occurrence_id, occ.ticket_url)


# ══════════════════════════════════════════════════════════════════════════
# venue_neighborhood — candidate selection modes
# ══════════════════════════════════════════════════════════════════════════
@given('"{venue:NoQuote}" has street, neighborhood, city and postal code stored')
def step_given_all_four_stored(context, venue):
    venue_id = _make_venue(context, venue)
    context.evbt_pending_full_address = {
        "street": "Rua Quarenta e Oito, 490",
        "neighborhood": "Espinheiro",
        "city": "Recife",
        "postal_code": "52020-060",
    }
    context.evbt_pending_full_address_venue = venue_id


@given('every one of those four values is sourced "{source:NoQuote}"')
def step_given_all_four_sourced(context, source):
    context.repository.update_venue_address_components(
        context.evbt_pending_full_address_venue,
        source=source,
        **context.evbt_pending_full_address,
    )
    addr = _address_row(context, context.evbt_pending_full_address_venue)
    for field in _ADDRESS_FIELDS:
        assert addr.get(field) is not None, (field, addr)
        assert addr.get(f"{field}_source") == source, (field, addr)


def _select_candidates(context, **kwargs) -> None:
    context.evbt_selection_error = None
    try:
        context.evbt_candidates = context.repository.list_address_backfill_candidates(
            None, 100, **kwargs
        )
    except Exception as e:  # noqa: BLE001 - the scenario asserts on it
        context.evbt_candidates = None
        context.evbt_selection_error = e


@when("the address components backfill selects candidates in upgrade mode")
def step_when_select_upgrade(context):
    _select_candidates(context, mode="upgrade")


@when("the address components backfill selects candidates in the default mode")
def step_when_select_default(context):
    _select_candidates(context)


@when('the address components backfill selects candidates in mode "{mode:NoQuote}"')
def step_when_select_named_mode(context, mode):
    _select_candidates(context, mode=mode)


@then('"{venue:NoQuote}" is among the selected candidates')
def step_then_venue_among_candidates(context, venue):
    assert context.evbt_selection_error is None, context.evbt_selection_error
    venue_id = context.evs_venue_ids[venue]
    ids = [row["venue_id"] for row in context.evbt_candidates]
    assert venue_id in ids, (venue_id, ids)


@then("no candidates are selected")
def step_then_no_candidates(context):
    assert context.evbt_selection_error is None, context.evbt_selection_error
    assert context.evbt_candidates == [], context.evbt_candidates


@then("the selection is rejected with an error naming the allowed modes")
def step_then_selection_rejected(context):
    err = context.evbt_selection_error
    assert isinstance(err, ValueError), f"expected ValueError, got {err!r}"
    message = str(err)
    for mode in ("fill", "upgrade"):
        assert mode in message, (mode, message)


# ══════════════════════════════════════════════════════════════════════════
# venue_neighborhood — the Google upgrade rung
# ══════════════════════════════════════════════════════════════════════════
@given('"{venue:NoQuote}" has the neighborhood "{value:NoQuote}" sourced "{source:NoQuote}"')
def step_given_venue_neighborhood_sourced(context, venue, value, source):
    venue_id = _make_venue(context, venue)
    context.repository.update_venue_address_components(
        venue_id, source=source, neighborhood=value
    )
    addr = _address_row(context, venue_id)
    assert addr.get("neighborhood") == value, addr
    assert addr.get("neighborhood_source") == source, addr


@given("the free-tier address lookup switch is on")
def step_given_free_tier_switch_on(context):
    context.admin_config_service.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, True)


@given("the free-tier address lookup switch is off")
def step_given_free_tier_switch_off(context):
    context.admin_config_service.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, False)
    context.evbt_geocoding_baseline = {
        outcome: VENUE_GEOCODING_REQUESTS_TOTAL.labels(outcome=outcome)._value.get()
        for outcome in ("success", "no_place_id", "api_error", "disabled")
    }
    # The spy goes on the method production code actually calls — a spy on
    # anything else would assert nothing.
    context.google_places_client.fetch_address_components = AsyncMock(
        side_effect=AssertionError(
            "fetch_address_components must not be called while the switch is off"
        )
    )


@given('Google answers that venue\'s address components with the sublocality "{value:NoQuote}"')
def step_given_google_answers_sublocality(context, value):
    _program_google(
        context, [_google_component(value, "sublocality_level_1", "political")]
    )


@given("Google answers that venue's address components with no components at all")
def step_given_google_answers_nothing(context):
    _program_google(context, None)


@given("Google answers that venue's address components with the same name for the second-level area and the city")
def step_given_google_answers_area_equals_city(context):
    # ONE component typed `administrative_area_level_2`: the LAST rung of
    # `_NEIGHBORHOOD_TYPES` and the FIRST of `_CITY_TYPES`, so
    # `map_address_components` genuinely returns neighborhood == city for a
    # municipality that publishes no `sublocality*` (F09's pollution class).
    _program_google(
        context, [_google_component("Igarassu", "administrative_area_level_2", "political")]
    )


@given("a servable venue in a municipality that publishes no sublocality")
def step_given_venue_no_sublocality_municipality(context):
    _make_venue(context, "Igarassu Bar")


@when("the address components backfill runs one batch in upgrade mode")
def step_when_backfill_batch_upgrade(context):
    context.evbt_batch_started_at = time.time()
    context.evbt_batch_result = asyncio.run(
        context.venue_address_backfill_service.backfill_batch(limit=200, mode="upgrade")
    )
    context.evbt_batch_finished_at = time.time()


@then('"{venue:NoQuote}" has the neighborhood "{value:NoQuote}"')
def step_then_named_venue_neighborhood(context, venue, value):
    addr = _address_row(context, context.evs_venue_ids[venue])
    assert addr.get("neighborhood") == value, addr.get("neighborhood")
    context.evbt_current_venue_id = context.evs_venue_ids[venue]


@then('"{venue:NoQuote}" still has the neighborhood "{value:NoQuote}"')
def step_then_named_venue_neighborhood_still(context, venue, value):
    step_then_named_venue_neighborhood(context, venue, value)


@then('that neighborhood is sourced "{source:NoQuote}"')
def step_then_neighborhood_sourced(context, source):
    addr = _address_row(context, _current_venue_id(context))
    assert addr.get("neighborhood_source") == source, addr.get("neighborhood_source")


@then("that venue has no neighborhood stored")
def step_then_venue_no_neighborhood(context):
    addr = _address_row(context, _current_venue_id(context))
    assert addr.get("neighborhood") is None, addr.get("neighborhood")


@then("that venue has the city stored")
def step_then_venue_has_a_city(context):
    addr = _address_row(context, _current_venue_id(context))
    assert addr.get("city"), addr.get("city")


@then("no Place Details address lookup is made")
def step_then_no_place_details_lookup(context):
    context.google_places_client.fetch_address_components.assert_not_called()


@then('the address lookup outcome "{outcome:NoQuote}" is counted')
def step_then_geocoding_outcome_counted(context, outcome):
    before = context.evbt_geocoding_baseline[outcome]
    after = VENUE_GEOCODING_REQUESTS_TOTAL.labels(outcome=outcome)._value.get()
    assert after > before, f"{outcome}: {before} -> {after}"


# ══════════════════════════════════════════════════════════════════════════
# venue_neighborhood — the shared write boundary
# ══════════════════════════════════════════════════════════════════════════
@given('a servable venue whose stored city is "{city:NoQuote}"')
def step_given_venue_with_city(context, city):
    venue_id = _make_venue(context, f"Venue of {city}")
    context.repository.update_venue_address_components(
        venue_id, source="parsed", city=city
    )
    assert _address_row(context, venue_id).get("city") == city


@given('a servable venue whose stored city is "{city:NoQuote}" sourced "{source:NoQuote}"')
def step_given_venue_with_city_sourced(context, city, source):
    venue_id = _make_venue(context, f"Venue of {city} ({source})")
    context.repository.update_venue_address_components(
        venue_id, source=source, city=city
    )
    addr = _address_row(context, venue_id)
    assert addr.get("city") == city, addr
    assert addr.get("city_source") == source, addr


@when('the address text parser writes the neighborhood "{value:NoQuote}" for that venue')
def step_when_parser_writes_neighborhood(context, value):
    context.repository.update_venue_address_components(
        _current_venue_id(context), source="parsed", neighborhood=value
    )


@when('the address text parser writes the city "{city:NoQuote}" and the neighborhood "{neighborhood:NoQuote}" for that venue')
def step_when_parser_writes_city_and_neighborhood(context, city, neighborhood):
    context.repository.update_venue_address_components(
        _current_venue_id(context), source="parsed", city=city, neighborhood=neighborhood
    )


@when('the address text parser writes the neighborhood "{neighborhood:NoQuote}" and the street "{street:NoQuote}" for that venue')
def step_when_parser_writes_neighborhood_and_street(context, neighborhood, street):
    context.repository.update_venue_address_components(
        _current_venue_id(context), source="parsed",
        neighborhood=neighborhood, street=street,
    )


@when('Google writes the city "{city:NoQuote}" and the neighborhood "{neighborhood:NoQuote}" for that venue')
def step_when_google_writes_city_and_neighborhood(context, city, neighborhood):
    context.repository.update_venue_address_components(
        _current_venue_id(context), source="google", city=city, neighborhood=neighborhood
    )


@when('an operator writes the neighborhood "{neighborhood:NoQuote}" for that venue')
def step_when_operator_writes_neighborhood(context, neighborhood):
    context.repository.update_venue_address_components(
        _current_venue_id(context), source="operator", neighborhood=neighborhood
    )


@then('that venue has the neighborhood "{value:NoQuote}"')
def step_then_that_venue_neighborhood(context, value):
    addr = _address_row(context, _current_venue_id(context))
    assert addr.get("neighborhood") == value, addr.get("neighborhood")


@then('that venue still has the city "{city:NoQuote}" stored')
@then('that venue has the city "{city:NoQuote}" stored')
def step_then_that_venue_city(context, city):
    addr = _address_row(context, _current_venue_id(context))
    assert addr.get("city") == city, addr.get("city")


@then('that city is sourced "{source:NoQuote}"')
def step_then_city_sourced(context, source):
    addr = _address_row(context, _current_venue_id(context))
    assert addr.get("city_source") == source, addr.get("city_source")


@then('that venue has the street "{street:NoQuote}" stored')
def step_then_that_venue_street(context, street):
    addr = _address_row(context, _current_venue_id(context))
    assert addr.get("street") == street, addr.get("street")


@given('that venue\'s stored neighborhood has been corrected to "{value:NoQuote}"')
def step_given_neighborhood_corrected(context, value):
    venue_id = context.evs_venue_ids[_DEFAULT_VENUE]
    context.repository.update_venue_address_components(
        venue_id, source="google", neighborhood=value
    )
    context.evbt_current_venue_id = venue_id


@then('the occurrence payload carries the venue neighborhood "{value:NoQuote}"')
def step_then_payload_neighborhood(context, value):
    occs = _occurrences_for_last_event(context)
    assert occs, "no occurrence projected for the scenario's event"
    for occ in occs:
        assert occ.venue_neighborhood == value, occ.venue_neighborhood


@then("the occurrence payload still carries every other contracted field")
def step_then_payload_contract_intact(context):
    occs = _occurrences_for_last_event(context)
    assert occs, "no occurrence projected for the scenario's event"
    actual = set(EventOccurrence.model_fields)
    assert actual == set(_CONTRACT_FIELDS), (
        "EventOccurrence's field set changed: "
        f"added={sorted(actual - set(_CONTRACT_FIELDS))} "
        f"removed={sorted(set(_CONTRACT_FIELDS) - actual)}"
    )
    occ = occs[0]
    payload = occ.model_dump()
    assert set(payload) == set(_CONTRACT_FIELDS)
    for field in ("occurrence_id", "event_id", "occurrence_date", "starts_at",
                  "venue_id", "venue_name", "venue_lat", "venue_lng",
                  "source_permalink", "source_handle", "city_slug", "status"):
        assert payload[field] is not None, field


# ══════════════════════════════════════════════════════════════════════════
# the address gauges
# ══════════════════════════════════════════════════════════════════════════
@given('the catalog holds address rows sourced "{a:NoQuote}", "{b:NoQuote}" and "{c:NoQuote}"')
def step_given_catalog_holds_mixed_sources(context, a, b, c):
    context.evbt_expected_sources = {}
    for i, source in enumerate((a, b, c)):
        venue_id = _make_venue(context, f"Provenance venue {i} ({source})")
        context.repository.update_venue_address_components(
            venue_id, source=source,
            street=f"Rua {i}", neighborhood=f"Bairro {i}",
            city=f"Cidade {i}", postal_code=f"5000{i}-000",
        )
        context.evbt_expected_sources[venue_id] = source


@given("the catalog holds two address rows whose stored neighborhood equals its stored city")
def step_given_two_rows_neighborhood_equals_city(context):
    # Written through `operator`, the ONE source the write-time guard
    # exempts (R08) — a `google`/`parsed` write of the same shape is
    # dropped by design, so this is the only honest way to stage the
    # pre-existing residue the gauge exists to measure.
    for i in range(2):
        venue_id = _make_venue(context, f"Self-named bairro {i}")
        context.repository.update_venue_address_components(
            venue_id, source="operator", city="Igarassu", neighborhood="Igarassu",
        )
        addr = _address_row(context, venue_id)
        assert addr.get("neighborhood") == "Igarassu", addr


@given("no address components backfill batch has ever run")
def step_given_no_batch_has_run(context):
    # Prometheus gauges are process-global; this step establishes the
    # never-refreshed state a fresh process would start in.
    VENUE_ADDRESS_GAUGES_REFRESHED_TIMESTAMP_SECONDS.set(0)


@then("the address provenance gauge reports a row count per field and per source")
def step_then_provenance_gauge_reports(context):
    reported = {
        (field, source): VENUE_ADDRESS_SOURCE_ROWS.labels(field=field, source=source)._value.get()
        for field in _ADDRESS_FIELDS
        for source in _ADDRESS_SOURCES
    }
    assert sum(reported.values()) > 0, reported
    context.evbt_reported_provenance = reported


@then("the reported counts match the rows actually stored")
def step_then_provenance_counts_match(context):
    expected = {(f, s): 0 for f in _ADDRESS_FIELDS for s in _ADDRESS_SOURCES}
    for addr in context.rds_store.addresses.values():
        for field in _ADDRESS_FIELDS:
            source = addr.get(f"{field}_source") or "none"
            expected[(field, source)] += 1
    assert context.evbt_reported_provenance == expected, (
        context.evbt_reported_provenance, expected
    )


@then("the neighborhood-equals-city gauge reports {count:d}")
def step_then_neighborhood_equals_city_gauge(context, count):
    value = VENUE_ADDRESS_NEIGHBORHOOD_EQUALS_CITY._value.get()
    assert value == count, value


@then("the address gauges refresh timestamp reports 0")
def step_then_refresh_timestamp_zero(context):
    value = VENUE_ADDRESS_GAUGES_REFRESHED_TIMESTAMP_SECONDS._value.get()
    assert value == 0, value


@then("the address gauges refresh timestamp reports that batch's time")
def step_then_refresh_timestamp_stamped(context):
    value = VENUE_ADDRESS_GAUGES_REFRESHED_TIMESTAMP_SECONDS._value.get()
    assert context.evbt_batch_started_at <= value <= context.evbt_batch_finished_at, (
        value, context.evbt_batch_started_at, context.evbt_batch_finished_at
    )


# ══════════════════════════════════════════════════════════════════════════
# the nightlife day
# ══════════════════════════════════════════════════════════════════════════
@given('an accepted recurring event at "{venue:NoQuote}" whose recurrence text is "{text:NoQuote}" and whose resolved time is {clock}')
def step_given_recurring_event_with_text_and_time(context, venue, text, clock):
    _baseline(context)
    hour, minute = (int(p) for p in clock.strip().split(":"))
    # A deliberately STALE starts_at, matching this repo's own recurring
    # fixture convention: only its clock time survives expansion.
    stale = (_now(context) - timedelta(days=60)).astimezone(RECIFE_TZ)
    _insert_event(
        context, venue_name=venue, is_recurring=True, recurrence_text=text,
        starts_at=stale.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        ).astimezone(timezone.utc),
        time_known=True,
    )


@given("the events projection has already run at {when} in Recife")
def step_given_projection_already_ran_at(context, when):
    _baseline(context)
    context.evs_now = _parse_recife(when).astimezone(timezone.utc)
    context.evs_summary = context.redis_projection_service.project_events(now=_now(context))


def _dated_occurrence_id(context, date: str) -> str:
    return f"{context.evs_last_event_id}_{date}"


def _index_holds_date(context, members, date: str) -> bool:
    """True when `members` holds the scenario event's occurrence for
    `date` — under its dated recurring id, or (for a one-off) under the
    bare event id whose payload carries that occurrence_date."""
    event_id = context.evs_last_event_id
    if _dated_occurrence_id(context, date) in members:
        context.evbt_last_checked_occurrence_id = _dated_occurrence_id(context, date)
        return True
    if event_id in members:
        occ = context.redis_only_dao.get_event_occurrence(event_id)
        if occ is not None and occ.occurrence_date == date:
            context.evbt_last_checked_occurrence_id = event_id
            return True
    return False


@then('the city index for "{slug:NoQuote}" contains the occurrence for {date}')
def step_then_city_index_contains_date(context, slug, date):
    members = context.redis_only_dao.get_city_events_index(slug)
    assert _index_holds_date(context, members, date), (date, members)


@then('the city index for "{slug:NoQuote}" does not contain the occurrence for {date}')
def step_then_city_index_lacks_date(context, slug, date):
    members = context.redis_only_dao.get_city_events_index(slug)
    assert not _index_holds_date(context, members, date), (date, members)


@then('the venue index for "{venue:NoQuote}" contains the occurrence for {date}')
def step_then_venue_index_contains_date(context, venue, date):
    venue_id = context.evs_venue_ids[venue]
    members = context.redis_only_dao.get_venue_events_index(venue_id)
    assert _index_holds_date(context, members, date), (date, members)


@then('the venue index for "{venue:NoQuote}" does not contain the occurrence for {date}')
def step_then_venue_index_lacks_date(context, venue, date):
    venue_id = context.evs_venue_ids[venue]
    members = context.redis_only_dao.get_venue_events_index(venue_id)
    assert not _index_holds_date(context, members, date), (date, members)


@then("that occurrence has a payload key")
def step_then_occurrence_has_payload(context):
    occ_id = context.evbt_last_checked_occurrence_id
    assert context.redis_only_dao.get_event_occurrence(occ_id) is not None, occ_id


@then("the occurrence for {date} has no payload key")
def step_then_occurrence_has_no_payload(context, date):
    occ_id = _dated_occurrence_id(context, date)
    assert context.redis_only_dao.get_event_occurrence(occ_id) is None, occ_id


@then("that occurrence is scored by its own start time of {when} in Recife")
def step_then_occurrence_scored_by_start(context, when):
    occ_id = context.evbt_last_checked_occurrence_id
    expected = _parse_recife(when).astimezone(timezone.utc).timestamp()
    city_score = context.redis_only_dao.city_events_index_score("recife", occ_id)
    assert city_score == expected, (occ_id, city_score, expected)
    occ = context.redis_only_dao.get_event_occurrence(occ_id)
    assert occ is not None and occ.starts_at.timestamp() == expected, occ


@then("the nightlife rollback counter is bumped once")
def step_then_rollback_counter_bumped(context):
    _baseline(context)
    delta = EVENTS_PROJECTION_NIGHTLIFE_ROLLBACK_TOTAL._value.get() - context.evbt_rollback_baseline
    assert delta == 1, delta


@then("the nightlife rollback counter is not bumped")
def step_then_rollback_counter_not_bumped(context):
    _baseline(context)
    delta = EVENTS_PROJECTION_NIGHTLIFE_ROLLBACK_TOTAL._value.get() - context.evbt_rollback_baseline
    assert delta == 0, delta


# ══════════════════════════════════════════════════════════════════════════
# the events city vocabulary
# ══════════════════════════════════════════════════════════════════════════
@given('the geo fence configures the cities "{first:NoQuote}" and "{second:NoQuote}"')
def step_given_geo_fence_configures(context, first, second):
    from app.services.venue_eligibility import CAPITALS_BY_SLUG

    cities = []
    for slug in (first, second):
        capital = CAPITALS_BY_SLUG[slug]
        cities.append({
            "slug": slug, "name": capital["name"],
            "lat": capital["lat"], "lng": capital["lng"], "radius_km": 150.0,
        })
    context.rds_store.set_geo_fence({"enabled": True, "cities": cities})


@given("every accepted event is in Recife")
def step_given_every_event_in_recife(context):
    _baseline(context)
    _insert_event(
        context, venue_name=_DEFAULT_VENUE,
        starts_at=_now(context) + timedelta(hours=2), time_known=True,
    )


def _fail_remember_for(context, should_fail) -> None:
    """Wrap the REAL `remember_city_slug` so it raises for the slugs
    `should_fail` selects and behaves normally for the rest, recording every
    slug it refused. Wrapping rather than replacing matters: the projector
    reaches this method from TWO call sites with different contracts (§5's
    guarded configured-slug loop, and `index_event_occurrence`'s unguarded
    same-call durability write), and a blanket replacement could only ever
    exercise both at once."""
    real = context.redis_only_dao.remember_city_slug
    context.evbt_remember_failures = []

    def _maybe_boom(city_slug):
        if should_fail(city_slug):
            context.evbt_remember_failures.append(city_slug)
            raise redis.RedisError(
                f"SADD events_known_cities_v1 {city_slug} failed"
            )
        return real(city_slug)

    context.redis_only_dao.remember_city_slug = _maybe_boom


@given('remembering the configured city "{slug:NoQuote}" fails')
def step_given_remember_configured_city_fails(context, slug):
    """Fails ONLY for a configured city that holds no events, so the only
    call site reached is §5's guarded loop — `index_event_occurrence` is
    never asked to remember this slug, because nothing is indexed under it.
    That is what makes the next scenario's opposite assertion meaningful
    rather than a contradiction."""
    context.evbt_expected_failing_slug = slug
    _fail_remember_for(context, lambda city_slug: city_slug == slug)


@given("remembering any city slug fails")
def step_given_remember_any_city_slug_fails(context):
    context.evbt_expected_failing_slug = None
    _fail_remember_for(context, lambda city_slug: True)


@when("the events projection runs and is allowed to fail")
def step_when_projection_runs_and_may_fail(context):
    context.evs_summary = None
    context.evbt_projection_error = None
    try:
        context.evs_summary = context.redis_projection_service.project_events(
            now=_now(context)
        )
    except Exception as e:  # noqa: BLE001 - the scenario asserts on it
        context.evbt_projection_error = e


@then("remembering that configured city was attempted and failed")
def step_then_configured_city_write_attempted(context):
    """Anti-vacuity: without this the scenario would pass just as happily if
    the injected failure had never fired at all."""
    assert context.evbt_remember_failures == [context.evbt_expected_failing_slug], (
        context.evbt_remember_failures, context.evbt_expected_failing_slug
    )


@then("the projection cycle fails loudly instead of returning a summary")
def step_then_cycle_fails_loudly(context):
    assert context.evbt_projection_error is not None, (
        "project_events returned a summary; the known-cities write that is "
        "coupled to the index write must NOT be swallowed"
    )
    assert isinstance(context.evbt_projection_error, redis.RedisError), (
        context.evbt_projection_error
    )
    assert context.evs_summary is None


@then("the divergence that failure exists to surface is visible")
def step_then_divergence_visible(context):
    """The concrete reason the raise must not be swallowed: the two ZADDs
    inside `index_event_occurrence` land BEFORE its SADD, so at the moment
    of failure the occurrence IS in the city index while the slug is NOT in
    the vocabulary vibes_bot validates against. A guard here would leave
    exactly this state behind silently, every cycle."""
    indexed = set(context.redis_only_dao.get_city_events_index("recife"))
    assert indexed, "nothing was indexed, so there is no divergence to observe"
    assert context.redis_only_dao.list_known_city_slugs() == [], (
        context.redis_only_dao.list_known_city_slugs()
    )


@when('the geo fence is reduced to the city "{slug:NoQuote}"')
def step_when_geo_fence_reduced(context, slug):
    fence = context.rds_store.get_geo_fence()
    remaining = [c for c in fence.get("cities", []) if c["slug"] == slug]
    assert remaining, slug
    context.rds_store.set_geo_fence({"enabled": True, "cities": remaining})


@when("the known events cities set is erased")
def step_when_known_cities_erased(context):
    context.fake_redis.delete("events_known_cities_v1")
    assert context.redis_only_dao.list_known_city_slugs() == []


@then('the known events cities are "{first:NoQuote}" and "{second:NoQuote}"')
def step_then_known_events_cities(context, first, second):
    actual = sorted(context.redis_only_dao.list_known_city_slugs())
    assert actual == sorted([first, second]), actual


@then("the projection still writes every accepted occurrence")
def step_then_projection_still_writes(context):
    rows = context.rds_store.list_events_for_projection(now=_now(context))
    assert rows, "no selectable event — the assertion below would be vacuous"
    members = set()
    for city in context.rds_store.get_geo_fence().get("cities", []):
        members |= set(context.redis_only_dao.get_city_events_index(city["slug"]))
    assert members, "nothing was indexed at all"
    for row in rows:
        event_id = row["event_id"]
        assert any(
            member == event_id or member.startswith(f"{event_id}_")
            for member in members
        ), (event_id, sorted(members))
    assert context.evs_summary["occurrences"] == len(members), (
        context.evs_summary, sorted(members)
    )


@then("the projection cycle is not reported as failed")
def step_then_cycle_not_failed(context):
    assert context.evs_summary["errors"] == 0, context.evs_summary
    assert context.evs_summary["error_events"] == [], context.evs_summary
