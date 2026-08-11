"""Behave steps for tests/bdd/enrichment/menu-item-lifecycle.feature.

See plans/260811_menu-item-lifecycle.md. Drives the REAL
`EventExtractionService` (+ its automatic `app.services.event_merge` call
after every post — now dispatching to `app.services.event_merge.
compute_menu_identity` for `post_type == "menu"` rows) over the SAME
`context.ee_*` harness `instagram_event_extraction_steps.py` already built —
the Background ("the event extraction pipeline is configured for a known
venue", registered by `event_ticket_info_and_attractions_steps.py`) and
`_add_post`/`_add_handle_post`/`_run_extraction`/`_stored_event`/
`_extraction_json` are reused verbatim, the same pattern every sibling
extraction feature file in this suite already follows. "When the post is
extracted" (singular) and "When the posts are extracted" (plural) are
ALREADY registered (event_ticket_info_and_attractions_steps.py and
date_correctness_and_path_parity_steps.py respectively) — reused, never
redefined. "that item awaits a human decision" is likewise already
registered by post_items_and_categories_steps.py and reused unmodified.

Two scenarios need machinery beyond a single-venue extraction, mirroring
post_items_and_categories_steps.py's own two precedents:
  - "Keep the same dish at two venues apart" adds a SECOND, independently
    resolved venue+handle+post.
  - "Leave an unresolved menu item unmerged" needs a genuinely UNRESOLVED
    venue_id — the "handles" eligibility mode's multi-venue branch, a second
    venue sharing the Background venue's handle with a `location_text`
    naming neither, exactly post_items_and_categories_steps.py's own
    unresolved-venue Given.

The expiry scenarios manipulate `context.ee_now` (already the extraction
harness's own now-override seam) to place a menu item's `last_seen_at` in
the past, then reset it to `None` (real wall-clock) before reading — the
admin API's derived `is_current` is computed against real
`datetime.now(timezone.utc)`, never against `context.ee_now`, so an
absolute past timestamp is what actually exercises it.

`_build_menu_admin_app` is a small, deliberate LOCAL duplicate of
instagram_event_extraction_steps.py's own `_build_admin_events_app` (never
imported) — it needs to attach an optional `redis_client` to the container
for the "changed expiry window" scenario's live admin-config read, which the
shared helper does not support and should not gain just for this one file.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.instagram import VenueInstagram
from app.models.menu_lifecycle import ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY
from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from tests.bdd.steps.instagram_event_extraction_steps import (
    RECIFE_LAT,
    RECIFE_LNG,
    _add_handle_post,
    _add_post,
    _extraction_json,
    _run_extraction,
    _stored_event,
)

_TS = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
# The default window (app.models.menu_lifecycle.DEFAULT_MENU_EXPIRY_DAYS) is
# 365 days — comfortably inside/outside margins for "this month" vs "over a
# year unseen", never near the boundary (boundary precision is pytest's job,
# see tests/test_menu_item_lifecycle.py).
_RECENT_DAYS_AGO = 5
_STALE_DAYS_AGO = 400


def _build_menu_admin_app(context) -> None:
    attrs: dict = {"pipeline_repository": context.ee_dao}
    fake_redis = getattr(context, "ml_fake_redis", None)
    if fake_redis is not None:
        attrs["redis_client"] = fake_redis
    app = FastAPI()
    app.include_router(admin_events_router)
    set_events_container(type("C", (), attrs)())
    context.ee_client = TestClient(app)


def _menu_items(context, venue_id=None) -> list[dict]:
    vid = venue_id or context.ee_venue_id
    return [e for e in context.ee_dao.list_events(venue_id=vid) if e.get("post_type") == "menu"]


_DISH_TITLE = "Prato Executivo"


def _menu_json(**overrides) -> str:
    """A menu-item extraction — no date_text/time_text, matching the plan's
    own production evidence ("Especial do dia": starts_at NULL, is_recurring
    true) and, just as importantly, keeping `compute_event_identity` (venue
    + DATE + title) structurally unable to match two of these: with no
    date, the pre-existing venue-identity merge path returns `None` for
    every menu post exactly as it does today (see the plan's own "Evidence"
    section) — so any merge these scenarios observe can only be
    `compute_menu_identity`'s doing, never an accidental collision with the
    unrelated, unmodified event path."""
    payload = dict(kind="menu", title=_DISH_TITLE)
    payload.update(overrides)
    return _extraction_json(**payload)


def _seed_menu_item(context, shortcode: str, *, days_ago: int, status: str = "accepted", **overrides) -> str:
    """Seed a menu item DIRECTLY through the DAO, already `last_seen_at`
    `days_ago` days in the past — the "already posted, some time ago"
    precondition several scenarios need, without running a real extraction
    for it. Mirrors event_ticket_info_and_attractions_steps.py's own
    `_seed_existing_event` (seed directly, exercise the REAL pipeline only
    for whatever the scenario is actually testing — here, the merge/expiry
    behaviour a LATER post or read triggers). `status="accepted"` by
    default (a dish already past any unrelated review-queue reason) so the
    expiry scenarios isolate staleness as the ONLY thing under test — see
    "Never queue a dish because it expired", which would be a meaningless
    proof against a dish that was already queued for an unrelated reason."""
    from app.services.event_reconciliation import new_event_id

    last_seen = datetime.now(timezone.utc) - timedelta(days=days_ago)
    event_id = new_event_id()
    fields = dict(
        event_id=event_id, venue_id=context.ee_venue_id, source_kind="venue_post",
        source_handle=context.ee_handle, source_shortcode=shortcode,
        source_event_key=f"{shortcode}_key", status=status, title=_DISH_TITLE,
        post_type="menu", is_recurring=True, recurrence_text="de segunda a sexta",
        raw_extraction={"kind": "menu", "time_known": False},
        first_seen_at=last_seen, last_seen_at=last_seen,
    )
    fields.update(overrides)
    context.ee_dao.insert_event(fields)
    return event_id


def _read_dish(context) -> dict:
    """The admin-API read every "presented as current"/"still readable"
    Then step performs itself — several scenarios have no separate "When
    the menu is read" step (e.g. "Bring a dish back...", which reads
    straight after "the post is extracted"), so the read cannot depend on
    an earlier When having populated `context.ml_response`."""
    _build_menu_admin_app(context)
    resp = context.ee_client.get(f"/admin/events/{context.ml_dish_event_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── §A: one dish, one row ───────────────────────────────────────────────────
@given("two posts from one venue announcing the same dish")
def step_given_two_posts_same_dish(context):
    _add_post(context, "ml_dish_1", timestamp=_TS)
    context.ee_openai.program(_menu_json(title="Especial do Dia"))
    _add_post(context, "ml_dish_2", timestamp=_TS)
    context.ee_openai.program(_menu_json(title="ESPECIAL DO DIA"))


@then("one menu item exists for that dish")
def step_then_one_menu_item_exists(context):
    items = _menu_items(context)
    assert len(items) == 1, items
    context.ml_dish_event_id = items[0]["event_id"]


@given("a stored menu item last seen months ago")
def step_given_stored_menu_item_last_seen_months_ago(context):
    context.ml_dish_event_id = _seed_menu_item(context, "ml_refresh_old", days_ago=90)


@given("a new post announcing the same dish")
def step_given_new_post_announcing_the_same_dish(context):
    _add_post(context, "ml_refresh_new")
    context.ee_openai.program(_menu_json())


@then("that dish records having been seen just now")
def step_then_dish_seen_just_now(context):
    items = _menu_items(context)
    assert len(items) == 1, items  # refreshed IN PLACE, never duplicated
    assert items[0]["event_id"] == context.ml_dish_event_id
    last_seen = items[0]["last_seen_at"]
    assert datetime.now(timezone.utc) - last_seen < timedelta(minutes=5), last_seen


@given("two posts from one venue announcing different dishes")
def step_given_two_posts_different_dishes(context):
    _add_post(context, "ml_dish_a", timestamp=_TS)
    context.ee_openai.program(_menu_json(title="Feijoada"))
    _add_post(context, "ml_dish_b", timestamp=_TS)
    context.ee_openai.program(_menu_json(title="Moqueca"))


@then("two menu items exist")
def step_then_two_menu_items_exist(context):
    items = _menu_items(context)
    assert len(items) == 2, items


@given("two venues each announcing a dish of the same name")
def step_given_two_venues_same_dish_name(context):
    second_venue_id, second_handle = "ml_dish_second_venue", "ml_dish_second_venue_ig"
    context.ee_dao.upsert_venue(Venue(
        venue_id=second_venue_id, venue_name="Segundo Restaurante",
        venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
    ))
    context.ee_dao.set_venue_instagram(
        VenueInstagram(venue_id=second_venue_id, instagram_handle=second_handle, status="found")
    )
    context.ee_dao.upsert_venue_event_profile(
        second_venue_id, tier="evidence_confirmed", category_pass=True,
        category_reason="RESTAURANT", evidence_score=5, evidence_sample=None,
        evaluated_at=datetime.now(timezone.utc),
    )
    _add_post(context, "ml_dish_v1", timestamp=_TS)
    context.ee_openai.program(_menu_json(title="Feijoada"))

    from app.services.event_extraction_service import ArchivedPost

    second_post = ArchivedPost(
        shortcode="ml_dish_v2", permalink="https://instagram.com/p/ml_dish_v2",
        caption="Nosso prato do dia é maravilhoso", timestamp=_TS,
        flyer_photo_key="ml_dish_v2.jpg", flyer_confidence=0.9, any_photo_key="ml_dish_v2.jpg",
    )
    context.ee_post_source.posts_by_venue.setdefault(second_venue_id, []).append(second_post)
    context.ee_openai.program(_menu_json(title="Feijoada"))

    context.ml_second_venue_id = second_venue_id
    context.ml_second_handle = second_handle


@then("each venue has its own menu item")
def step_then_each_venue_has_its_own_menu_item(context):
    row1 = context.ee_dao.get_event_by_source(context.ee_handle, "ml_dish_v1")
    row2 = context.ee_dao.get_event_by_source(context.ml_second_handle, "ml_dish_v2")
    assert row1 is not None and row2 is not None, (row1, row2)
    assert row1["event_id"] != row2["event_id"]
    assert row1["venue_id"] == context.ee_venue_id
    assert row2["venue_id"] == context.ml_second_venue_id


@given("a menu item whose venue could not be resolved")
def step_given_menu_item_venue_unresolved(context):
    second_venue_id = "ml_unresolved_second_venue"
    context.ee_dao.upsert_venue(Venue(
        venue_id=second_venue_id, venue_name="Segundo Local",
        venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
    ))
    context.ee_dao.set_venue_instagram(
        VenueInstagram(venue_id=second_venue_id, instagram_handle=context.ee_handle, status="found")
    )
    _add_handle_post(context, "ml_unresolved")
    context.ee_openai.program(_menu_json(
        title="Prato da Casa", location_text="Praça de Alimentação, Shopping Recife",
    ))
    context.ee_run_config = {"eligibility": {"mode": "handles", "handles": [context.ee_handle]}}


@then("that item is not merged into any dish")
def step_then_item_not_merged_into_any_dish(context):
    row = _stored_event(context)
    assert row["post_type"] == "menu", row
    assert row["venue_id"] is None, row


# ── §C: staying current ──────────────────────────────────────────────────────
@given("a menu item seen this month")
def step_given_menu_item_seen_this_month(context):
    context.ml_dish_event_id = _seed_menu_item(context, "ml_current", days_ago=_RECENT_DAYS_AGO)


@given("a menu item unseen for longer than the expiry window")
def step_given_menu_item_unseen_beyond_expiry_window(context):
    context.ml_dish_event_id = _seed_menu_item(context, "ml_stale", days_ago=_STALE_DAYS_AGO)


@given("a new post announcing that dish")
def step_given_a_new_post_announcing_that_dish(context):
    _add_post(context, "ml_revive")
    context.ee_openai.program(_menu_json())


@when("the menu is read")
def step_when_the_menu_is_read(context):
    pass  # each Then step below performs its own read — see _read_dish


@then("that dish is presented as current")
def step_then_dish_presented_as_current(context):
    assert _read_dish(context)["is_current"] is True


@then("that dish is not presented as current")
def step_then_dish_not_presented_as_current(context):
    assert _read_dish(context)["is_current"] is False


@then("that dish and its source posts are still readable")
def step_then_dish_and_sources_still_readable(context):
    row = _read_dish(context)
    assert row["event_id"] == context.ml_dish_event_id
    assert len(row["sources"]) >= 1, row


@when("the review queue is listed")
def step_when_the_review_queue_is_listed(context):
    _build_menu_admin_app(context)
    context.ml_queue_response = context.ee_client.get("/admin/events/review")
    assert context.ml_queue_response.status_code == 200, context.ml_queue_response.text


@then("that dish is absent from the queue")
def step_then_dish_absent_from_queue(context):
    ids = {item["event_id"] for item in context.ml_queue_response.json()}
    assert context.ml_dish_event_id not in ids, ids


@given("an operator has shortened the configured expiry window")
def step_given_operator_shortened_expiry_window(context):
    context.ml_fake_redis = fakeredis.FakeRedis(decode_responses=True)
    context.ml_fake_redis.set(ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY, json.dumps(30))
    context.ml_expiry_window_days = 30


@given("a menu item unseen for longer than that window")
def step_given_menu_item_unseen_beyond_that_window(context):
    # Comfortably past the SHORTENED (30-day) window but well inside the
    # 365-day default — proves the LIVE override is honoured, not the
    # shipped default.
    days_ago = context.ml_expiry_window_days + 10
    context.ml_dish_event_id = _seed_menu_item(context, "ml_shortened_window", days_ago=days_ago)


# ── §D: events and promotions are untouched ─────────────────────────────────
@given("two posts announcing the same event on different days")
def step_given_two_posts_same_event_different_days(context):
    _add_post(context, "ml_event_day1", timestamp=datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc))
    context.ee_openai.program(_extraction_json(
        kind="event", title="Karaoke", date_text="07/08", time_text="22h",
    ))
    _add_post(context, "ml_event_day2", timestamp=datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc))
    context.ee_openai.program(_extraction_json(
        kind="event", title="Karaoke", date_text="08/08", time_text="22h",
    ))


@then("two events exist")
def step_then_two_events_exist(context):
    items = [
        e for e in context.ee_dao.list_events(venue_id=context.ee_venue_id)
        if e.get("post_type") == "event"
    ]
    assert len(items) == 2, items


@given("two posts announcing the same promotion on one day")
def step_given_two_posts_same_promotion_one_day(context):
    _add_post(context, "ml_promo_1", timestamp=_TS)
    context.ee_openai.program(_extraction_json(
        kind="promotion", title="Happy Hour em Dobro", date_text="01/07", time_text="18h",
    ))
    _add_post(context, "ml_promo_2", timestamp=_TS)
    context.ee_openai.program(_extraction_json(
        kind="promotion", title="HAPPY HOUR EM DOBRO", date_text="01/07", time_text="18h",
    ))


@then("one promotion exists")
def step_then_one_promotion_exists(context):
    items = [
        e for e in context.ee_dao.list_events(venue_id=context.ee_venue_id)
        if e.get("post_type") == "promotion"
    ]
    assert len(items) == 1, items
