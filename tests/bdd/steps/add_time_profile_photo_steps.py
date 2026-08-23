"""Behave steps for tests/bdd/api/add-time-profile-photo.feature.

Drives the REAL AddVenueHandler (built by environment.py) plus a REAL
VenueProfilePhotoService wired with small fakes at its Apify / media-store /
image-fetcher boundaries -- the same shape
add_venue_instagram_discovery_steps.py uses to drive the Instagram cascade on
its own.

The eligibility gate (defect 3) reads `serving.eligible_venue` from RDS via
`rds_store.is_venue_servable`, and the negative-cache attempt table
(`instagram.profile_photo_attempt`) lives in RDS too -- so, unlike most
add-venue BDD features, this file's Background step points
`context.add_venue_handler.venue_dao` at the RDS-backed `context.repository`
instead of the harness's default Redis-only `context.venue_dao`. That mirrors
PRODUCTION wiring exactly: app/container.py constructs AddVenueHandler with
`venue_dao=self.pipeline_repository`, the same RDS-backed repository. Only the
BDD harness's *default* wiring (environment.py's `_build_test_app`) uses a
lighter Redis-only double, for scenarios that never touch RDS.

A venue's Instagram handle is seeded directly into RDS rather than routed
through the (separate) Instagram-discovery cascade feature: this file leaves
add-time Instagram discovery unconfigured (`instagram_cascade_service` stays
None), so `_discover_instagram_handle` reports "skipped" with `handle: None`
-- exactly the recovered/geo-linked shape `capture_for_venue`'s own handle
fallback (a single-row RDS read) exists to handle. Pre-seeding
`instagram.handle` keeps this feature's fixtures decoupled from
add_venue_instagram_discovery_steps.py's own fakes.
"""
from __future__ import annotations

import asyncio

from behave import given, when, then  # type: ignore[import-untyped]

from app.api.apify_instagram_client import ProfileFetchResult
from app.config import settings
from app.dao.venue_media_store import profile_photo_key
from app.services.venue_profile_photo_service import (
    PROFILE_PHOTO_ATTEMPT_TABLE,
    PROFILE_PHOTO_TABLE,
    VenueProfilePhotoService,
)
from tests.bdd.steps.add_venue_instagram_discovery_steps import _venue_id_for

_CDN = "https://media.apivibesensemiddleware.click"
_MEDIA_BUCKET = "vibesense-media-test"


def _pic_url(handle: str) -> str:
    return f"https://scontent.cdninstagram.com/{handle}.jpg"


def _handle_for(venue_name: str) -> str:
    # Deterministic on venue_name, like _venue_id_for -- so a Given step can
    # seed a handle before the venue's real id is known from BestTime.
    return "handle_" + _venue_id_for(venue_name)[len("ven_ig_"):]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeApifyClient:
    def __init__(self):
        self.calls: list[str] = []
        self.programmed: dict[str, object] = {}
        self.delay: float = 0.0

    async def fetch_profile(self, handle: str):
        self.calls.append(handle)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.programmed.get(handle, ProfileFetchResult(username=handle))
        if isinstance(item, Exception):
            raise item
        return item


class _FakeMediaStore:
    """Mirrors VenueMediaStore's public surface without touching S3, so a
    scenario can make the "upload" step arbitrarily slow without spinning a
    real thread -- the observable contract under test is which phase the
    add-time deadline covers, not boto3/threading cancellation semantics."""

    def __init__(self):
        self.puts: list[dict] = []
        self.upload_delay: float = 0.0

    def profile_photo_key(self, venue_id: str, content_hash: str) -> str:
        return profile_photo_key(venue_id, content_hash)

    def cdn_url(self, key: str) -> str:
        return f"{_CDN}/{key}"

    async def put_profile_photo(self, *, venue_id, content_hash, data, content_type):
        if self.upload_delay:
            await asyncio.sleep(self.upload_delay)
        key = self.profile_photo_key(venue_id, content_hash)
        self.puts.append({"venue_id": venue_id, "key": key})
        return key, self.cdn_url(key)


