"""Behave steps for tests/bdd/api/instagram-handle-in-venue-inventory.feature.

Drives the REAL `list_venue_inventory` handler against a REAL RedisVenueDAO
backed by fakeredis (via `context.venue_dao`, built once per scenario by
tests/bdd/environment.py). Venues and their Instagram records are seeded
through the DAO's own real setters (`upsert_venue` / `set_venue_instagram`),
matching production shapes rather than hand-built response dicts.

Two proxy DAOs stand in for `context.venue_dao` when a scenario needs to
observe or corrupt what `get_venue_instagram_bulk` returns without touching
the DAO itself:
- `_CountingIgDao` counts bulk vs per-venue Instagram reads, proving the
  "one bulk read per page, never per-venue" guarantee against real call
  counts rather than an assumption.
- `_MalformedIgDao` injects a non-`VenueInstagram` object for specific venue
  ids into the bulk-read result — a stored row so malformed it lacks the
  expected attributes entirely, independent of whatever JSON/pydantic
  tolerance `get_venue_instagram_bulk` already has at the parse layer. This
  is also the shape `tests/test_admin_venue_inventory.py`'s existing fake DAO
  already returns (a bare `object()`), so the production code must tolerate
  it regardless of this BDD feature.
"""
from __future__ import annotations

import importlib

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import Venue
from app.models.instagram import VenueInstagram

admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")


def _venue_id_for(name: str) -> str:
    return name.lower().replace(" ", "_")


def _ensure_venue(context, name: str) -> str:
    vid = _venue_id_for(name)
    seeded = getattr(context, "_ig_inv_venue_ids", set())
    if vid not in seeded:
        context.venue_dao.upsert_venue(
            Venue(
                venue_id=vid,
                venue_name=name,
                venue_address=f"{name} address",
                venue_lat=-8.05,
                venue_lng=-34.88,
            )
        )
        context._ig_inv_venue_ids = seeded | {vid}
    return vid


# ── counting / fault-injection DAO proxies ─────────────────────────────────


class _CountingIgDao:
    """Wraps the real DAO, counting Instagram bulk vs per-venue reads."""

    def __init__(self, dao):
        self._dao = dao
        self.bulk_calls = 0
        self.single_calls = 0

    def __getattr__(self, name):
        return getattr(self._dao, name)

    def get_venue_instagram_bulk(self, venue_ids):
        self.bulk_calls += 1
        return self._dao.get_venue_instagram_bulk(venue_ids)

    def get_venue_instagram(self, venue_id):
        self.single_calls += 1
        return self._dao.get_venue_instagram(venue_id)


class _MalformedIgDao:
    """Wraps the real DAO, injecting a non-VenueInstagram object for specific
    venue ids into the bulk-read result."""

    def __init__(self, dao, malformed_ids):
        self._dao = dao
        self.malformed_ids = set(malformed_ids)

    def __getattr__(self, name):
        return getattr(self._dao, name)

    def get_venue_instagram_bulk(self, venue_ids):
        result = dict(self._dao.get_venue_instagram_bulk(venue_ids))
        for vid in venue_ids:
            if vid in self.malformed_ids:
                result[vid] = object()
        return result


def _active_dao(context):
    """The DAO this scenario's listing call should be wired against — real
    by default, or a counting/fault-injection proxy set up by a Given."""
    return getattr(context, "_ig_active_dao", None) or context.venue_dao


# ── Background ──────────────────────────────────────────────────────────────


@given("the venue inventory endpoint is configured")
def step_inventory_configured(context):
    assert context.venue_dao is not None
    assert context.container is not None
    # `context.venue_dao`/fakeredis are rebuilt fresh per scenario by
    # environment.py, but plain context attributes are not — reset this
    # scenario-local "already seeded" cache so it doesn't skip re-creating a
    # venue name reused across scenarios (e.g. "Champagne Club") against the
    # new, empty fakeredis instance.
    context._ig_inv_venue_ids = set()
    context._ig_active_dao = None


