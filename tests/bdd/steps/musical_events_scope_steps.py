"""Behave steps for tests/bdd/enrichment/musical-events-scope.feature.

See plans/260914_musical-events-scope.md. This feature proves TWO
independent, both-necessary gates a post's `category` drives:

1. The agentic-cost gate — `EventDisplayTitleService.run_for_events` and
   `EventVenueAdvisorService.run_for_events` must skip a deny-listed-category
   row before any OpenAI call, exercised here with fake model clients
   (never a live call — this repo's CLAUDE.md forbids that in BDD) that
   count their own invocations.
2. The serving-projection gate — `is_selectable`
   (`app.services.event_projection_selection`), reached only through the
   REAL `RedisProjectionService.project_events()` (never a hand-built
   projection key), so an admin-config edit's live effect is proven the
   same way `hide_promoter_events`'s own BDD scenarios are.

Reuses the SHARED per-scenario RDS/Redis harness `environment.py` already
builds (`context.rds_store`, `context.fake_redis`,
`context.redis_projection_service`, `context.redis_only_dao`) — the same
harness `events_serving_projection_steps.py` exercises — plus that file's
own `_venue_id_for`/`_override_setting` helpers (a plain Python import of
another steps module; behave's own step registry treats a re-registration
of the identical (pattern, function) pair as a no-op, not an
`AmbiguousStep` — see `StepRegistry.add_step_definition`'s "This may occur
when a step module imports another one" case, empirically confirmed safe
against this exact codebase via `--dry-run`).

Both agentic-pass admin-config flags (`event_display_title_enabled`,
`event_venue_advisor_enabled`) are turned on directly by this file's own
harness setup, never via a Gherkin step — the feature file's own Background
is only about the deny-list, and each flag's OWN on/off behaviour is already
covered by its own feature file
(`events-venue-night-duplication-display-title.feature`,
`dedup-agentic-mitigation-discovery.feature`); re-deriving that here would
just be duplicate coverage under a different name.

The venue-link-audit-reviewer regression-guard scenario reuses the whole
`agentic_venue_resolution_fallback_steps.py` harness (`_ensure`, `_seed_case`,
`_raw_from_case`, `_DEFAULT_CASE_ID`) rather than building a second one: that
file already proves the reviewer's real shape (a flagged handle/venue pair,
sourced from the real audit, given a programmable model stub), and "the
reviewer runs over the audit's flagged pairs" is already bound there — this
file only adds the one category-flavoured Given/Then this scenario needs.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import parse as _parse
from behave import given, register_type, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import Venue
from app.routers.admin_events_router import router as _admin_events_router
from app.routers.admin_events_router import set_container as _set_events_container
from app.services.event_display_title import (
    ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY,
    EventDisplayTitleService,
)
from app.services.event_venue_advisor import (
    ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY,
    EventVenueAdvisorService,
)
from tests.bdd.steps.events_serving_projection_steps import (
    _DEFAULT_VENUE as _EVS_DEFAULT_VENUE,
    _override_setting,
    _venue_id_for,
)

_NOW = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)

# The plan's own seeded default (Desired Behavior) — restated here as a
# plain literal, deliberately NOT imported from app.models.post_category:
# the "is not on the deny-list" premise-check step must stay meaningful
# (and reach a genuine, buildable red) even before that module carries the
# new constant.
_PLAN_DEFAULT_DENY_LIST = (
    "kids / family", "workshop", "food festival", "tasting",
    "sports screening", "comedy", "quiz / trivia",
)
_NON_MUSIC_CATEGORIES_KEY = "admin_config:event_non_music_categories"


@_parse.with_pattern(r'[^"]*')
def _parse_mes_quoted(text):
    """A quoted field that may be empty (`""`) — parse's default `{}` type
    requires 1+ characters, which never matches this feature's blank/null
    category Examples rows. Same shape as
    `tests/bdd/steps/blocked_venues_steps.py`'s own `MaybeEmpty` type."""
    return text


register_type(MesQuoted=_parse_mes_quoted)


