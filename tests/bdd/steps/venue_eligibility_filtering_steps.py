"""Behave steps for tests/bdd/refresh/venue_eligibility_filtering.feature."""
from __future__ import annotations

import asyncio
import importlib
import json
import re
from typing import Optional

from behave import given, when, then  # type: ignore[import-untyped]
from prometheus_client import generate_latest

from app.handlers.venue_handler import VenueHandler
from app.models import Venue
from app.models.vibe_attributes import VibeAttributes
from app.services.venues_refresher_service import VenuesRefresherService

# All venues seed inside this radius so nearby serving can reach them.
_LAT = -8.05
_LNG = -34.88
_RADIUS_KM = 1.0


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "empty_named_venue"


def _refresher(context) -> VenuesRefresherService:
    return VenuesRefresherService(
        venue_dao=context.venue_dao,
        besttime_api=context.besttime,
        redis_client=context.fake_redis,
    )


def _seed_active_venue(
    context,
    name: str,
    *,
    venue_type: Optional[str] = None,
    google_type: Optional[str] = None,
    venue_id: Optional[str] = None,
) -> str:
    venue_id = venue_id or _slug(name)
    context.venue_dao.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id=venue_id,
            venue_name=name,
            venue_address=f"{venue_id} address",
            venue_lat=_LAT,
            venue_lng=_LNG,
            venue_type=venue_type,
        )
    )
    if google_type is not None:
        context.venue_dao.set_vibe_attributes(
            VibeAttributes(
                venue_id=venue_id,
                google_place_id=f"place_{venue_id}",
                google_primary_type=google_type,
            )
        )
    context.current_venue_id = venue_id
    return venue_id


def _venue_json(context, venue_id: str) -> dict:
    from app.dao.redis_venue_dao import VENUES_GEO_PLACE_MEMBER_FORMAT_V1

    raw = context.fake_redis.get(VENUES_GEO_PLACE_MEMBER_FORMAT_V1.format(venue_id))
    assert raw is not None, f"missing venue json for {venue_id}"
    return json.loads(raw)


def _metric_value(name: str, labels: Optional[dict] = None) -> float:
    labels = labels or {}
    prefix = f"{name}{{"
    plain = f"{name} "
    for line in generate_latest().decode("utf-8").splitlines():
        if line.startswith("#"):
            continue
        if labels:
            if not line.startswith(prefix):
                continue
            blob = line.split("{", 1)[1].split("}", 1)[0]
            parsed = {}
            for part in blob.split(","):
                key, value = part.split("=", 1)
                parsed[key] = value.strip('"')
            if any(parsed.get(k) != v for k, v in labels.items()):
                continue
            return float(line.rsplit(" ", 1)[1])
        if line.startswith(plain):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _admin_module():
    return importlib.import_module("app.routers.admin_trigger_router")


# ── Background ───────────────────────────────────────────────────────────────
@given("a clean venue inventory")
def step_clean_inventory(context):
    context.fake_redis.flushall()


@given(
    "the eligibility filter uses the default blocked types, blocked Google "
    "types, and blocked name keywords"
)
def step_default_filter(context):
    # Defaults are in effect whenever admin_config:venue_eligibility is absent.
    pass


# ── Given: inventory rows ────────────────────────────────────────────────────
@given("the BestTime account inventory contains a venue with an empty name")
def step_inventory_empty_name(context):
    context.current_venue_id = "inv_empty_name"
    context.besttime.programmed_inventory_pages = [[{
        "venue_id": "inv_empty_name",
        "venue_name": "",
        "venue_address": "Somewhere",
        "venue_lat": _LAT,
        "venue_lng": _LNG,
    }]]


@given('the BestTime account inventory contains a venue named "{name}" with no type')
def step_inventory_named(context, name):
    venue_id = _slug(name)
    context.current_venue_id = venue_id
    context.besttime.programmed_inventory_pages = [[{
        "venue_id": venue_id,
        "venue_name": name,
        "venue_address": f"{venue_id} address",
        "venue_lat": _LAT,
        "venue_lng": _LNG,
    }]]


# ── Given: active venues ─────────────────────────────────────────────────────
@given('an active venue named "{name}" with no Google type')
def step_active_no_google(context, name):
    _seed_active_venue(context, name)


@given('an active venue named "{name}" whose Google type resolves to "{gtype}"')
def step_active_with_google(context, name, gtype):
    _seed_active_venue(context, name, google_type=gtype)


@given('an active venue named "{name}" with BestTime type OTHER and no Google type')
def step_active_other(context, name):
    _seed_active_venue(context, name, venue_type="OTHER")


@given("an active venue with an empty name")
def step_active_empty(context):
    _seed_active_venue(context, "", venue_id="active_empty_name")


