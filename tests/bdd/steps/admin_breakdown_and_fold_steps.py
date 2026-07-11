"""Behave steps for tests/bdd/api/admin-breakdown-and-addvenue-fold.feature.

Covers two operator-visible bug fixes:

- GET /admin/venue-type-breakdown must resolve its DAO through the shared
  ``_get_venue_dao_from_container`` helper (the container exposes the RDS-backed
  repository as ``pipeline_repository``, not ``venue_dao``). The BDD harness
  normally wires ``container.venue_dao`` on the MagicMock, which masks the
  production ``AttributeError``; the breakdown steps below ``del`` that attribute
  so the scenario reproduces the production container shape
  (``pipeline_repository`` only).
- ``AddVenueHandler._geo_lookup`` must accent-fold names with ``_fold_text`` so an
  accented re-add short-circuits on the free local geo index instead of spending
  a paid BestTime create.
"""
from __future__ import annotations

import asyncio
import json

import parse as _parse
from behave import given, when, then, register_type  # type: ignore[import-untyped]

from app.handlers.add_venue_handler import (
    AddVenueByAddressRequest,
    GEO_LINK_UNDO_SOURCE,
)
from app.models import NewVenueResponse, Venue
from app.models.vibe_attributes import VibeAttributes


@_parse.with_pattern(r'[^"]+')
def _parse_unquoted(text):
    """A field that stops at the next double quote — keeps the single-count
    map step from greedily swallowing the two-count line's middle."""
    return text


register_type(Unquoted=_parse_unquoted)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_year_month(context) -> str:
    """The calendar month the budget service reads (matches the harness's
    year_month_provider)."""
    return getattr(context, "year_month", None) or context.fixed_year_month


def _seed_venue(context, *, venue_id, name, lat, lng, venue_type=None,
                deprecated_source=None):
    kwargs = dict(
        processed=True,
        forecast=True,
        venue_id=venue_id,
        venue_name=name,
        venue_address=f"{name} Address, Recife - PE, Brazil",
        venue_lat=lat,
        venue_lng=lng,
    )
    if venue_type is not None:
        kwargs["venue_type"] = venue_type
    if deprecated_source is not None:
        kwargs["lifecycle_status"] = "deprecated"
        kwargs["deprecated_source"] = deprecated_source
    context.venue_dao.upsert_venue(Venue(**kwargs))


def _ok_add_response(name: str, address: str, lat: float, lng: float) -> NewVenueResponse:
    return NewVenueResponse.model_validate(
        {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven_besttime_created_fold",
                "venue_name": name,
                "venue_address": address,
                "venue_lat": lat,
                "venue_lon": lng,
            },
            "analysis": [],
        }
    )


# ---------------------------------------------------------------------------
# GET /admin/venue-type-breakdown — setup
# ---------------------------------------------------------------------------


@given('the catalog contains {count:d} active venues with BestTime type "{vtype}"')
@given('the catalog contains {count:d} active venue with BestTime type "{vtype}"')
def step_seed_typed_venues(context, count, vtype):
    ids = getattr(context, "_seeded_ids_by_type", {})
    seeded = ids.setdefault(vtype, [])
    for _ in range(count):
        idx = len(seeded)
        vid = f"ven_{vtype.lower()}_{idx:03d}"
        _seed_venue(context, venue_id=vid, name=f"{vtype} Venue {idx}",
                    lat=-8.05, lng=-34.88, venue_type=vtype)
        seeded.append(vid)
    context._seeded_ids_by_type = ids


@given('the catalog contains {count:d} active venues with no BestTime type')
@given('the catalog contains {count:d} active venue with no BestTime type')
def step_seed_untyped_venues(context, count):
    existing = getattr(context, "_seeded_untyped", 0)
    for _ in range(count):
        idx = existing
        _seed_venue(context, venue_id=f"ven_untyped_{idx:03d}",
                    name=f"Untyped Venue {idx}", lat=-8.05, lng=-34.88,
                    venue_type=None)
        existing += 1
    context._seeded_untyped = existing


@given('both "{vtype}" venues have Google primary type "{gtype}"')
def step_seed_google_type(context, vtype, gtype):
    for vid in context._seeded_ids_by_type.get(vtype, []):
        context.venue_dao.set_vibe_attributes(
            VibeAttributes(venue_id=vid, google_primary_type=gtype)
        )


@given('the "{vtype}" venue has no Google vibe attributes')
def step_no_google_type(context, vtype):
    # No-op: we simply never seed vibe attributes for this venue.
    pass


@given('the application container is not initialized')
def step_container_not_initialized(context):
    from app.routers import set_admin_container

    set_admin_container(None)
    context._container_forced_none = True


# ---------------------------------------------------------------------------
# GET /admin/venue-type-breakdown — action
# ---------------------------------------------------------------------------


@when('the operator requests the admin venue-type breakdown')
def step_request_breakdown(context):
    # Reproduce the production container shape: the real Container exposes the
    # RDS-backed repository as `pipeline_repository`, never `venue_dao`. The
    # harness wires `venue_dao` too on the MagicMock, which would mask a
    # regression to a direct `_container.venue_dao` reference; drop it so the
    # breakdown must resolve `pipeline_repository` (200).
    if not getattr(context, "_container_forced_none", False):
        try:
            del context.container.venue_dao
        except AttributeError:
            pass
    context.response = context.client.get("/admin/venue-type-breakdown")