# ── fake model clients (never a live OpenAI call) ──────────────────────────
class _FakeTitleModel:
    def __init__(self) -> None:
        self.calls: list = []

    async def pick_display_title(self, *, venue_name, local_date, source_titles, lineup):
        self.calls.append(list(source_titles))
        # One of the group's own source titles — trivially a subset of the
        # group's own distinctive tokens, so it always clears
        # validate_display_title; what matters here is only whether the
        # call happened at all.
        return json.dumps({"display_title": source_titles[0]})


class _FakeAdvisorModel:
    def __init__(self) -> None:
        self.calls: list = []

    async def recommend_event_venue(self, *, location_text, caption, candidates):
        self.calls.append(location_text)
        return json.dumps({
            "venue_id": candidates[0]["venue_id"],
            # Verbatim substring of every touched-event fixture's own
            # location_text below — satisfies the validator's evidence
            # check; what matters here is only whether the call happened.
            "evidence_quote": "Bar do Zé",
        })


# ── harness ──────────────────────────────────────────────────────────────
def _ensure(context) -> None:
    if hasattr(context, "mes_title_model"):
        return
    context.mes_title_model = _FakeTitleModel()
    context.mes_advisor_model = _FakeAdvisorModel()
    context.mes_touched_event_id = None
    context.mes_category = None
    context.mes_display_counts: dict = {}
    context.mes_advisor_counts: dict = {}
    context.mes_proj_event_id = None
    context.mes_admin_http = None
    # Both agentic passes ON for this whole feature — see module docstring.
    context.fake_redis.set(ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY, json.dumps(True))
    context.fake_redis.set(ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY, json.dumps(True))
    # The serving projection is OFF by default (app/config.py); this
    # feature's own selection scenarios need it on, mirroring
    # events_serving_projection_steps.py's own Background step.
    _override_setting(context, "events_projection_enabled", True)


# ── Background ──────────────────────────────────────────────────────────
@given("the event non-music category deny-list is at its default")
def step_given_deny_list_default(context):
    _ensure(context)
    # No admin override written: load_non_music_categories (once it exists)
    # falls back to the shipped defaults, exactly like every sibling loader
    # in this module family when its key is absent.


# ── touched-event fixtures (agentic-cost gate) ─────────────────────────────
def _base_touched_event_fields(category: str) -> dict:
    return {
        "venue_id": None, "location_resolution": None,
        "starts_at": _NOW + timedelta(hours=2), "title": "Some Night",
        "post_type": "event", "status": "accepted", "lineup": [],
        "location_text": "Show no Bar do Zé, Boa Viagem",
        "category": category or None,
    }


@given('a touched event whose category is "{category:MesQuoted}"')
def step_given_touched_event_category(context, category):
    _ensure(context)
    context.mes_category = category or None
    event_id = uuid.uuid4().hex
    context.rds_store.insert_event({
        "event_id": event_id,
        "source_kind": "venue_post", "source_handle": "mes_handle_a",
        "source_shortcode": f"mes_a_{event_id}",
        "raw_extraction": {"title": "ROWKA"},
        "first_seen_at": _NOW, "last_seen_at": _NOW,
        **_base_touched_event_fields(category),
    })
    context.mes_touched_event_id = event_id


@given("that event has two source titles and no obvious canonical pick")
def step_given_two_source_titles(context):
    event_id = context.mes_touched_event_id
    # A second events.post_item_source row for the SAME event, via the
    # public insert_event API (its event-row fields fully overwrite the
    # first call's identical values; a NEW source row is always appended —
    # see InMemoryRdsVenueStore.insert_event's own docstring). "ROWKA" and
    # "VITINHO POLÊMICO" are the exact two production strings this
    # pipeline's own display-title fixtures already use for "two
    # mutually-non-comparable titles -> no obvious pick, one model call"
    # (tests/bdd/steps/events_venue_night_duplication_display_title_steps.py's
    # FIVE_ACTS).
    context.rds_store.insert_event({
        "event_id": event_id,
        "source_kind": "venue_post", "source_handle": "mes_handle_b",
        "source_shortcode": f"mes_b_{event_id}",
        "raw_extraction": {"title": "VITINHO POLÊMICO"},
        "first_seen_at": _NOW + timedelta(minutes=1),
        "last_seen_at": _NOW + timedelta(minutes=1),
        **_base_touched_event_fields(context.mes_category),
    })


