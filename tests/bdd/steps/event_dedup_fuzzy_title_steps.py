"""Behave steps for tests/bdd/enrichment/event-dedup-fuzzy-title.feature.

See plans/260812_event-dedup-fuzzy-title.md. Self-contained per scenario
(own fresh `InMemoryRdsVenueStore` + fakeredis-backed admin app), mirroring
`tests/bdd/steps/hide_promoter_events_steps.py`'s own pattern — this feature
only exercises the merge pass over events seeded directly through the DAO,
never a real extraction pipeline.

`event_dedup_auto_merge_enabled` defaults to True for every scenario here —
the shipped-disabled default (plan §C, "the six things most likely to go
wrong" #1) is a PRODUCTION posture pinned by
`tests/test_event_dedup.py::test_auto_merge_disabled_by_default`, not
something these behavioural scenarios re-litigate; they exist to prove what
the feature DOES once an operator turns it on.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.metrics import EVENT_MERGE_TOTAL
from app.models.event_kind import KIND_MENU
from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services import event_dedup
from app.services.event_identity import compute_source_event_key
from app.services.event_merge import compute_event_identity, merge_touched_events
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE = ZoneInfo("America/Recife")
_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

_DEFAULT_HANDLE = "dedup_test_handle"
_LINEUP_DEFAULT_VENUE = "Conchittas Bar"
_LINEUP_DEFAULT_TITLE = "Sextou"  # a single generic-vocab word -> always an empty distinctive set
_ELEVEN_PERFORMERS = [f"Performer {i}" for i in range(1, 12)]


def _local_dt(date_str: str, time_str: str = "12:00") -> datetime:
    hour, minute = (int(p) for p in time_str.split(":"))
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=RECIFE)


# ── context / fixture setup ─────────────────────────────────────────────────
def _ensure_context(context) -> None:
    if hasattr(context, "dedup_dao"):
        return
    context.dedup_dao = InMemoryRdsVenueStore()
    context.dedup_redis = fakeredis.FakeRedis(decode_responses=True)
    context.dedup_venues: dict[str, str] = {}
    context.dedup_event_ids: list[str] = []
    context.dedup_seq = 0
    context.dedup_client = None
    # plan §C: auto-merge ships disabled in production; these behavioural
    # scenarios exercise the feature with the operator's flag ON (see the
    # module docstring). Individual scenarios may override.
    context.dedup_redis.set(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, json.dumps(True))


def _ensure_venue(context, name: str) -> str:
    _ensure_context(context)
    if name not in context.dedup_venues:
        venue_id = f"dedup_venue_{len(context.dedup_venues) + 1}"
        context.dedup_dao.upsert_venue(
            Venue(venue_id=venue_id, venue_name=name, venue_lat=-8.05, venue_lng=-34.88)
        )
        context.dedup_venues[name] = venue_id
    return context.dedup_venues[name]


def _seed_item(
    context, title, venue_name, *, starts_at=None, source_handle=None, first_seen_at=None,
    post_type="event", lineup=None, status=None, title_edited=False, venue_edited=False,
) -> str:
    """`first_seen_at` defaults to a STRICTLY LATER moment than the
    previous `_seed_item` call in this scenario (never a shared constant) —
    matching the real pipeline, where each `Given`/`And` line is a
    separately-crawled post. `app.services.event_merge.merge_event_fields`
    breaks a genuine content disagreement (e.g. two different titles) by
    preferring whichever source was more recently seen — this is what lets
    a shortened title's fuller successor become the surviving title (see
    the "shortened title" scenario). Bulk fixtures (a Gherkin table, or the
    eight-row production cluster) pass an EXPLICIT, SHARED `first_seen_at`
    instead — representing rows discovered together in the same crawl —
    which keeps the canonical's OWN title from being overwritten by a later
    but unrelated absorption within that same batch."""
    _ensure_context(context)
    venue_id = _ensure_venue(context, venue_name)
    context.dedup_seq += 1
    seen_at = first_seen_at if first_seen_at is not None else _NOW + timedelta(seconds=context.dedup_seq)
    edited = []
    if title_edited:
        edited.append("title")
    if venue_edited:
        edited.append("venue_id")
    fields = {
        "event_id": new_event_id(), "venue_id": venue_id, "starts_at": starts_at,
        "title": title, "post_type": post_type, "lineup": lineup or [],
        "status": status or "pending_review",
        "source_kind": "venue_post", "source_handle": source_handle or _DEFAULT_HANDLE,
        "source_shortcode": f"dedup_sc_{context.dedup_seq}",
        "first_seen_at": seen_at, "last_seen_at": seen_at,
        "operator_edited_fields": edited or None,
    }
    context.dedup_dao.insert_event(fields)
    context.dedup_event_ids.append(fields["event_id"])
    return fields["event_id"]


def _alive(context, event_id: str) -> bool:
    row = context.dedup_dao.get_event(event_id)
    return row is not None and row.get("status") != "superseded"


def _survivors(context, ids=None) -> list:
    ids = ids if ids is not None else context.dedup_event_ids
    return [eid for eid in ids if _alive(context, eid)]


def _run_merge_pass(context) -> None:
    merge_touched_events(
        context.dedup_dao, list(context.dedup_event_ids), _NOW, redis_like=context.dedup_redis,
    )


def _build_client(context) -> TestClient:
    if context.dedup_client is not None:
        return context.dedup_client
    app = FastAPI()
    app.include_router(admin_events_router)
    set_events_container(type("C", (), {
        "pipeline_repository": context.dedup_dao, "redis_client": context.dedup_redis,
    })())
    context.dedup_client = TestClient(app)
    return context.dedup_client


def _sum_title_outcomes() -> float:
    total = 0.0
    for metric in EVENT_MERGE_TOTAL.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels.get("identity") == "title":
                total += sample.value
    return total


# ── Background ───────────────────────────────────────────────────────────
@given('the venue "{name}" exists')
def step_given_venue_exists(context, name):
    _ensure_venue(context, name)


@given("the candidate window is {hours:d} hours")
def step_given_candidate_window(context, hours):
    _ensure_context(context)
    context.dedup_redis.set(event_dedup.ADMIN_CONFIG_CANDIDATE_WINDOW_HOURS_KEY, json.dumps(hours))


@given(
    'the generic event vocabulary includes "{a}", "{b}", "{c}", "{d}" and "{e}"'
)
def step_given_generic_vocabulary(context, a, b, c, d, e):
    _ensure_context(context)
    context.dedup_redis.set(
        event_dedup.ADMIN_CONFIG_GENERIC_VOCABULARY_KEY, json.dumps([a, b, c, d, e]),
    )


# ── item fixtures: dated, local time ────────────────────────────────────────
@given('an item "{title}" at "{venue}" starting {date} {time} local, from the account "{handle}"')
def step_given_item_dated_local_with_account(context, title, venue, date, time, handle):
    _seed_item(context, title, venue, starts_at=_local_dt(date, time), source_handle=handle)


@given('an item "{title}" at "{venue}" starting {date} {time} local whose operator edited its title')
def step_given_item_dated_title_edited(context, title, venue, date, time):
    _seed_item(context, title, venue, starts_at=_local_dt(date, time), title_edited=True)


@given('an item "{title}" at "{venue}" starting {date} {time} local whose operator edited its venue')
def step_given_item_dated_venue_edited(context, title, venue, date, time):
    _seed_item(context, title, venue, starts_at=_local_dt(date, time), venue_edited=True)


@given('a confirmed item "{title}" at "{venue}" starting {date} {time} local')
def step_given_confirmed_item_dated(context, title, venue, date, time):
    context.dedup_confirmed_id = _seed_item(
        context, title, venue, starts_at=_local_dt(date, time), status="confirmed",
    )


@given('an item "{title}" at "{venue}" starting {date} {time} local')
def step_given_item_dated_local(context, title, venue, date, time):
    _seed_item(context, title, venue, starts_at=_local_dt(date, time))


# ── item fixtures: undated ──────────────────────────────────────────────────
@given('an item "{title}" at "{venue}" with no date, from the account "{handle}"')
def step_given_item_undated_with_account(context, title, venue, handle):
    _seed_item(context, title, venue, starts_at=None, source_handle=handle)


@given('an item "{title}" at "{venue}" with no date, first seen {date}')
def step_given_item_undated_first_seen(context, title, venue, date):
    first_seen = _local_dt(date, "00:00").astimezone(timezone.utc)
    _seed_item(context, title, venue, starts_at=None, first_seen_at=first_seen)


# ── item fixtures: date-only (§B2 lineup scenarios, non-event guard) ───────
@given('an item "{title}" at "{venue}" on {date} typed as "{post_type}"')
def step_given_item_on_date_typed(context, title, venue, date, post_type):
    _seed_item(context, title, venue, starts_at=_local_dt(date, "20:00"), post_type=post_type)


@given('an item "{title}" at "{venue}" on {date} with no lineup')
def step_given_item_on_date_no_lineup(context, title, venue, date):
    _seed_item(context, title, venue, starts_at=_local_dt(date, "20:00"), lineup=[])


@given('an item "{title}" at "{venue}" on {date} listing three performers')
def step_given_item_on_date_three_performers(context, title, venue, date):
    _seed_item(
        context, title, venue, starts_at=_local_dt(date, "20:00"),
        lineup=["Perf A", "Perf B", "Perf C"],
    )


@given('an item "{title}" at "{venue}" on {date}')
def step_given_item_on_date(context, title, venue, date):
    _seed_item(context, title, venue, starts_at=_local_dt(date, "20:00"))


# ── menu items (out of scope path) ──────────────────────────────────────────
@given('a menu item "{title}" at "{venue}" with no date')
def step_given_menu_item(context, title, venue):
    if not hasattr(context, "dedup_title_outcomes_before"):
        context.dedup_title_outcomes_before = _sum_title_outcomes()
    _seed_item(context, title, venue, starts_at=None, post_type=KIND_MENU)


# ── data tables ──────────────────────────────────────────────────────────────
@given('the following items at "{venue}":')
def step_given_table_of_items(context, venue):
    for row in context.table:
        raw = row["starts_at local"]
        date_str, time_str = raw.split(" ")
        _seed_item(
            context, row["title"], venue, starts_at=_local_dt(date_str, time_str), first_seen_at=_NOW,
        )


@given('the following items at "{venue}" starting {date} {time} local:')
def step_given_table_of_items_same_start(context, venue, date, time):
    starts_at = _local_dt(date, time)
    for row in context.table:
        _seed_item(context, row["title"], venue, starts_at=starts_at, first_seen_at=_NOW)


# ── lineup assignment on the last two seeded items ─────────────────────────
def _last_two(context):
    return context.dedup_event_ids[-2], context.dedup_event_ids[-1]


def _set_lineup(context, event_id: str, lineup: list) -> None:
    context.dedup_dao.update_event(event_id, {"lineup": lineup})


@given('both items list "{a}", "{b}" and "{c}"')
def step_given_both_items_list_three(context, a, b, c):
    ida, idb = _last_two(context)
    _set_lineup(context, ida, [a, b, c])
    _set_lineup(context, idb, [a, b, c])


@given("both items list the same eleven performers")
def step_given_both_items_list_eleven(context):
    ida, idb = _last_two(context)
    _set_lineup(context, ida, list(_ELEVEN_PERFORMERS))
    _set_lineup(context, idb, list(_ELEVEN_PERFORMERS))


@given('both items list only "{name}" in common')
def step_given_both_items_list_one_shared(context, name):
    ida, idb = _last_two(context)
    _set_lineup(context, ida, [name])
    _set_lineup(context, idb, [name])


# ── the shorthand "listing" fixtures (lineup-focused scenarios) ────────────
# Two DIFFERENT generic-only titles — both reduce to an EMPTY distinctive
# set (so only the lineup signal can drive these scenarios' outcome), but
# they must NOT be the same title: an identical (venue_id, date,
# normalized-title) tuple would collide on §A's EXACT identity, which runs
# BEFORE the fuzzy pass and merges unconditionally on identity alone,
# masking whatever the lineup rule alone would have decided.
_LINEUP_DEFAULT_TITLE_A = "Sextou"
_LINEUP_DEFAULT_TITLE_B = "Aniversário"


@given('an item listing "{a}" and "{b}"')
def step_given_item_listing_two(context, a, b):
    starts_at = _local_dt("2026-08-07", "21:00")
    _seed_item(context, _LINEUP_DEFAULT_TITLE_A, _LINEUP_DEFAULT_VENUE, starts_at=starts_at, lineup=[a, b])
    context.dedup_lineup_anchor = (_LINEUP_DEFAULT_VENUE, starts_at)


@given('a sibling item at the same venue and night listing "{a}" and "{b}"')
def step_given_sibling_item_listing(context, a, b):
    venue, starts_at = context.dedup_lineup_anchor
    _seed_item(context, _LINEUP_DEFAULT_TITLE_B, venue, starts_at=starts_at, lineup=[a, b])


# ── compound fixtures ────────────────────────────────────────────────────────
@given('a suggested merge between two items at "{venue}"')
def step_given_a_suggested_merge(context, venue):
    a = _seed_item(context, "Ação Leitura: Bate-papo com Marcelino Freire", venue, starts_at=_local_dt("2026-08-04", "19:00"))
    b = _seed_item(context, "Ação Leitura: Bate-papo com Jeferson Tenório", venue, starts_at=_local_dt("2026-08-04", "19:00"))
    _run_merge_pass(context)
    context.dedup_suggested_pair = (a, b)


@given("a title merge that absorbed one item into another")
def step_given_a_title_merge_absorbed(context):
    a = _seed_item(context, "Rodolpho", "Conchittas Bar", starts_at=_local_dt("2026-08-07", "21:00"))
    b = _seed_item(context, "Rodolpho Produções", "Conchittas Bar", starts_at=_local_dt("2026-08-07", "21:00"))
    _run_merge_pass(context)
    survivors = _survivors(context, [a, b])
    absorbed = [eid for eid in (a, b) if eid not in survivors]
    assert len(survivors) == 1 and len(absorbed) == 1, (survivors, absorbed)
    context.dedup_canonical_id = survivors[0]
    context.dedup_absorbed_id = absorbed[0]
    audit = context.dedup_dao.list_event_merge_suggestions(
        candidate_event_id=context.dedup_absorbed_id, decision="auto_merged",
    )
    assert audit, "no auto_merged audit row found for the absorbed item"
    context.dedup_moved_source_id = audit[-1]["moved_source_ids"][0]


@given("two items with the same venue, the same date and the same normalized title")
def step_given_two_exact_identity_items(context):
    a = _seed_item(context, "NOITE DA PATROA", "Conchittas Bar", starts_at=_local_dt("2026-08-07", "20:00"))
    b = _seed_item(context, "noite da patroa", "Conchittas Bar", starts_at=_local_dt("2026-08-07", "20:00"))
    context.dedup_exact_pair = (a, b)


# The production titles/dates from the plan's own Evidence section, and the
# `compute_source_event_key`/`compute_event_identity` outputs they hashed to
# BEFORE this feature shipped — computed against the UNMODIFIED functions
# (this plan never touches app.services.event_identity or
# app.services.event_merge.compute_event_identity; see those modules'
# docstrings) and pinned here as the regression guard.
_GOLDEN_IDENTITY = [
    ("Aniversário do RODOLPHO Produções", None,
     "e4c428c073ae89f683c2370d9f494f8b", None),
    ("Aniversário do Rodolpho Produções", _local_dt("2026-08-07", "19:00"),
     "7cbafd8f83fc8192c947abf18b2bbdc8", ("2026-08-07", "aniversario do rodolpho producoes")),
    ("31º Rodolpho Produções", _local_dt("2026-08-07", "19:00"),
     "2f63420039392cd17a4e060971be4958", ("2026-08-07", "31o rodolpho producoes")),
    ("Rodolpho", _local_dt("2026-08-07", "21:00"),
     "ef98422fb4e1efa1ed354981aefd1756", ("2026-08-07", "rodolpho")),
    ("Rodolpho Produções", _local_dt("2026-08-07", "21:00"),
     "b875558143e3d148cef2c99da5127aee", ("2026-08-07", "rodolpho producoes")),
    ("SEXTOU NO CONCHITTAS BAR!", _local_dt("2026-08-07", "21:00"),
     "27bed410da1d7d329f72f184071014e5", ("2026-08-07", "sextou no conchittas bar!")),
    ("Aniversário do Rodolpho Produções", _local_dt("2026-08-08", "00:00"),
     "05f442e2be13ae8bd98c9f672dd56794", ("2026-08-08", "aniversario do rodolpho producoes")),
    ("31 Anos", _local_dt("2026-08-08", "00:00"),
     "60a3fda16a87fc5faf55d9de008dac98", ("2026-08-08", "31 anos")),
]


@given("the production titles from the Conchittas cluster")
def step_given_production_titles(context):
    _ensure_context(context)
    context.dedup_identity_rows = _GOLDEN_IDENTITY


@given("the eight production rows of the Conchittas Rodolpho cluster")
def step_given_eight_production_rows(context):
    venue = "Conchittas Bar"
    handle = "conchittasbar"
    party_rows = [
        ("Aniversário do RODOLPHO Produções", None),
        ("Aniversário do Rodolpho Produções", _local_dt("2026-08-07", "19:00")),
        ("31º Rodolpho Produções", _local_dt("2026-08-07", "19:00")),
        ("Rodolpho", _local_dt("2026-08-07", "21:00")),
        ("Rodolpho Produções", _local_dt("2026-08-07", "21:00")),
        ("SEXTOU NO CONCHITTAS BAR!", _local_dt("2026-08-07", "21:00")),
        ("Aniversário do Rodolpho Produções", _local_dt("2026-08-08", "00:00")),
    ]
    context.dedup_cluster_party_ids = []
    for title, starts_at in party_rows:
        # All seven rows share ONE first_seen_at (representing production
        # rows discovered independently, with no stated relative crawl
        # order) — so a content disagreement between two absorbed titles is
        # broken in the canonical's own favor (merge_event_fields keeps the
        # canonical's value when recency is tied), matching the plan's own
        # expectation that the SURVIVING title is the earliest-inserted
        # (oldest-ULID) row's own, never a later absorption's.
        eid = _seed_item(context, title, venue, starts_at=starts_at, source_handle=handle, first_seen_at=_NOW)
        context.dedup_cluster_party_ids.append(eid)
    # The eleven shared performers rescue SEXTOU (empty distinctive title
    # set) — plan's own Evidence table.
    rodolpho_producoes_id = context.dedup_cluster_party_ids[4]
    sextou_id = context.dedup_cluster_party_ids[5]
    _set_lineup(context, rodolpho_producoes_id, list(_ELEVEN_PERFORMERS))
    _set_lineup(context, sextou_id, list(_ELEVEN_PERFORMERS))

    context.dedup_cluster_anos_id = _seed_item(
        context, "31 Anos", venue, starts_at=_local_dt("2026-08-08", "00:00"), source_handle=handle,
        first_seen_at=_NOW,
    )


# ── When ─────────────────────────────────────────────────────────────────
@when("the merge pass runs")
def step_when_merge_pass_runs(context):
    _run_merge_pass(context)


@when("the merge pass runs again")
def step_when_merge_pass_runs_again(context):
    _run_merge_pass(context)


@when("the review queue is read")
def step_when_review_queue_is_read(context):
    client = _build_client(context)
    context.dedup_review_response = client.get("/admin/events/review")
    assert context.dedup_review_response.status_code == 200, context.dedup_review_response.text


@when("an operator applies the suggestion")
def step_when_operator_applies_suggestion(context):
    ida, idb = context.dedup_suggested_pair
    pending = [
        s for s in context.dedup_dao.list_event_merge_suggestions(event_id=ida, decision="pending")
        if idb in (s["event_id"], s["candidate_event_id"])
    ]
    assert pending, "no pending suggestion found for the seeded pair"
    client = _build_client(context)
    response = client.post(f"/admin/events/merge-suggestions/{pending[0]['suggestion_id']}/apply")
    assert response.status_code == 200, response.text
    canonical_id = response.json()["event_id"]
    absorbed_id = idb if canonical_id == ida else ida
    context.dedup_canonical_id = canonical_id
    context.dedup_absorbed_id = absorbed_id


@when("an operator reverses the merge")
def step_when_operator_reverses_merge(context):
    client = _build_client(context)
    response = client.post(f"/admin/events/{context.dedup_absorbed_id}/reverse-merge")
    assert response.status_code == 200, response.text


@when("each title's content identity key is computed")
def step_when_identity_keys_computed(context):
    venue_id = _ensure_venue(context, "Conchittas Bar")
    context.dedup_computed_identity = [
        (title, compute_source_event_key(title, starts_at), compute_event_identity(venue_id, starts_at, title))
        for title, starts_at, _, _ in context.dedup_identity_rows
    ]


# ── Then: counts and survivors ──────────────────────────────────────────────
@then('one item remains at "{venue}" for that night')
def step_then_one_item_remains_at_venue(context, venue):
    venue_id = context.dedup_venues[venue]
    at_venue = [eid for eid in context.dedup_event_ids if context.dedup_dao.get_event(eid) and context.dedup_dao.get_event(eid).get("venue_id") == venue_id]
    survivors = _survivors(context, at_venue)
    assert len(survivors) == 1, survivors
    context.dedup_survivor = survivors[0]


@then('exactly one item remains for that night at "{venue}"')
def step_then_exactly_one_item_remains(context, venue):
    step_then_one_item_remains_at_venue(context, venue)


@then("one item remains")
def step_then_one_item_remains(context):
    survivors = _survivors(context)
    assert len(survivors) == 1, survivors
    context.dedup_survivor = survivors[0]


@then('the surviving item is titled "{title}"')
def step_then_surviving_item_titled(context, title):
    row = context.dedup_dao.get_event(context.dedup_survivor)
    assert row["title"] == title, row["title"]


@then("the surviving item carries both source posts")
def step_then_surviving_item_carries_both(context):
    sources = context.dedup_dao.list_event_sources(context.dedup_survivor)
    assert len(sources) == 2, sources


@then("the surviving item has the date {date}")
def step_then_surviving_item_has_date(context, date):
    row = context.dedup_dao.get_event(context.dedup_survivor)
    assert row["starts_at"] is not None
    assert row["starts_at"].astimezone(RECIFE).date().isoformat() == date, row["starts_at"]


@then("that item carries all five source posts")
def step_then_carries_all_five(context):
    sources = context.dedup_dao.list_event_sources(context.dedup_survivor)
    assert len(sources) == 5, sources


@then("the two items are merged")
def step_then_two_items_merged(context):
    ids = context.dedup_event_ids[-2:]
    survivors = _survivors(context, ids)
    assert len(survivors) == 1, survivors


@then("the two items are not merged")
def step_then_two_items_not_merged(context):
    ids = context.dedup_event_ids[-2:]
    survivors = _survivors(context, ids)
    assert len(survivors) == 2, survivors


@then("the two items are not merged by the lineup rule")
def step_then_two_items_not_merged_by_lineup(context):
    step_then_two_items_not_merged(context)


@then("all three items remain")
def step_then_all_three_remain(context):
    ids = context.dedup_event_ids[-3:]
    survivors = _survivors(context, ids)
    assert len(survivors) == 3, survivors


@then("all three items remain unchanged")
def step_then_all_three_remain_unchanged(context):
    step_then_all_three_remain(context)


@then("all four items remain")
def step_then_all_four_remain(context):
    ids = context.dedup_event_ids[-4:]
    survivors = _survivors(context, ids)
    assert len(survivors) == 4, survivors


@then("both items remain")
def step_then_both_items_remain(context):
    step_then_two_items_not_merged(context)


@then("both items still remain")
def step_then_both_items_still_remain(context):
    ida, idb = context.dedup_suggested_pair
    assert _alive(context, ida) and _alive(context, idb)


# ── Then: suggestions ────────────────────────────────────────────────────────
def _pending_pair_suggestions(context, ida, idb):
    return [
        s for s in context.dedup_dao.list_event_merge_suggestions(event_id=ida, decision="pending")
        if idb in (s["event_id"], s["candidate_event_id"])
    ]


@then("no merge is suggested for any pair of them")
def step_then_no_merge_suggested_for_any_pair(context):
    for eid in context.dedup_event_ids[-3:]:
        assert context.dedup_dao.list_event_merge_suggestions(event_id=eid, decision="pending") == []


@then("no merge is suggested for that pair")
def step_then_no_merge_suggested_for_pair(context):
    ida, idb = context.dedup_event_ids[-2:]
    assert _pending_pair_suggestions(context, ida, idb) == []


@then("a merge is suggested for that pair")
def step_then_merge_suggested_for_pair(context):
    ida, idb = context.dedup_event_ids[-2:]
    suggestions = _pending_pair_suggestions(context, ida, idb)
    assert len(suggestions) == 1, suggestions
    context.dedup_last_suggestion = suggestions[0]


@then("the suggestion states each title's distinctive words")
def step_then_suggestion_states_distinctive_words(context):
    s = context.dedup_last_suggestion
    assert s["event_distinctive_words"], s
    assert s["candidate_distinctive_words"], s


@then("each of the two items carries the suggestion")
def step_then_each_item_carries_suggestion(context):
    ida, idb = context.dedup_suggested_pair
    body = context.dedup_review_response.json()
    by_id = {item["event_id"]: item for item in body}
    assert ida in by_id and idb in by_id, body
    assert by_id[ida]["merge_suggestions"], by_id[ida]
    assert by_id[idb]["merge_suggestions"], by_id[idb]
    context.dedup_review_by_id = by_id


@then("each suggestion names the other item")
def step_then_each_suggestion_names_other(context):
    ida, idb = context.dedup_suggested_pair
    by_id = context.dedup_review_by_id
    a_suggestion = by_id[ida]["merge_suggestions"][0]
    b_suggestion = by_id[idb]["merge_suggestions"][0]
    assert a_suggestion["other_event_id"] == idb, a_suggestion
    assert b_suggestion["other_event_id"] == ida, b_suggestion


@then("the merge records shared lineup as its reason")
def step_then_merge_records_shared_lineup(context):
    ida, idb = context.dedup_event_ids[-2:]
    canonical = ida if _alive(context, ida) else idb
    suggestions = context.dedup_dao.list_event_merge_suggestions(event_id=canonical, decision="auto_merged")
    assert suggestions, "no auto_merged record found"
    assert event_dedup.REASON_LINEUP in (suggestions[-1].get("reasons") or []), suggestions[-1]


# ── Then: undated absorption / menu path ────────────────────────────────────
@then("the title similarity path considered neither item")
def step_then_title_path_considered_neither(context):
    ids = context.dedup_event_ids[-2:]
    for eid in ids:
        assert _alive(context, eid)
        assert context.dedup_dao.list_event_merge_suggestions(event_id=eid) == []
    after = _sum_title_outcomes()
    assert after == context.dedup_title_outcomes_before, (after, context.dedup_title_outcomes_before)


# ── Then: operator protections ──────────────────────────────────────────────
@then("the confirmed item still exists")
def step_then_confirmed_item_still_exists(context):
    row = context.dedup_dao.get_event(context.dedup_confirmed_id)
    assert row is not None, "confirmed item was deleted"
    context.dedup_confirmed_row_after = row


@then("the confirmed item is still confirmed")
def step_then_confirmed_item_still_confirmed(context):
    assert context.dedup_confirmed_row_after["status"] == "confirmed", context.dedup_confirmed_row_after


# ── Then: reversibility and audit ───────────────────────────────────────────
@then("the absorbed item is still readable")
def step_then_absorbed_item_still_readable(context):
    ida, idb = context.dedup_event_ids[-2:]
    absorbed_id = ida if not _alive(context, ida) else idb
    row = context.dedup_dao.get_event(absorbed_id)
    assert row is not None, "absorbed row was deleted, not superseded"
    context.dedup_absorbed_id = absorbed_id
    context.dedup_canonical_id = idb if absorbed_id == ida else ida


@then("the absorbed item is superseded")
def step_then_absorbed_item_superseded(context):
    row = context.dedup_dao.get_event(context.dedup_absorbed_id)
    assert row["status"] == "superseded", row


@then("the absorbed item records the item that absorbed it")
def step_then_absorbed_item_records_absorber(context):
    row = context.dedup_dao.get_event(context.dedup_absorbed_id)
    assert row.get("superseded_by") == context.dedup_canonical_id, row


@then("the absorbed item's source posts are attached to the surviving item")
def step_then_absorbed_sources_attached_to_survivor(context):
    absorbed_sources = context.dedup_dao.list_event_sources(context.dedup_absorbed_id)
    assert absorbed_sources == [], absorbed_sources
    canonical_sources = context.dedup_dao.list_event_sources(context.dedup_canonical_id)
    assert len(canonical_sources) >= 2, canonical_sources


@then("the absorbed item is no longer superseded")
def step_then_absorbed_item_no_longer_superseded(context):
    row = context.dedup_dao.get_event(context.dedup_absorbed_id)
    assert row["status"] != "superseded", row
    assert row.get("superseded_by") is None, row


@then("the absorbed item carries its own source post again")
def step_then_absorbed_item_carries_own_source(context):
    sources = context.dedup_dao.list_event_sources(context.dedup_absorbed_id)
    assert len(sources) == 1, sources


@then("the previously surviving item no longer carries that source post")
def step_then_survivor_no_longer_carries_source(context):
    sources = context.dedup_dao.list_event_sources(context.dedup_canonical_id)
    source_ids = {s["id"] for s in sources}
    assert context.dedup_moved_source_id not in source_ids, sources


@then("the absorbed item is deleted, not superseded")
def step_then_absorbed_item_deleted(context):
    ida, idb = context.dedup_exact_pair
    absorbed_id = ida if not _alive(context, ida) else idb
    row = context.dedup_dao.get_event(absorbed_id)
    assert row is None, row


@then("the two items are merged by the exact identity rule")
def step_then_merged_by_exact_identity(context):
    ida, idb = context.dedup_exact_pair
    survivors = _survivors(context, [ida, idb])
    assert len(survivors) == 1, survivors


# ── Then: identity unchanged ─────────────────────────────────────────────────
@then("every key equals the key stored for it before this feature shipped")
def step_then_identity_keys_unchanged(context):
    golden_by_title = {}
    for title, starts_at, golden_key, golden_identity_tail in context.dedup_identity_rows:
        golden_by_title[(title, starts_at)] = (golden_key, golden_identity_tail)
    for title, key, identity in context.dedup_computed_identity:
        matching = [
            (g_key, g_tail) for (g_title, g_starts_at), (g_key, g_tail) in golden_by_title.items()
            if g_title == title
        ]
        assert matching, title
        # A title may repeat (e.g. two "Aniversário..." rows on different
        # dates) — match by key, which is unique per (title, date) pair.
        assert any(key == g_key for g_key, _ in matching), (title, key, matching)
        if identity is not None:
            _, date_str, normalized = None, identity[1].isoformat(), identity[2]
            assert any(
                g_tail is not None and g_tail == (date_str, normalized) for _, g_tail in matching
            ), (title, identity, matching)
        else:
            assert any(g_tail is None for _, g_tail in matching), (title, matching)


# ── Then: the whole cluster ───────────────────────────────────────────────
@then('the seven event rows survive as one item titled "{title}"')
def step_then_seven_rows_survive_as_one(context, title):
    survivors = _survivors(context, context.dedup_cluster_party_ids)
    assert len(survivors) == 1, survivors
    row = context.dedup_dao.get_event(survivors[0])
    assert row["title"] == title, row["title"]
    context.dedup_survivor = survivors[0]


@then("that item carries every source post of the seven")
def step_then_carries_every_source_of_seven(context):
    sources = context.dedup_dao.list_event_sources(context.dedup_survivor)
    assert len(sources) == 7, sources


@then('"31 Anos" is still a separate item')
def step_then_anos_still_separate(context):
    assert _alive(context, context.dedup_cluster_anos_id)
    assert context.dedup_cluster_anos_id != context.dedup_survivor