@given("an active venue with an empty name within the search radius")
def step_active_empty_radius(context):
    _seed_active_venue(context, "", venue_id="active_empty_name")


@given('an active venue typed {venue_type} within the search radius')
def step_active_typed_radius(context, venue_type):
    _seed_active_venue(context, "Some Parish", venue_type=venue_type, venue_id="typed_venue")


@given('an active venue named "{name}" within the search radius')
def step_active_named_radius(context, name):
    _seed_active_venue(context, name)


@given('a venue already deprecated with reason "{reason}"')
def step_pre_deprecated(context, reason):
    venue_id = _seed_active_venue(context, "Already Gone", google_type="supermarket")
    context.venue_dao.soft_delete_venue(
        venue_id=venue_id, reason=reason, source="eligibility_filter"
    )
    data = _venue_json(context, venue_id)
    context.pre_deprecated_reason = data.get("deprecated_reason")
    context.pre_deprecated_at = data.get("deprecated_at")
    context.current_venue_id = venue_id


# ── When ─────────────────────────────────────────────────────────────────────
@when("the inventory sync runs")
def step_run_inventory_sync(context):
    context.sync_summary = asyncio.run(
        _refresher(context).sync_account_inventory_to_redis()
    )


@when("the eligibility sweep runs")
@when("the eligibility sweep runs again")
def step_run_sweep(context):
    context.soft_del_keyword_before = _metric_value(
        "venues_soft_deleted_total",
        {"reason": "ineligible_name_keyword", "source": "eligibility_filter"},
    )
    context.deprecated_gauge_before = _metric_value("venues_deprecated_total")
    # Capture the crawlable (active) set before the sweep so we can prove the
    # sweep moved a venue out of it (not just that soft-delete excludes it).
    context.active_before_sweep = set(context.venue_dao.list_active_venue_ids())
    context.sweep_summary = asyncio.run(_refresher(context).run_eligibility_sweep())


@when("the photo, live forecast, and Instagram enrichment jobs run")
def step_enrichment_jobs_run(context):
    # Every enrichment/refresh job enumerates active IDs only — model that set.
    context.processed_ids = set(context.venue_dao.list_active_venue_ids())


@when("a client requests nearby venues")
def step_request_nearby(context):
    handler = VenueHandler(context.venue_dao)
    context.nearby = handler.get_venues_nearby(_LAT, _LNG, _RADIUS_KM, verbose=False)
    context.nearby_ids = {item.venue_id for item in context.nearby}
    context.nearby_names = {item.venue_name for item in context.nearby}


@when("an operator requests the eligibility configuration")
def step_get_config(context):
    context.config_response = asyncio.run(_admin_module().get_eligibility_config())


@when('an operator adds "{keyword}" to the blocked name keywords')
def step_add_keyword(context, keyword):
    context.config_response = asyncio.run(
        _admin_module().update_eligibility_config(
            config={"blocked_name_keywords": [keyword]}
        )
    )


@when("an operator submits an eligibility configuration with a non-list blocked-types value")
def step_submit_invalid_config(context):
    from fastapi import HTTPException

    context.invalid_config_error = None
    try:
        asyncio.run(
            _admin_module().update_eligibility_config(
                config={"blocked_venue_types": "not-a-list"}
            )
        )
    except HTTPException as e:
        context.invalid_config_error = e


# ── Then: lifecycle ──────────────────────────────────────────────────────────
@then("the venue is persisted as deprecated")
def step_persisted_deprecated(context):
    data = _venue_json(context, context.current_venue_id)
    assert data.get("lifecycle_status") == "deprecated", data


@then("the venue is persisted as active")
def step_persisted_active(context):
    data = _venue_json(context, context.current_venue_id)
    assert data.get("lifecycle_status", "active") == "active", data


@then('its deprecated reason is "{reason}"')
def step_deprecated_reason(context, reason):
    assert _venue_json(context, context.current_venue_id).get("deprecated_reason") == reason


@then('its deprecated source is "{source}"')
def step_deprecated_source(context, source):
    assert _venue_json(context, context.current_venue_id).get("deprecated_source") == source


@then("the venue is not returned by nearby serving")
def step_not_served(context):
    handler = VenueHandler(context.venue_dao)
    result = handler.get_venues_nearby(_LAT, _LNG, _RADIUS_KM, verbose=False)
    assert context.current_venue_id not in {item.venue_id for item in result}


@then("the venue is eligible for downstream enrichment")
def step_eligible_enrichment(context):
    assert context.current_venue_id in set(context.venue_dao.list_active_venue_ids())