@given("that event is queued for venue resolution with candidate venues")
def step_given_queued_with_candidates(context):
    event_id = context.mes_touched_event_id
    venue_id = f"mes_venue_{event_id[:10]}"
    context.rds_store.upsert_venue(Venue(
        venue_id=venue_id, venue_name="Bar do Zé", venue_lat=-8.05, venue_lng=-34.88,
    ))
    context.rds_store.replace_event_venue_link_candidates(event_id, [
        {"venue_id": venue_id, "rank": 1, "score": 0.6, "method": "name_match", "evidence": {}},
    ])


# ── When: the agentic passes ───────────────────────────────────────────────
def _run_display_title(context) -> dict:
    service = EventDisplayTitleService(
        context.rds_store, context.mes_title_model, redis_client=context.fake_redis,
    )
    counts = asyncio.run(service.run_for_events([context.mes_touched_event_id]))
    context.mes_display_counts = counts
    return counts


def _run_venue_advisor(context) -> dict:
    service = EventVenueAdvisorService(
        context.rds_store, context.mes_advisor_model, redis_client=context.fake_redis,
    )
    counts = asyncio.run(service.run_for_events([context.mes_touched_event_id]))
    context.mes_advisor_counts = counts
    return counts


@when("the display-title pass runs for the touched events")
def step_when_display_title_pass_runs(context):
    _run_display_title(context)


@when("the venue-advisor pass runs for the touched events")
def step_when_venue_advisor_pass_runs(context):
    _run_venue_advisor(context)


@when("the display-title pass and the venue-advisor pass run for the touched events")
def step_when_both_passes_run(context):
    _run_display_title(context)
    _run_venue_advisor(context)


# ── Then: the agentic passes ───────────────────────────────────────────────
@then("no display-title model call is charged for that event")
def step_then_no_display_title_call(context):
    assert context.mes_title_model.calls == [], context.mes_title_model.calls


@then("no venue-advisor model call is charged for that event")
def step_then_no_venue_advisor_call(context):
    assert context.mes_advisor_model.calls == [], context.mes_advisor_model.calls


@then("a display-title model call is charged for that event")
def step_then_display_title_call_charged(context):
    assert len(context.mes_title_model.calls) == 1, context.mes_title_model.calls


@then("a venue-advisor model call is charged for that event")
def step_then_venue_advisor_call_charged(context):
    assert len(context.mes_advisor_model.calls) == 1, context.mes_advisor_model.calls


@then('that event\'s recorded outcome is "{outcome}"')
def step_then_recorded_outcome(context, outcome):
    counts = {**context.mes_display_counts, **context.mes_advisor_counts}
    assert counts.get(outcome) == 1, (
        outcome, context.mes_display_counts, context.mes_advisor_counts,
    )


# ── projection-selection fixtures (the serving gate) ────────────────────────
@given('an event whose category is "{category:MesQuoted}"')
def step_given_projection_event_category(context, category):
    _ensure(context)
    context.mes_category = category or None


@given(
    "that event has a resolved venue, an accepted status, no superseding row, "
    "and a current start time"
)
def step_given_event_fully_selectable(context):
    venue_id = _venue_id_for(context, _EVS_DEFAULT_VENUE)
    event_id = uuid.uuid4().hex
    context.rds_store.insert_event({
        "event_id": event_id, "venue_id": venue_id, "status": "accepted",
        "post_type": "event", "superseded_by": None,
        "starts_at": _NOW + timedelta(hours=2), "time_known": True,
        "category": context.mes_category,
        "source_handle": "mes_proj_handle", "source_shortcode": f"mes_proj_{event_id}",
        "first_seen_at": _NOW, "last_seen_at": _NOW,
    })
    context.mes_proj_event_id = event_id


