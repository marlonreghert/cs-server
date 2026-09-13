"""Behave steps for
tests/bdd/observability/events-venue-night-duplication-backlog.feature.

See plans/260912_events-venue-night-duplication.md §A. Self-contained per
scenario (own fresh `InMemoryRdsVenueStore` + fakeredis), mirroring
`tests/bdd/steps/event_dedup_fuzzy_title_steps.py`'s own pattern — this
feature reads a REPORT over rows seeded directly through the DAO.

The gauge scenario deliberately drives a REAL `EventExtractionService.run()`
(with a post source that yields nothing) rather than calling the publish
helper directly: the property under test is "an extraction run publishes
these measures", and calling the publisher by hand would assert the
publisher, not the wiring.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.metrics import EVENT_DEDUP_BACKLOG
from app.models.event_kind import KIND_EVENT, KIND_MENU
from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services import event_dedup
from app.services.event_dedup_backlog import (
    MEASURE_VENUE_NIGHT_EXCESS_ROWS,
    MEASURE_VENUE_NIGHT_GROUPS,
    collect_dedup_backlog,
)
from app.services.event_merge import merge_touched_events
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE = ZoneInfo("America/Recife")
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_SATURDAY = "2026-09-12"
_SUNDAY = "2026-09-13"
_HANDLE = "beerdock_recife"

_VENUE_ADDRESSES = {
    "Club Metrópole": "R. das Ninfas, 125 - Boa Vista, Recife - PE",
    "BeerDock Boa Viagem": "Av. Cons. Aguiar, 1000 - Boa Viagem, Recife - PE",
    "BeerDock Casa Forte": "Av. Rui Barbosa, 500 - Casa Forte, Recife - PE",
    "Sempre Rock Bar": "R. do Futuro, 50 - Graças, Recife - PE",
}


def _local_dt(date_str: str, time_str: str = "21:00") -> datetime:
    hour, minute = (int(p) for p in time_str.split(":"))
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=RECIFE)


def _ensure(context) -> None:
    if hasattr(context, "backlog_dao"):
        return
    context.backlog_dao = InMemoryRdsVenueStore()
    context.backlog_redis = fakeredis.FakeRedis(decode_responses=True)
    context.backlog_venues = {}
    context.backlog_event_ids = []
    context.backlog_seq = 0
    context.backlog_client = None
    context.backlog_gauge_before = None


def _ensure_venue(context, name: str) -> str:
    _ensure(context)
    if name not in context.backlog_venues:
        venue_id = f"backlog_venue_{len(context.backlog_venues) + 1}"
        context.backlog_dao.upsert_venue(Venue(
            venue_id=venue_id, venue_name=name, venue_lat=-8.05, venue_lng=-34.88,
            venue_address=_VENUE_ADDRESSES.get(name, f"{name}, Recife - PE"),
        ))
        context.backlog_venues[name] = venue_id
    return context.backlog_venues[name]


def _seed(
    context, title, venue_name, *, starts_at=None, post_type=KIND_EVENT,
    location_text=None, source_handle=None, status="accepted",
) -> str:
    _ensure(context)
    venue_id = _ensure_venue(context, venue_name) if venue_name else None
    context.backlog_seq += 1
    seen = _NOW + timedelta(seconds=context.backlog_seq)
    fields = {
        "event_id": new_event_id(), "venue_id": venue_id, "starts_at": starts_at,
        "title": title, "post_type": post_type, "status": status,
        "location_text": None, "lineup": [],
        "source_kind": "venue_post", "source_handle": source_handle or _HANDLE,
        "source_shortcode": f"backlog_sc_{context.backlog_seq}",
        "first_seen_at": seen, "last_seen_at": seen,
        # The event's own frozen extracted location, exactly where the real
        # pipeline stores it (the primary source's raw_extraction) — never
        # the event-level column, which only an operator PATCH writes.
        "raw_extraction": {"location_text": location_text} if location_text else {},
    }
    context.backlog_dao.insert_event(fields)
    context.backlog_event_ids.append(fields["event_id"])
    return fields["event_id"]


def _read_backlog(context):
    context.backlog = collect_dedup_backlog(
        context.backlog_dao, redis_like=context.backlog_redis,
    )
    return context.backlog


def _client(context) -> TestClient:
    if context.backlog_client is None:
        app = FastAPI()
        app.include_router(admin_events_router)
        set_events_container(type("C", (), {
            "pipeline_repository": context.backlog_dao,
            "redis_client": context.backlog_redis,
        })())
        context.backlog_client = TestClient(app)
    return context.backlog_client


def _gauge(measure: str) -> float:
    for metric in EVENT_DEDUP_BACKLOG.collect():
        for sample in metric.samples:
            if sample.labels.get("measure") == measure:
                return sample.value
    return float("nan")


# ── Background ───────────────────────────────────────────────────────────
# "Given the event extraction pipeline is configured for a known venue" is
# NOT redefined here: behave's step registry is global, and that text is
# already bound in `event_ticket_info_and_attractions_steps.py` (which seeds
# the shared `context.ee_*` harness). Reusing it — rather than rewording the
# feature or shadowing the definition — is this repo's own documented
# convention for a step text two features legitimately share. Every step
# below builds this feature's OWN fixtures lazily through `_ensure`, so the
# shared Background's work is simply irrelevant here, never conflicting.
@given('the venue catalog carries "{a}" and "{b}"')
def step_given_catalog_carries_two(context, a, b):
    _ensure_venue(context, a)
    _ensure_venue(context, b)


# ── seeds ─────────────────────────────────────────────────────────────────
_THREE_ACTS = ("ROWKA", "VITINHO POLÊMICO", "SÁBADO VAI FERVER")


@given('three stored events at "{venue}" on one Saturday')
def step_given_three_events_saturday(context, venue):
    for title in _THREE_ACTS:
        _seed(context, title, venue, starts_at=_local_dt(_SATURDAY))


@given('two stored events at "{venue}" on one Saturday')
def step_given_two_events_saturday(context, venue):
    for title in _THREE_ACTS[:2]:
        _seed(context, title, venue, starts_at=_local_dt(_SATURDAY))


@given('one stored event at "{venue}" on that Saturday')
def step_given_one_event_that_saturday(context, venue):
    _seed(context, "NOITE SOLO", venue, starts_at=_local_dt(_SATURDAY))


@given('one stored event at "{venue}" on one Saturday')
def step_given_one_event_saturday(context, venue):
    _seed(context, "NOITE SOLO", venue, starts_at=_local_dt(_SATURDAY))


@given('two stored events at "{venue}" on one Sunday')
def step_given_two_events_sunday(context, venue):
    for title in ("DOMINGO DA LUA", "TARDEZINHA DO MAR"):
        _seed(context, title, venue, starts_at=_local_dt(_SUNDAY))


@given('one stored event at "{venue}" on one Sunday')
def step_given_one_event_sunday(context, venue):
    _seed(context, "DOMINGO DA LUA", venue, starts_at=_local_dt(_SUNDAY))


@given("one of them has been superseded by a merge")
def step_given_one_superseded(context):
    absorbed = context.backlog_event_ids[-1]
    survivor = context.backlog_event_ids[-2]
    context.backlog_dao.update_event(
        absorbed, {"status": "superseded", "superseded_by": survivor},
    )


@given('one stored event and one stored menu item at "{venue}" on one Saturday')
def step_given_event_and_menu(context, venue):
    _seed(context, "ROWKA", venue, starts_at=_local_dt(_SATURDAY))
    _seed(context, "Especial do dia", venue, starts_at=_local_dt(_SATURDAY), post_type=KIND_MENU)


@given("two stored events on one Saturday with no venue resolved")
def step_given_two_events_no_venue(context):
    _ensure(context)
    for title in _THREE_ACTS[:2]:
        _seed(context, title, None, starts_at=_local_dt(_SATURDAY))


@given('two stored events at "{venue}" on one Saturday whose titles share no distinctive word')
def step_given_two_disjoint(context, venue):
    for title in ("ROWKA", "VITINHO POLÊMICO"):
        _seed(context, title, venue, starts_at=_local_dt(_SATURDAY))


@given('two stored events at "{venue}" on one Sunday whose titles are entirely generic')
def step_given_two_generic(context, venue):
    for title in ("Sextou", "Festa"):
        _seed(context, title, venue, starts_at=_local_dt(_SUNDAY))


@given('two stored events at "{venue}" on one day whose titles differ only in the speaker\'s name')
def step_given_two_suggest_band(context, venue):
    context.backlog_suggest_ids = [
        _seed(context, "Ação Leitura: Bate-papo com Marcelino Freire", venue,
              starts_at=_local_dt(_SATURDAY, "19:00")),
        _seed(context, "Ação Leitura: Bate-papo com Jeferson Tenório", venue,
              starts_at=_local_dt(_SATURDAY, "19:00")),
    ]


@given("the merge pass has run for that venue")
def step_given_merge_pass_has_run(context):
    merge_touched_events(
        context.backlog_dao, list(context.backlog_event_ids), _NOW,
        redis_like=context.backlog_redis,
    )


@given('two stored events at "{venue}" whose recorded location text is "{location_text}"')
def step_given_two_events_with_location_text(context, venue, location_text):
    for title in ("SAMBINHA", "ROCK NIGHT"):
        _seed(context, title, venue, starts_at=_local_dt(_SATURDAY), location_text=location_text)


@given('one stored event at "{venue}" whose recorded location text is "{location_text}"')
def step_given_one_event_with_location_text(context, venue, location_text):
    _seed(context, "PAGODE DA CASA", venue, starts_at=_local_dt(_SUNDAY), location_text=location_text)


@given("twelve venue-nights each holding two stored events")
def step_given_twelve_venue_nights(context):
    _ensure(context)
    for index in range(12):
        venue = f"Paged Venue {index + 1}"
        for title in ("ROWKA", "VITINHO POLÊMICO"):
            _seed(context, title, venue, starts_at=_local_dt(_SATURDAY))


def set_backlog_single_night_venue(context, venue: str) -> str:
    """The backlog harness's half of the shared `"X" runs one night rather
    than a programme` step. The STEP itself is defined once, in
    `events_venue_night_duplication_steps.py`, because behave's registry is
    global and all three of this plan's feature files state the same
    precondition — that one definition dispatches to whichever harness the
    running scenario actually built, and calls this for the backlog one."""
    venue_id = _ensure_venue(context, venue)
    context.backlog_redis.set(
        event_dedup.ADMIN_CONFIG_SINGLE_NIGHT_VENUES_KEY, json.dumps([venue_id]),
    )
    context.backlog_single_night_venue_ids = [venue_id]
    return venue_id


@given("the excess row gauge has been recorded")
def step_given_excess_gauge_recorded(context):
    from app.services.event_dedup_backlog import publish_dedup_backlog_gauges

    publish_dedup_backlog_gauges(context.backlog_dao, redis_like=context.backlog_redis)
    context.backlog_gauge_before = _gauge(MEASURE_VENUE_NIGHT_EXCESS_ROWS)


# ── When ──────────────────────────────────────────────────────────────────
@when("the dedup backlog is read")
def step_when_backlog_read(context):
    context.backlog_rows_before = {
        eid: context.backlog_dao.get_event(eid) for eid in context.backlog_event_ids
    }
    _read_backlog(context)


@when("an extraction run completes")
def step_when_extraction_run_completes(context):
    from app.services.event_extraction_service import EventExtractionService

    class _NoPosts:
        async def posts_for_venue(self, venue_id, since):
            return []

        async def posts_for_handle(self, handle, venue_ids, since):
            return []

        async def image_data_uri(self, key):
            return None

    class _VenueDao:
        """`EventExtractionService` reaches for `get_venue_instagram` (a
        `VenueRepository` method, not an `InMemoryRdsVenueStore` one) purely
        to label a venue's posts with its handle. This scenario extracts no
        posts at all — it exists to prove the END of a run publishes the
        backlog gauges — so the handle lookup is stubbed rather than a whole
        repository (and its Redis half) wired up for it."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_venue_instagram(self, venue_id):
            return None

    service = EventExtractionService(
        venue_dao=_VenueDao(context.backlog_dao), post_source=_NoPosts(), openai_client=None,
        now_provider=lambda: _NOW, redis_client=context.backlog_redis,
    )
    asyncio.run(service.run({
        "eligibility": {
            "mode": "venue_ids",
            "venue_ids": ",".join(context.backlog_venues.values()),
        },
    }))


@when("the dedup sweep runs with apply for that single-night venue")
def step_when_sweep_runs_with_apply(context):
    from scripts.measure_event_dedup import sweep

    sweep(
        context.backlog_dao, now=_NOW,
        single_night_venue_ids=context.backlog_single_night_venue_ids,
    )


@when("an operator reads the dedup backlog report")
def step_when_operator_reads_report(context):
    context.backlog_rows_before = {
        eid: context.backlog_dao.get_event(eid) for eid in context.backlog_event_ids
    }
    response = _client(context).get("/admin/events/dedup-backlog")
    assert response.status_code == 200, response.text
    context.backlog_response = response.json()


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


@when("an operator reads the dedup backlog report limited to {limit_word} groups")
def step_when_operator_reads_report_limited(context, limit_word):
    limit = _WORD_NUMBERS[limit_word]
    response = _client(context).get(f"/admin/events/dedup-backlog?limit={limit}")
    assert response.status_code == 200, response.text
    context.backlog_response = response.json()


# ── Then ──────────────────────────────────────────────────────────────────
@then("the backlog reports one venue-night group")
def step_then_one_group(context):
    assert context.backlog.venue_night_group_count == 1, context.backlog.venue_night_groups


@then("the backlog reports two venue-night groups")
def step_then_two_groups(context):
    assert context.backlog.venue_night_group_count == 2, context.backlog.venue_night_groups


@then("the backlog reports no venue-night groups")
def step_then_no_groups(context):
    assert context.backlog.venue_night_group_count == 0, context.backlog.venue_night_groups


@then('the group names "{venue}" and that Saturday')
def step_then_group_names_venue_and_date(context, venue):
    group = context.backlog.venue_night_groups[0]
    assert group.venue_name == venue, group
    assert group.local_date == _SATURDAY, group


@then("the group lists the three titles")
def step_then_group_lists_three_titles(context):
    group = context.backlog.venue_night_groups[0]
    assert set(group.titles) == set(_THREE_ACTS), group.titles


@then("the backlog reports three excess rows")
def step_then_three_excess(context):
    assert context.backlog.venue_night_excess_rows == 3, context.backlog.venue_night_groups


@then("the backlog reports no excess rows")
def step_then_no_excess(context):
    assert context.backlog.venue_night_excess_rows == 0, context.backlog.venue_night_groups


@then("the backlog reports one pair refused as disjoint")
def step_then_one_refused_disjoint(context):
    assert context.backlog.refused_disjoint_pairs == 1, context.backlog


@then("the backlog reports one pair refused for having no distinctive tokens")
def step_then_one_refused_no_tokens(context):
    assert context.backlog.refused_no_distinctive_tokens_pairs == 1, context.backlog


@then("the backlog reports one pending merge suggestion")
def step_then_one_pending_suggestion(context):
    assert context.backlog.pending_suggestions == 1, context.backlog.pending_suggestions


@then('the backlog reports "{handle}" as an account with disputed attributions')
def step_then_account_disputed(context, handle):
    handles = {d.source_handle for d in context.backlog.attribution_disputes}
    assert handle in handles, context.backlog.attribution_disputes


@then('the backlog names "{location_text}" and the two events it would recover')
def step_then_names_location_and_two_events(context, location_text):
    matching = [
        d for d in context.backlog.attribution_disputes if d.location_text == location_text
    ]
    assert len(matching) == 1, context.backlog.attribution_disputes
    assert matching[0].row_count == 2, matching[0]
    assert len(matching[0].event_ids) == 2, matching[0]


@then('the backlog reports two distinct location texts for "{handle}"')
def step_then_two_distinct_location_texts(context, handle):
    texts = {
        d.location_text for d in context.backlog.attribution_disputes
        if d.source_handle == handle
    }
    assert len(texts) == 2, context.backlog.attribution_disputes
    context.backlog_dispute_texts = texts


@then("each location text carries its own row count")
def step_then_each_text_has_row_count(context):
    by_text = {d.location_text: d.row_count for d in context.backlog.attribution_disputes}
    assert by_text.get("CASA FORTE") == 2, by_text
    assert by_text.get("MADALENA") == 1, by_text


@then("the venue-night group gauge reports one")
def step_then_group_gauge_one(context):
    assert _gauge(MEASURE_VENUE_NIGHT_GROUPS) == 1, _gauge(MEASURE_VENUE_NIGHT_GROUPS)


@then("the excess row gauge reports two")
def step_then_excess_gauge_two(context):
    assert _gauge(MEASURE_VENUE_NIGHT_EXCESS_ROWS) == 2, _gauge(MEASURE_VENUE_NIGHT_EXCESS_ROWS)


@then("the excess row gauge reports fewer rows than before the sweep")
def step_then_excess_gauge_fell(context):
    after = _gauge(MEASURE_VENUE_NIGHT_EXCESS_ROWS)
    before = context.backlog_gauge_before
    assert before is not None, "the before-gauge was never recorded"
    assert after < before, (before, after)


@then("the response lists the venue-night group with its titles")
def step_then_response_lists_group(context):
    groups = context.backlog_response["venue_night_groups"]
    assert len(groups) == 1, groups
    assert set(groups[0]["titles"]) == set(_THREE_ACTS), groups[0]


@then("the response lists the refusal counts")
def step_then_response_lists_refusals(context):
    body = context.backlog_response
    assert "refused_disjoint_pairs" in body, body
    assert "refused_no_distinctive_tokens_pairs" in body, body
    assert body["refused_disjoint_pairs"] == 3, body


@then("the response lists the accounts with disputed attributions")
def step_then_response_lists_disputes(context):
    assert "attribution_disputes" in context.backlog_response, context.backlog_response


@then("the response lists {count_word} groups")
def step_then_response_lists_n_groups(context, count_word):
    count = _WORD_NUMBERS[count_word]
    groups = context.backlog_response["venue_night_groups"]
    assert len(groups) == count, len(groups)


@then("the response reports {count_word} groups in total")
def step_then_response_reports_total(context, count_word):
    count = _WORD_NUMBERS[count_word]
    assert context.backlog_response["venue_night_group_total"] == count, context.backlog_response


@then("every stored event is unchanged")
def step_then_every_event_unchanged(context):
    for event_id, before in context.backlog_rows_before.items():
        after = context.backlog_dao.get_event(event_id)
        assert after == before, (event_id, before, after)


@then("no merge suggestion is recorded")
def step_then_no_suggestion_recorded(context):
    assert context.backlog_dao.list_event_merge_suggestions() == []