# ── Given: seeding venues + Instagram records ───────────────────────────────


@given(
    'the venue "{name}" has an accepted Instagram handle "{handle}" found by '
    'the "{source}" tier with confidence {confidence:g}'
)
def step_accepted_handle(context, name, handle, source, confidence):
    vid = _ensure_venue(context, name)
    context.venue_dao.set_venue_instagram(
        VenueInstagram(
            venue_id=vid,
            instagram_handle=handle,
            instagram_url=f"https://instagram.com/{handle}",
            confidence_score=confidence,
            status="found",
            source=source,
        )
    )


@given('the venue "{name}" has an Instagram record with status "{status}"')
def step_status_only_record(context, name, status):
    vid = _ensure_venue(context, name)
    context.venue_dao.set_venue_instagram(
        VenueInstagram(
            venue_id=vid,
            instagram_handle=None,
            instagram_url=None,
            confidence_score=0.0,
            status=status,
            source=None,
        )
    )


@given('the venue "{name}" has no Instagram record at all')
def step_no_record(context, name):
    _ensure_venue(context, name)


@given(
    'the venue "{name}" has a low-confidence Instagram handle "{handle}" with '
    "confidence {confidence:g}"
)
def step_low_confidence_handle(context, name, handle, confidence):
    vid = _ensure_venue(context, name)
    context.venue_dao.set_venue_instagram(
        VenueInstagram(
            venue_id=vid,
            instagram_handle=handle,
            instagram_url=f"https://instagram.com/{handle}",
            confidence_score=confidence,
            status="low_confidence",
            source="venue_website",
        )
    )


@given("the inventory contains 25 venues with Instagram handles")
def step_25_venues(context):
    for i in range(25):
        name = f"Bulk Venue {i}"
        vid = _ensure_venue(context, name)
        context.venue_dao.set_venue_instagram(
            VenueInstagram(
                venue_id=vid,
                instagram_handle=f"bulk_{i}",
                instagram_url=f"https://instagram.com/bulk_{i}",
                confidence_score=0.7,
                status="found",
                source="venue_website",
            )
        )
    context._ig_active_dao = _CountingIgDao(context.venue_dao)


@given('the venue "{name}" has a malformed Instagram record')
def step_malformed_record(context, name):
    vid = _ensure_venue(context, name)
    dao = getattr(context, "_ig_active_dao", None)
    if not isinstance(dao, _MalformedIgDao):
        dao = _MalformedIgDao(context.venue_dao, [])
        context._ig_active_dao = dao
    dao.malformed_ids.add(vid)


@given(
    'the venue "{name}" has an Instagram handle "{handle}" with no recorded source'
)
def step_legacy_no_source(context, name, handle):
    vid = _ensure_venue(context, name)
    context.venue_dao.set_venue_instagram(
        VenueInstagram(
            venue_id=vid,
            instagram_handle=handle,
            instagram_url=f"https://instagram.com/{handle}",
            confidence_score=0.6,
            status="found",
            source=None,
        )
    )


# ── When ─────────────────────────────────────────────────────────────────


@when("I list the venue inventory")
def step_list_inventory(context):
    dao = _active_dao(context)
    context.container.pipeline_repository = dao
    context.container.venue_dao = dao
    try:
        context.inventory_response = admin_trigger_router.list_venue_inventory(
            status="all", q=None, limit=250, cursor=None,
        )
        context.inventory_error = None
    except Exception as e:  # noqa: BLE001 - captured for "listing succeeds"
        context.inventory_response = None
        context.inventory_error = e


# ── Then ─────────────────────────────────────────────────────────────────


def _row_for(context, name: str) -> dict:
    vid = _venue_id_for(name)
    for item in context.inventory_response["items"]:
        if item["venue_id"] == vid:
            context._ig_last_row = item
            return item
    raise AssertionError(f"no inventory row for venue {name!r} ({vid})")


