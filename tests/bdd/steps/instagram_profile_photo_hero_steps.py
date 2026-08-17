"""Behave steps for tests/bdd/enrichment/instagram-profile-photo-hero.feature.

Drives the REAL `VenueProfilePhotoService` over the REAL `VenueRepository`
(RDS system of record via `tests.rds_fake.InMemoryRdsVenueStore`, which
`environment.py` already wires as `context.repository`/`context.rds_store`),
the REAL `VenueMediaStore` (only its boto3 client is faked, so the
content-addressed key, the cache-control header and the CDN URL are all
produced by production code), and the REAL projector
(`context.redis_projection_service`) writing into fakeredis.

Fakes sit only at the true external boundaries, matching this suite's
established pattern (see deep_review_corpus_steps.py):

- Apify's profile scrape (`fetch_profile`) — a programmable queue per handle
  that also RECORDS every handle it was asked for, so "no profile scrape is
  requested" is asserted as the absence of a billed call, not as a log line.
- The Instagram CDN image download — a programmable `(bytes, content_type)`
  per URL. The byte cap and content-type allowlist are the SERVICE's policy,
  so the fake hands over whatever it is told to and the service decides.

Imports of the feature's own modules are deliberately lazy and tolerant:
during the true-RED run they do not exist yet, and the scenarios must fail on
observable state (no object stored, no RDS row, no Redis key) rather than on
an ImportError.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from behave import given, when, then  # type: ignore[import-untyped]

from app.config import settings
from app.models.instagram import VenueInstagram
from app.models.venue import Venue

_LAT, _LNG = -8.05, -34.88

_PROFILE_PHOTO_TABLE = "instagram.profile_photo"
# The negative cache: one row per venue whose last attempt produced no photo.
# A DIFFERENT table from the photo row on purpose — a failed refresh must not
# overwrite (and so un-serve) a hero the venue already has.
_PROFILE_PHOTO_ATTEMPT_TABLE = "instagram.profile_photo_attempt"
_REDIS_KEY = "venue_profile_photo_v1:{}"

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _override_setting(context, name, value):
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)


def _venue(vid: str) -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=vid,
        venue_address=f"{vid} address", venue_lat=_LAT, venue_lng=_LNG,
        venue_type="BAR",
    )


# ── boundary fakes ──────────────────────────────────────────────────────────


class _RecordingS3:
    """Stands in for boto3 at the ONE call the media store makes. Every
    put_object kwarg is kept verbatim so the scenarios can assert on the key
    and the cache-control header production code actually sent."""

    def __init__(self):
        self.puts: list[dict] = []

    def put_object(self, **kwargs):
        self.puts.append(dict(kwargs))
        return {}


class _FakeProfileApify:
    """Programmed per handle with a profile result or an exception.

    `calls` is the cost gate's evidence: a handle absent from it was never
    billed. An unprogrammed handle raises, so a scenario can never pass by
    accidentally scraping a venue it never set up.
    """

    def __init__(self):
        self.calls: list[str] = []
        self._programmed: dict[str, object] = {}

    def program(self, handle: str, result_or_exception) -> None:
        self._programmed[handle] = result_or_exception

    async def fetch_profile(self, handle: str):
        self.calls.append(handle)
        item = self._programmed.get(handle)
        if item is None:
            raise AssertionError(f"BDD: no profile programmed for @{handle}")
        if isinstance(item, Exception):
            raise item
        return item


class _FakeImageFetcher:
    """Programmed per URL with `(bytes, content_type)` or an exception."""

    def __init__(self):
        self.calls: list[str] = []
        self._programmed: dict[str, object] = {}

    def program(self, url: str, payload) -> None:
        self._programmed[url] = payload

    async def __call__(self, url: str, *, max_bytes: int | None = None):
        self.calls.append(url)
        item = self._programmed.get(url)
        if item is None:
            raise AssertionError(f"BDD: no image programmed for {url}")
        if isinstance(item, Exception):
            raise item
        return item


def _profile_result(pic_url=None, *, error_code=None, username=None):
    """A `ProfileFetchResult` when the real dataclass exists, else a duck-typed
    stand-in with the identical attributes (true-RED tolerance)."""
    try:
        from app.api.apify_instagram_client import ProfileFetchResult

        return ProfileFetchResult(
            username=username, profile_pic_url=pic_url, error_code=error_code,
        )
    except ImportError:
        return SimpleNamespace(
            username=username, profile_pic_url=pic_url, error_code=error_code,
            error_description=None,
        )


# ── harness wiring ──────────────────────────────────────────────────────────


def _media_store(context):
    if getattr(context, "media_store", None) is None:
        context.recording_s3 = _RecordingS3()
        try:
            from app.dao.venue_media_store import VenueMediaStore

            context.media_store = VenueMediaStore(
                bucket=settings.media_bucket,
                region="us-east-1",
                cdn_base_url=settings.media_cdn_base_url,
                s3_client=context.recording_s3,
            )
        except ImportError:
            context.media_store = None
    return context.media_store


def _service(context):
    """The real service, or None while it does not exist yet (true RED)."""
    if getattr(context, "profile_photo_service", None) is None:
        if getattr(context, "fake_profile_apify", None) is None:
            context.fake_profile_apify = _FakeProfileApify()
            context.fake_image_fetcher = _FakeImageFetcher()
        try:
            from app.services.venue_profile_photo_service import (
                VenueProfilePhotoService,
            )

            context.profile_photo_service = VenueProfilePhotoService(
                repo=context.repository,
                apify_client=context.fake_profile_apify,
                media_store=_media_store(context),
                image_fetcher=context.fake_image_fetcher,
            )
        except ImportError:
            context.profile_photo_service = None
    return context.profile_photo_service


def _boundaries(context):
    """The fakes, created even when the service does not exist yet, so the
    `calls` lists the assertions read are always present."""
    if getattr(context, "fake_profile_apify", None) is None:
        context.fake_profile_apify = _FakeProfileApify()
        context.fake_image_fetcher = _FakeImageFetcher()
    return context.fake_profile_apify, context.fake_image_fetcher


def _summary(context) -> dict:
    return getattr(context, "profile_photo_summary", None) or {}


def _puts(context) -> list[dict]:
    recorder = getattr(context, "recording_s3", None)
    return recorder.puts if recorder is not None else []


def _seed_venue(context, vid: str) -> None:
    context.repository.upsert_venue(_venue(vid))


def _seed_handle(context, vid: str, handle: str) -> None:
    context.repository.set_venue_instagram(
        VenueInstagram(
            venue_id=vid, instagram_handle=handle,
            instagram_url=f"https://www.instagram.com/{handle}/",
            status="found", confidence_score=0.9, source="bdd",
        )
    )


def _age_row(context, vid: str, days: float) -> None:
    """Backdate the profile-photo row's updated_at — the freshness gate's input."""
    row = context.rds_store.enrichment.get(_PROFILE_PHOTO_TABLE, {}).get(vid)
    assert row is not None, f"BDD: no profile photo row seeded for {vid}"
    row["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()


def _seed_photo_row(context, vid: str, data: bytes, days_old: float) -> str:
    """Write an existing profile-photo row the way the service would have, and
    return the CDN URL it holds."""
    content_hash = hashlib.sha256(data).hexdigest()
    key = f"venue-profile-photos/{vid}/{content_hash[:16]}.jpg"
    url = f"{settings.media_cdn_base_url.rstrip('/')}/{key}"
    context.rds_store.upsert_enrichment(
        _PROFILE_PHOTO_TABLE, vid,
        {
            "venue_id": vid,
            "instagram_handle": getattr(context, "handles", {}).get(vid),
            "photo_url": url,
            "s3_key": key,
            "content_hash": content_hash,
            "content_type": "image/jpeg",
            "byte_size": len(data),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        history=False,
    )
    _age_row(context, vid, days_old)
    return url


def _project(context):
    return context.redis_projection_service.rebuild_redis_from_rds()


def _redis_photo(context, vid: str):
    raw = context.fake_redis.get(_REDIS_KEY.format(vid))
    return json.loads(raw) if raw else None


# ── Background ──────────────────────────────────────────────────────────────


@given("the Instagram profile photo job is enabled")
def step_job_enabled(context):
    _override_setting(context, "instagram_profile_photo_enabled", True)
    _boundaries(context)
    context.handles = {}
    context.seeded_venues = []


@given("the Instagram profile photo job is disabled")
def step_job_disabled(context):
    _override_setting(context, "instagram_profile_photo_enabled", False)


@given("the media bucket and CDN base URL are configured")
def step_media_configured(context):
    _override_setting(context, "media_bucket", "vibesense-media-000000000000")
    _override_setting(
        context, "media_cdn_base_url", "https://media.apivibesensemiddleware.click"
    )
    # Generous defaults so only the scenarios that are ABOUT a limit hit one.
    _override_setting(context, "instagram_profile_photo_max_venues_per_run", 50)
    _override_setting(context, "instagram_profile_photo_max_bytes", 5_000_000)


# ── Given: venues ───────────────────────────────────────────────────────────


@given('a servable venue "{vid}" with the confirmed Instagram handle "{handle}"')
def step_venue_with_handle(context, vid, handle):
    _seed_venue(context, vid)
    _seed_handle(context, vid, handle)
    context.handles[vid] = handle
    context.seeded_venues.append(vid)
    context.last_venue_id = vid
    context.last_handle = handle


@given('a servable venue "{vid}" with no confirmed Instagram handle')
def step_venue_without_handle(context, vid):
    _seed_venue(context, vid)
    context.seeded_venues.append(vid)
    context.last_venue_id = vid
    context.last_handle = None


@given("three servable venues with confirmed Instagram handles")
def step_three_venues(context):
    for vid, handle in (("v-one", "onebar"), ("v-two", "twobar"), ("v-three", "threebar")):
        _seed_venue(context, vid)
        _seed_handle(context, vid, handle)
        context.handles[vid] = handle
        context.seeded_venues.append(vid)
    context.trio = ["v-one", "v-two", "v-three"]


@given('a servable venue "{vid}" with a projected profile photo')
def step_venue_with_projected_photo(context, vid):
    _seed_venue(context, vid)
    context.seeded_venues.append(vid)
    context.last_venue_id = vid
    url = _seed_photo_row(context, vid, b"already-stored-bytes", days_old=1)
    context.projected_url = url
    _project(context)
    assert _redis_photo(context, vid) is not None, (
        f"precondition: {vid} should carry a projected profile photo"
    )


# ── Given: the scrape and the download ──────────────────────────────────────


def _program_pic(context, vid: str, data: bytes, content_type: str = "image/jpeg"):
    apify, fetcher = _boundaries(context)
    handle = context.handles[vid]
    pic_url = f"https://scontent.cdninstagram.com/{handle}/profile.jpg"
    apify.program(handle, _profile_result(pic_url, username=handle))
    fetcher.program(pic_url, (data, content_type))
    return pic_url


@given("the profile scrape returns a profile picture")
def step_scrape_returns_picture(context):
    vid = context.last_venue_id
    context.picture_bytes = f"profile-bytes-for-{vid}".encode()
    _program_pic(context, vid, context.picture_bytes)


@given('the profile scrape returns a profile picture for venue "{vid}"')
def step_scrape_returns_picture_for(context, vid):
    context.picture_bytes = f"profile-bytes-for-{vid}".encode()
    _program_pic(context, vid, context.picture_bytes)


@given("the profile scrape returns a profile with no picture URL")
def step_scrape_returns_no_picture(context):
    apify, _ = _boundaries(context)
    handle = context.handles[context.last_venue_id]
    apify.program(handle, _profile_result(None, username=handle))


@given('the profile scrape fails for venue "{vid}"')
def step_scrape_fails(context, vid):
    apify, _ = _boundaries(context)
    apify.program(
        context.handles[vid], _profile_result(None, error_code="http_error")
    )


@given("the profile scrape reports exhausted credit on the first venue")
def step_scrape_credit_exhausted(context):
    from app.api.apify_instagram_client import ApifyCreditExhaustedError

    apify, _ = _boundaries(context)
    first = context.trio[0]
    apify.program(
        context.handles[first], ApifyCreditExhaustedError("Apify credits exhausted (402)")
    )
    # The other two are programmed with perfectly good responses: if the run
    # kept going it would succeed, so "no scrape for the remaining venues" can
    # only pass because the run actually stopped.
    for vid in context.trio[1:]:
        _program_pic(context, vid, f"bytes-{vid}".encode())


@given('venue "{vid}" already has a profile photo stored {days:d} days ago')
def step_photo_stored_days_ago(context, vid, days):
    """The age stays a parameter even though the scheduled job no longer reads
    it: the scenarios that pass 400 days assert that age has STOPPED mattering,
    which is only meaningful if the row really is that old."""
    context.unchanged_bytes = f"stored-bytes-{vid}".encode()
    context.stored_url = _seed_photo_row(
        context, vid, context.unchanged_bytes, days_old=days
    )


@given("the retry window is {days:d} days")
def step_retry_window(context, days):
    # Deliberately tolerant of the setting not existing yet (true RED): the
    # scenario must fail on observable state — a second BILLED scrape — never
    # on an AttributeError raised inside a Given.
    if hasattr(settings, "instagram_profile_photo_retry_days"):
        _override_setting(context, "instagram_profile_photo_retry_days", days)


@given('the confirmed Instagram handle for venue "{vid}" is corrected to "{handle}"')
def step_handle_corrected(context, vid, handle):
    """Handle discovery revising itself — the case the freshness gate must not
    sleep through, because the stored photo now belongs to a different
    business."""
    _seed_handle(context, vid, handle)
    context.handles[vid] = handle
    context.last_venue_id = vid
    context.last_handle = handle


@given("the profile photo job has already run once")
def step_job_already_ran(context):
    step_run_job(context)
    context.first_run_summary = context.profile_photo_summary


@given('the last profile photo attempt for venue "{vid}" is {days:d} days old')
def step_age_attempt(context, vid, days):
    """Backdate the negative-cache row — the retry gate's input.

    Asserting the row exists is the point: without a recorded attempt there is
    nothing to suppress the next billed scrape with, and that absence is
    exactly the defect this scenario pins."""
    row = context.rds_store.enrichment.get(_PROFILE_PHOTO_ATTEMPT_TABLE, {}).get(vid)
    assert row is not None, (
        f"no profile photo attempt was recorded for {vid}; a failed venue with "
        "no recorded attempt is re-scraped (and re-billed) on every run"
    )
    row["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()


@given("the profile scrape returns a profile picture whose bytes are unchanged")
def step_scrape_returns_unchanged_bytes(context):
    _program_pic(context, context.last_venue_id, context.unchanged_bytes)


@given("the profile picture download exceeds the maximum allowed size")
def step_download_too_big(context):
    _override_setting(context, "instagram_profile_photo_max_bytes", 32)
    _program_pic(context, context.last_venue_id, b"x" * 4096)


@given("the profile picture download returns a disallowed content type")
def step_download_bad_content_type(context):
    _program_pic(
        context, context.last_venue_id, b"<html>login wall</html>", content_type="text/html"
    )


@given('venue "{vid}" has stored Google photos')
def step_venue_has_google_photos(context, vid):
    context.google_photos = [
        {"url": f"https://lh3.googleusercontent.com/{vid}=w320", "author_name": "A"}
    ]
    context.repository.set_venue_photos(vid, context.google_photos)


# ── When ────────────────────────────────────────────────────────────────────


@when("the profile photo job runs")
def step_run_job(context):
    """The SCHEDULED shape of the run: no config at all, i.e. backfill."""
    service = _service(context)
    if service is None:
        context.profile_photo_summary = {}
        return
    context.profile_photo_summary = _run(service.run())


@when('the profile photo job runs in "{mode}" mode')
def step_run_job_in_mode(context, mode):
    """The MANUAL shape: an operator-chosen mode, the only way refresh_all is
    ever reachable. Tolerant of `run` not yet accepting a mode (true RED) — the
    scenario must then fail on the observable absence of a second billed
    scrape, not on a TypeError inside a When."""
    service = _service(context)
    if service is None:
        context.profile_photo_summary = {}
        return
    context.profile_photo_summary = _run(service.run({"mode": mode}))


@when('the cost estimate is requested for "{mode}" mode')
def step_request_estimate(context, mode):
    """Price a run without buying one. Tolerant of `estimate` not existing yet
    (true RED): the scenario then fails on the estimate's venue count, which is
    the observable state under test."""
    service = _service(context)
    estimate = getattr(service, "estimate", None) if service is not None else None
    context.estimate = estimate({"mode": mode}) if estimate is not None else {}


# "When the Redis projection rebuild runs" is NOT redefined here — it already
# exists in projector_and_serving_bulk_reads_steps.py and writes to the same
# fakeredis through the same RedisProjectionService, so this feature reuses it
# rather than shadowing it (behave rejects a duplicate outright).


# ── Then ────────────────────────────────────────────────────────────────────


@then("the image bytes are stored in the media bucket under a content-addressed key")
def step_stored_content_addressed(context):
    puts = _puts(context)
    assert len(puts) == 1, f"expected exactly one upload, got {len(puts)}: {puts}"
    vid = context.last_venue_id
    digest = hashlib.sha256(context.picture_bytes).hexdigest()
    expected = f"venue-profile-photos/{vid}/{digest[:16]}.jpg"
    assert puts[0].get("Key") == expected, puts[0].get("Key")
    assert puts[0].get("Body") == context.picture_bytes, "uploaded bytes differ"


@then("the stored object carries an immutable one-year cache-control header")
def step_cache_control(context):
    puts = _puts(context)
    assert puts, "nothing was uploaded, so no cache-control header exists"
    assert puts[0].get("CacheControl") == "public, max-age=31536000, immutable", (
        puts[0].get("CacheControl")
    )


@then("the venue's profile photo row records the CDN URL")
def step_row_records_cdn_url(context):
    vid = context.last_venue_id
    row = context.rds_store.get_enrichment(_PROFILE_PHOTO_TABLE, vid)
    assert row is not None and row.get("deleted_at") is None, (
        f"no instagram.profile_photo row for {vid}"
    )
    digest = hashlib.sha256(context.picture_bytes).hexdigest()
    expected = (
        f"https://media.apivibesensemiddleware.click/"
        f"venue-profile-photos/{vid}/{digest[:16]}.jpg"
    )
    assert row["payload"].get("photo_url") == expected, row["payload"].get("photo_url")


@then('the Redis key "{key}" holds that CDN URL')
def step_redis_holds_that_url(context, key):
    vid = key.split(":", 1)[1]
    _project(context)
    doc = _redis_photo(context, vid)
    assert doc is not None, f"Redis key {key} is absent"
    digest = hashlib.sha256(context.picture_bytes).hexdigest()
    expected = (
        f"https://media.apivibesensemiddleware.click/"
        f"venue-profile-photos/{vid}/{digest[:16]}.jpg"
    )
    assert doc.get("photo_url") == expected, doc


@then('the Redis key "{key}" holds a CDN URL')
def step_redis_holds_a_url(context, key):
    vid = key.split(":", 1)[1]
    _project(context)
    doc = _redis_photo(context, vid)
    assert doc is not None, f"Redis key {key} is absent"
    url = doc.get("photo_url") or ""
    assert url.startswith("https://media.apivibesensemiddleware.click/"), url


@then('the run summary counts venue "{vid}" as "{outcome}"')
def step_summary_outcome(context, vid, outcome):
    summary = _summary(context)
    outcomes = summary.get("outcomes") or {}
    assert outcomes.get(vid) == outcome, (
        f"expected {vid} -> {outcome}, run summary said {outcomes.get(vid)!r} "
        f"(summary={summary})"
    )


@then('no profile scrape is requested for venue "{vid}"')
def step_no_scrape_for(context, vid):
    apify, _ = _boundaries(context)
    handle = context.handles.get(vid)
    assert handle not in apify.calls, (
        f"@{handle} was scraped (a billed call) for {vid}; calls={apify.calls}"
    )


@then('the number of profile scrapes for venue "{vid}" is {count:d}')
def step_scrape_count(context, vid, count):
    """Every scrape is a billed Apify unit, so this counts money, not calls."""
    apify, _ = _boundaries(context)
    handle = context.handles.get(vid)
    actual = apify.calls.count(handle)
    assert actual == count, (
        f"@{handle} was scraped {actual}x, expected {count}x; calls={apify.calls}"
    )


@then('the profile scrape for venue "{vid}" used the handle "{handle}"')
def step_scrape_used_handle(context, vid, handle):
    apify, _ = _boundaries(context)
    assert handle in apify.calls, (
        f"{vid} was never scraped with @{handle}; calls={apify.calls}"
    )


@then("no profile scrape is requested for the estimated venues")
def step_estimate_spends_nothing(context):
    """The whole point of a separate estimate endpoint: pricing a run must not
    be able to start one. Asserted as zero billed Apify calls, not as a flag."""
    apify, fetcher = _boundaries(context)
    assert apify.calls == [], f"the estimate made billed scrapes: {apify.calls}"
    assert fetcher.calls == [], f"the estimate downloaded images: {fetcher.calls}"
    assert not _puts(context), "the estimate uploaded to the media bucket"


@then("the estimate reports {count:d} venue to scrape")
def step_estimate_count(context, count):
    estimate = getattr(context, "estimate", None) or {}
    assert estimate.get("venues_to_scrape") == count, (
        f"estimate said {estimate.get('venues_to_scrape')!r} venues, "
        f"expected {count} (estimate={estimate})"
    )


@then("the estimate reports the cost of {count:d} profile scrape")
def step_estimate_cost(context, count):
    estimate = getattr(context, "estimate", None) or {}
    unit = settings.apify_instagram_profile_cost_usd
    assert estimate.get("unit_cost_usd") == unit, (
        f"unit cost {estimate.get('unit_cost_usd')!r} != {unit}"
    )
    assert abs((estimate.get("est_cost_usd") or 0) - count * unit) < 1e-9, (
        f"estimate={estimate}, expected {count} x {unit}"
    )


@then("the estimate carries a warning")
def step_estimate_has_warning(context):
    """refresh_all re-buys photos the catalog already has, so the admin UI must
    be handed something to put a warning sign next to."""
    estimate = getattr(context, "estimate", None) or {}
    assert estimate.get("warning"), f"no warning on a refresh_all estimate: {estimate}"


@then("the estimate carries no warning")
def step_estimate_has_no_warning(context):
    estimate = getattr(context, "estimate", None) or {}
    assert not estimate.get("warning"), (
        f"backfill is the free, routine mode and must not cry wolf: {estimate}"
    )


@then("the number of billed profile scrapes equals the estimated venue count")
def step_run_matches_estimate(context):
    """The invariant that keeps the estimate honest: estimate and run share ONE
    selection function, so the number the operator approved is the number of
    Apify units the run actually buys."""
    estimate = getattr(context, "estimate", None) or {}
    apify, _ = _boundaries(context)
    expected = estimate.get("venues_to_scrape")
    assert expected is not None, f"the estimate reported no count: {estimate}"
    assert len(apify.calls) == expected, (
        f"the run made {len(apify.calls)} billed scrapes ({apify.calls}) but the "
        f"estimate promised {expected}"
    )
    assert _summary(context).get("apify_calls") == expected, _summary(context)


@then("no profile scrape is requested for the remaining venues")
def step_no_scrape_for_remaining(context):
    apify, _ = _boundaries(context)
    remaining = [context.handles[v] for v in context.trio[1:]]
    billed = [h for h in remaining if h in apify.calls]
    assert not billed, f"run kept spending after credit exhaustion: {billed}"


@then("the run summary reports the run stopped for exhausted credit")
def step_summary_credit_exhausted(context):
    summary = _summary(context)
    assert summary.get("stopped_reason") == "credit_exhausted", (
        f"summary={summary}"
    )


@then("no object is uploaded to the media bucket")
def step_no_upload(context):
    puts = _puts(context)
    assert not puts, f"expected no upload, got {[p.get('Key') for p in puts]}"


@then('no Redis profile photo key exists for venue "{vid}"')
def step_no_redis_key(context, vid):
    _project(context)
    doc = _redis_photo(context, vid)
    assert doc is None, f"Redis still holds a profile photo for {vid}: {doc}"


@then('the CDN URL projected for venue "{vid}" is unchanged')
def step_projected_url_unchanged(context, vid):
    _project(context)
    doc = _redis_photo(context, vid)
    assert doc is not None, f"no projected profile photo for {vid}"
    assert doc.get("photo_url") == context.stored_url, (
        f"served URL changed: {doc.get('photo_url')} != {context.stored_url}"
    )


@then('no profile photo row is written for venue "{vid}"')
def step_no_row(context, vid):
    row = context.rds_store.get_enrichment(_PROFILE_PHOTO_TABLE, vid)
    assert row is None or row.get("deleted_at") is not None, (
        f"a partial row was persisted for {vid}: {row}"
    )


@given('the profile photo row for venue "{vid}" is soft-deleted')
def step_soft_delete_row(context, vid):
    context.rds_store.soft_delete_enrichment(
        _PROFILE_PHOTO_TABLE, vid, history=False
    )


@then("the venue's Google photo projection is unchanged")
def step_google_photos_unchanged(context):
    vid = context.last_venue_id
    projected = context.redis_only_dao.get_venue_photos(vid)
    assert projected == context.google_photos, (
        f"detail photo path changed: {projected} != {context.google_photos}"
    )