# ---------------------------------------------------------------------------
# GET /admin/venue-type-breakdown — assertions
# ---------------------------------------------------------------------------


@then('the response field "{field}" must be {expected:d}')
def step_response_field_eq(context, field, expected):
    body = context.response.json()
    assert body.get(field) == expected, (
        f"expected {field}={expected}, got {body.get(field)!r}; body={body}"
    )


@then('the "{mapname}" map must count {count:d} for "{key:Unquoted}"')
def step_map_count_single(context, mapname, count, key):
    body = context.response.json()
    m = body.get(mapname) or {}
    assert m.get(key) == count, (
        f"expected {mapname}[{key!r}]={count}, got {m.get(key)!r}; map={m}"
    )


@then('the "{mapname}" map must count {count:d} for "{key:Unquoted}" and {count2:d} for "{key2:Unquoted}"')
def step_map_count_double(context, mapname, count, key, count2, key2):
    body = context.response.json()
    m = body.get(mapname) or {}
    assert m.get(key) == count, (
        f"expected {mapname}[{key!r}]={count}, got {m.get(key)!r}; map={m}"
    )
    assert m.get(key2) == count2, (
        f"expected {mapname}[{key2!r}]={count2}, got {m.get(key2)!r}; map={m}"
    )


# ---------------------------------------------------------------------------
# POST /admin/venues/by-address — accent-folded geo short-circuit
# ---------------------------------------------------------------------------


@given('an active venue named "{name}" is cataloged in the Redis geo index near '
       'latitude {lat:f} and longitude {lng:f}')
def step_seed_active_cataloged(context, name, lat, lng):
    vid = "ven_cataloged_active"
    _seed_venue(context, venue_id=vid, name=name, lat=lat, lng=lng, venue_type="BAR")
    context.cataloged_venue_id = vid


@given('a venue named "{name}" near latitude {lat:f} and longitude {lng:f} was '
       'deprecated by a geo-link undo')
def step_seed_deprecated_venue(context, name, lat, lng):
    _seed_venue(context, venue_id="ven_cataloged_deprecated", name=name,
                lat=lat, lng=lng, venue_type="BAR",
                deprecated_source=GEO_LINK_UNDO_SOURCE)


@given('the monthly new venue counter for the current month is {count:d}')
def step_seed_month_counter(context, count):
    ym = _current_year_month(context)
    context.fake_redis.set(f"venue_add_counter_v1:{ym}", count)
    # Pin generous budget so a manual add is not blocked by quota — the point of
    # the scenario is the geo short-circuit, not the quota gate.
    context.fake_redis.set(
        "admin_config:venue_monthly_budget",
        json.dumps({"monthly_quota": 500, "manual_reserve": 10}),
    )
    context.monthly_quota = 500
    context._seeded_counter = count


@when('the operator submits an add-venue request named "{name}" at latitude '
      '{lat:f} and longitude {lng:f}')
def step_submit_named_add(context, name, lat, lng):
    address = f"{name} Address, Recife - PE, Brazil"
    context.besttime.programmed_add_venue = _ok_add_response(name, address, lat, lng)
    request = AddVenueByAddressRequest.model_validate(
        {
            "venue_name": name,
            "venue_address": address,
            "venue_lat": lat,
            "venue_lng": lng,
        }
    )
    outcome = asyncio.run(context.add_venue_handler.add(request))

    class _Resp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    context.response = _Resp(outcome.status_code, outcome.body)


@then('the response body must identify the already-cataloged venue')
def step_body_identifies_cataloged(context):
    body = context.response.json()
    assert body.get("status") == "already_exists", body
    assert body.get("venue_id") == context.cataloged_venue_id, (
        f"expected already-cataloged id {context.cataloged_venue_id!r}, "
        f"got {body.get('venue_id')!r}; body={body}"
    )


@then('no BestTime add-venue call must be made')
def step_no_besttime_add_call(context):
    add_calls = [
        c for c in context.besttime.calls
        if c.get("method") == "add_venue_to_account"
    ]
    assert not add_calls, f"unexpected BestTime add-venue calls: {add_calls}"


@then('the monthly new venue counter for the current month must remain {count:d}')
def step_counter_remains(context, count):
    ym = _current_year_month(context)
    raw = context.fake_redis.get(f"venue_add_counter_v1:{ym}")
    actual = int(raw) if raw else 0
    assert actual == count, (
        f"expected counter to remain {count}, got {actual} (key venue_add_counter_v1:{ym})"
    )


@then('the geo lookup must not short-circuit on the deprecated venue')
@then('the geo lookup must not short-circuit')
def step_geo_no_short_circuit(context):
    body = context.response.json()
    assert body.get("status") != "already_exists", (
        f"expected the geo lookup to miss, but it short-circuited: {body}"
    )


@then('the request must fall through to the BestTime create path')
def step_fell_through_to_besttime(context):
    add_calls = [
        c for c in context.besttime.calls
        if c.get("method") == "add_venue_to_account"
    ]
    assert add_calls, (
        "expected the request to fall through to the BestTime create path, "
        f"but no add_venue_to_account call was made; calls={context.besttime.calls}"
    )