@given('"{category:MesQuoted}" is not on the event non-music category deny-list')
def step_given_category_not_on_deny_list(context, category):
    # A Given establishes state, not merely asserts it (standard Gherkin
    # idiom): this writes an admin-config override — the plan's own default
    # list minus `category` — so the live-edit scenario has a real, provable
    # "before" state to edit away from, independent of whether `category`
    # happens to already be on the shipped default.
    reduced = [c for c in _PLAN_DEFAULT_DENY_LIST if c != category]
    context.fake_redis.set(_NON_MUSIC_CATEGORIES_KEY, json.dumps(reduced))


@when("the serving projection selects events")
@when("the serving projection selects events again")
def step_when_projection_selects_events(context):
    context.mes_summary = context.redis_projection_service.project_events(now=_NOW)


@when('an operator adds "{category:MesQuoted}" to the event non-music category deny-list')
def step_when_operator_adds_to_deny_list(context, category):
    updated = list(_PLAN_DEFAULT_DENY_LIST) + [category]
    context.fake_redis.set(_NON_MUSIC_CATEGORIES_KEY, json.dumps(updated))


@then("that event is selected")
def step_then_event_is_selected(context):
    occ = context.redis_only_dao.get_event_occurrence(context.mes_proj_event_id)
    assert occ is not None, "expected the event to be projected"


@then("that event is not selected")
def step_then_event_is_not_selected(context):
    occ = context.redis_only_dao.get_event_occurrence(context.mes_proj_event_id)
    assert occ is None, "expected the event NOT to be projected"


# ── admin console visibility ────────────────────────────────────────────────
@given(
    'an event whose category is "{category:MesQuoted}" and is excluded from '
    "the serving projection"
)
def step_given_event_excluded_from_projection(context, category):
    _ensure(context)
    venue_id = _venue_id_for(context, _EVS_DEFAULT_VENUE)
    event_id = uuid.uuid4().hex
    context.rds_store.insert_event({
        "event_id": event_id, "venue_id": venue_id, "status": "accepted",
        "post_type": "event", "superseded_by": None,
        "starts_at": _NOW + timedelta(hours=2), "time_known": True,
        "category": category or None,
        "source_handle": "mes_admin_handle", "source_shortcode": f"mes_admin_{event_id}",
        "first_seen_at": _NOW, "last_seen_at": _NOW,
    })
    context.mes_proj_event_id = event_id
    # Establishes the "excluded" state for real, the same way every other
    # projection scenario in this feature does — never a hand-set flag.
    context.redis_projection_service.project_events(now=_NOW)


def _admin_http(context) -> TestClient:
    if context.mes_admin_http is None:
        app = FastAPI()
        app.include_router(_admin_events_router)
        _set_events_container(type("C", (), {
            "pipeline_repository": context.rds_store,
            "redis_client": context.fake_redis,
        })())
        context.mes_admin_http = TestClient(app)
    return context.mes_admin_http


@when("an operator lists events in the admin console")
def step_when_operator_lists_events(context):
    response = _admin_http(context).get("/admin/events")
    assert response.status_code == 200, response.text
    context.mes_admin_listing = response.json()


@then("that event still appears in the admin console listing")
def step_then_event_in_admin_listing(context):
    ids = {row["event_id"] for row in context.mes_admin_listing}
    assert context.mes_proj_event_id in ids, (context.mes_proj_event_id, ids)


# ── the venue-link-audit reviewer is unaffected (regression guard) ─────────
@given(
    "a flagged handle-venue pair whose corroborating events are all "
    "deny-listed-category events"
)
def step_given_flagged_pair_all_deny_listed_category(context):
    from tests.bdd.steps.agentic_venue_resolution_fallback_steps import (
        ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY,
        _DEFAULT_CASE_ID,
        _ensure as _vlr_ensure,
        _raw_from_case,
        _seed_case,
    )

    _vlr_ensure(context)
    context.vlr_redis.set(ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY, json.dumps(True))

    def _hook(_index, _location_text):
        return {"category": "kids / family"}

    case, handle, venue_ids = _seed_case(context, _DEFAULT_CASE_ID, event_hook=_hook)
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)


@then("a model call is still charged for that pair")
def step_then_model_call_still_charged(context):
    assert context.vlr_stub.calls >= 1, context.vlr_stub.calls