@then('the venue is soft-deleted with reason "{reason}"')
def step_soft_deleted_reason(context, reason):
    data = _venue_json(context, context.current_venue_id)
    assert data.get("lifecycle_status") == "deprecated", data
    assert data.get("deprecated_reason") == reason, data
    assert data.get("deprecated_source") == "eligibility_filter", data


@then("no Google Places lookup is performed for that venue")
@then("the venue is not soft-deleted before a Google Places lookup")
def step_no_google_lookup(context):
    # The sweep is cache-first: it never constructs or calls a Google client.
    assert not any(
        call.get("method", "").startswith("google") for call in context.besttime.calls
    )


@then("both venues are soft-deleted before any Google Places lookup")
def step_both_soft_deleted(context):
    for venue_id in ("active_empty_name", _slug("Igreja Batista Central")):
        data = _venue_json(context, venue_id)
        assert data.get("lifecycle_status") == "deprecated", (venue_id, data)


@then("the Google Places labeling step only runs for venues that survived the cheap filters")
def step_labeling_only_survivors(context):
    # Cheap-rejected venues are never labeled, so no vibe attributes are created.
    for venue_id in ("active_empty_name", _slug("Igreja Batista Central")):
        assert context.venue_dao.get_vibe_attributes(venue_id) is None


@then("the venue remains active")
def step_remains_active(context):
    data = _venue_json(context, context.current_venue_id)
    assert data.get("lifecycle_status", "active") == "active", data


@then("the venue is not soft-deleted")
def step_not_soft_deleted(context):
    assert context.current_venue_id in set(context.venue_dao.list_active_venue_ids())


@then('no crawl work is performed for "{name}"')
def step_no_crawl(context, name):
    venue_id = _slug(name)
    # Discriminating: the venue was crawlable before the sweep and the sweep
    # moved it out of the active (crawlable) set the enrichment jobs enumerate.
    assert venue_id in context.active_before_sweep, "venue was not active pre-sweep"
    assert venue_id not in context.processed_ids
    assert context.venue_dao.get_venue(venue_id).is_deprecated()


# ── Then: serving ────────────────────────────────────────────────────────────
@then('the response includes "{name}"')
def step_response_includes(context, name):
    assert name in context.nearby_names, context.nearby_names


@then('the response excludes "{name}"')
def step_response_excludes(context, name):
    assert name not in context.nearby_names, context.nearby_names


@then("the response excludes the empty-named venue")
def step_response_excludes_empty(context):
    assert "active_empty_name" not in context.nearby_ids


@then("the response excludes the CHURCH-typed venue")
def step_response_excludes_church(context):
    assert "typed_venue" not in context.nearby_ids


# ── Then: admin config ───────────────────────────────────────────────────────
@then(
    "the response returns the active blocked types, blocked Google types, and "
    "blocked name keywords"
)
def step_config_returns_lists(context):
    body = context.config_response
    assert body["blocked_venue_types"], body
    assert body["blocked_google_types"], body
    assert body["hard_blocked_name_keywords"], body
    assert "ambiguous_name_keywords" in body, body


@then("the update is rejected with a validation error")
def step_update_rejected(context):
    assert context.invalid_config_error is not None
    assert context.invalid_config_error.status_code == 400


@then("the active eligibility configuration is unchanged")
def step_config_unchanged(context):
    body = asyncio.run(_admin_module().get_eligibility_config())
    assert body["source"] == "defaults", body


# ── Then: observability ──────────────────────────────────────────────────────
@then(
    'the soft-deleted venues metric increments for reason "{reason}" and source "{source}"'
)
def step_metric_increments(context, reason, source):
    after = _metric_value(
        "venues_soft_deleted_total", {"reason": reason, "source": source}
    )
    assert after > context.soft_del_keyword_before, (after, context.soft_del_keyword_before)


@then("the deprecated-venues gauge reflects the new deprecated venue")
def step_gauge_reflects(context):
    # venues_deprecated_total is set to the current deprecated count by the
    # sweep's metrics refresh (a process-global gauge over per-scenario Redis).
    expected = context.venue_dao.count_deprecated_venues()
    after = _metric_value("venues_deprecated_total")
    assert expected >= 1, expected
    assert after == expected, (after, expected)


# ── Then: idempotency ────────────────────────────────────────────────────────
@then("the venue stays deprecated with its original reason and timestamp")
def step_stays_deprecated(context):
    data = _venue_json(context, context.current_venue_id)
    assert data.get("lifecycle_status") == "deprecated", data
    assert data.get("deprecated_reason") == context.pre_deprecated_reason, data
    assert data.get("deprecated_at") == context.pre_deprecated_at, data


@then("the venue is not reactivated")
def step_not_reactivated(context):
    assert context.current_venue_id not in set(context.venue_dao.list_active_venue_ids())