def _image_fetcher(payloads: dict, delay_holder: dict):
    async def _fetch(url, *, max_bytes=None):
        if delay_holder.get("seconds"):
            await asyncio.sleep(delay_holder["seconds"])
        item = payloads[url]
        if isinstance(item, Exception):
            raise item
        return item

    return _fetch


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("add-time profile-photo capture is enabled and configured")
def step_capture_enabled(context):
    _override_setting(context, "add_venue_profile_photo_enabled", True)
    _override_setting(context, "instagram_profile_photo_enabled", True)
    _override_setting(context, "media_cdn_base_url", _CDN)
    _override_setting(context, "media_bucket", _MEDIA_BUCKET)

    # See module docstring: mirrors production's RDS-backed venue_dao wiring
    # so the eligibility gate and the negative-cache attempt table (both RDS
    # reads on context.rds_store) see the venue this scenario's add() call
    # just created. Reassign context.venue_dao too (not only the handler's
    # copy) so generic reused steps like "the venue is persisted in the
    # catalog" (which read context.venue_dao directly) see the same store the
    # handler actually wrote to, instead of the harness's default, now-stale
    # Redis-only double.
    context.venue_dao = context.repository
    context.add_venue_handler.venue_dao = context.repository

    context.photo_apify = _FakeApifyClient()
    context.photo_media_store = _FakeMediaStore()
    context.photo_image_payloads: dict = {}
    context.photo_download_delay = {"seconds": 0.0}

    service = VenueProfilePhotoService(
        repo=context.repository,
        apify_client=context.photo_apify,
        media_store=context.photo_media_store,
        image_fetcher=_image_fetcher(
            context.photo_image_payloads, context.photo_download_delay
        ),
    )
    context.venue_profile_photo_service = service
    context.add_venue_handler.venue_profile_photo_service = service


# ---------------------------------------------------------------------------
# Given -- Instagram handle + picture fixtures
# ---------------------------------------------------------------------------


def _current_venue_name(context) -> str:
    name = getattr(context, "ig_current_venue_name", None)
    if name:
        return name
    raise AssertionError(
        "no venue name pinned yet -- a photo-fixture Given step ran before "
        "'BestTime creates/rejects the venue \"X\"' (which pins the name)"
    )


def _seed_handle(context, venue_name: str) -> str:
    vid = _venue_id_for(venue_name)
    handle = _handle_for(venue_name)
    context.rds_store.upsert_enrichment(
        "instagram.handle", vid, {"venue_id": vid, "instagram_handle": handle},
        history=False, promoted={"instagram_handle": handle, "source": "bdd-fixture"},
    )
    return handle


@given("the venue's Instagram profile has a picture")
def step_profile_has_picture(context):
    venue_name = _current_venue_name(context)
    handle = _seed_handle(context, venue_name)
    url = _pic_url(handle)
    context.photo_apify.programmed[handle] = ProfileFetchResult(
        username=handle, profile_pic_url=url
    )
    context.photo_image_payloads[url] = (b"fake-jpeg-bytes", "image/jpeg")


@given("the venue's Instagram profile has no picture")
def step_profile_has_no_picture(context):
    venue_name = _current_venue_name(context)
    handle = _seed_handle(context, venue_name)
    context.photo_apify.programmed[handle] = ProfileFetchResult(
        username=handle, profile_pic_url=None
    )


@given("the add-time profile-photo deadline is very short")
def step_short_deadline(context):
    _override_setting(context, "add_venue_profile_photo_deadline_seconds", 0.05)


@given("the media upload is slower than that deadline")
def step_slow_upload(context):
    context.photo_media_store.upload_delay = 0.3


@given("the Apify profile fetch is slower than that deadline")
def step_slow_apify_fetch(context):
    context.photo_apify.delay = 0.3


@given("add-time photo capture raises an error")
def step_capture_raises(context):
    async def _boom(venue_id, handle, **kwargs):
        raise RuntimeError("simulated profile-photo capture failure")

    context.venue_profile_photo_service.capture_for_venue = _boom


@given("the profile-photo capture service is not configured")
def step_service_not_configured(context):
    context.add_venue_handler.venue_profile_photo_service = None


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the venue's profile photo is persisted")
def step_photo_persisted(context):
    vid = context.response.json().get("venue_id")
    row = context.rds_store.get_enrichment(PROFILE_PHOTO_TABLE, vid)
    assert row is not None and row.get("deleted_at") is None, (
        f"no profile-photo row persisted for {vid}"
    )


@then("a profile-photo attempt is recorded for the venue")
def step_attempt_recorded(context):
    vid = context.response.json().get("venue_id")
    row = context.rds_store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, vid)
    assert row is not None, f"no profile-photo attempt recorded for {vid}"


@then("no Apify profile fetch is attempted")
def step_no_apify_fetch(context):
    assert context.photo_apify.calls == [], context.photo_apify.calls


# ---------------------------------------------------------------------------
# Settings override bookkeeping (restored in environment.py's after_scenario)
# ---------------------------------------------------------------------------


def _override_setting(context, name: str, value) -> None:
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)