@then('the row for "{name}" reports the Instagram handle "{handle}"')
def step_row_handle(context, name, handle):
    row = _row_for(context, name)
    assert row["instagram"] is not None, row
    assert row["instagram"]["handle"] == handle, row["instagram"]


@then('the row reports the Instagram url "{url}"')
def step_row_url(context, url):
    row = context._ig_last_row
    assert row["instagram"]["url"] == url, row["instagram"]


@then('the row reports the Instagram status "{status}"')
def step_row_status_generic(context, status):
    row = context._ig_last_row
    assert row["instagram"]["status"] == status, row["instagram"]


@then("the row reports the Instagram confidence {confidence:g}")
def step_row_confidence(context, confidence):
    row = context._ig_last_row
    assert row["instagram"]["confidence"] == confidence, row["instagram"]


@then('the row reports the Instagram source "{source}"')
def step_row_source_generic(context, source):
    row = context._ig_last_row
    assert row["instagram"]["source"] == source, row["instagram"]


@then('the row for "{name}" reports the Instagram status "{status}"')
def step_row_status(context, name, status):
    row = _row_for(context, name)
    assert row["instagram"] is not None, row
    assert row["instagram"]["status"] == status, row["instagram"]


@then('the row for "{name}" reports a null Instagram handle')
def step_row_null_handle(context, name):
    row = _row_for(context, name)
    assert row["instagram"] is not None, row
    assert row["instagram"]["handle"] is None, row["instagram"]


@then('the row for "{name}" reports no Instagram object')
def step_row_no_instagram(context, name):
    row = _row_for(context, name)
    assert row["instagram"] is None, row["instagram"]


@then('the row for "{name}" reports the instagram cache flag as {value}')
def step_row_cache_flag(context, name, value):
    row = _row_for(context, name)
    expected = value.strip().lower() == "true"
    assert row["cache_flags"]["instagram"] is expected, row["cache_flags"]


@then(
    'the row for "{name}" still reports its venue id, name, address, '
    "coordinates, lifecycle status and business status unchanged"
)
def step_row_fields_unchanged(context, name):
    row = _row_for(context, name)
    vid = _venue_id_for(name)
    assert row["venue_id"] == vid
    assert row["venue_name"] == name
    assert row["venue_address"] == f"{name} address"
    assert row["venue_lat"] == -8.05
    assert row["venue_lng"] == -34.88
    assert row["lifecycle_status"] == "active"
    assert row["google_business_status"] is None


@then("the Instagram records are read once in bulk for the page")
def step_bulk_read_once(context):
    dao = _active_dao(context)
    assert isinstance(dao, _CountingIgDao), "expected a counting DAO proxy to be wired"
    assert dao.bulk_calls == 1, dao.bulk_calls


@then("no per-venue Instagram read is issued")
def step_no_per_venue_read(context):
    dao = _active_dao(context)
    assert isinstance(dao, _CountingIgDao), "expected a counting DAO proxy to be wired"
    assert dao.single_calls == 0, dao.single_calls


@then("the listing succeeds")
def step_listing_succeeds(context):
    assert context.inventory_error is None, context.inventory_error
    assert context.inventory_response is not None


@then('the row for "{name}" still reports its handle')
def step_row_still_handle(context, name):
    row = _row_for(context, name)
    assert row["instagram"] is not None and row["instagram"]["handle"], row


@then('the row for "{name}" reports no usable Instagram handle')
def step_row_no_usable_handle(context, name):
    row = _row_for(context, name)
    instagram = row["instagram"]
    assert instagram is None or instagram.get("handle") is None, instagram


@then('the row for "{name}" reports a null Instagram source')
def step_row_null_source(context, name):
    row = _row_for(context, name)
    assert row["instagram"] is not None
    assert row["instagram"]["source"] is None, row["instagram"]
