"""Unit tests for the Instagram profile photo -> venue list hero feature.

Covers the lower-level edges the BDD deliberately does not restate:
`fetch_profile`'s parsing of a real Apify payload, the content-addressed key
and cache-control policy in `VenueMediaStore`, the freshness arithmetic,
run-summary bucketing across every outcome, and the projector registry entry.

See plans/260816_instagram-profile-photo-hero.md.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import fakeredis
import pytest

from app.api.apify_instagram_client import (
    ApifyCreditExhaustedError,
    ApifyInstagramClient,
    ApifyTransportFailure,
    ProfileFetchResult,
)
from app.config import settings
from app.dao.redis_venue_dao import (
    VENUE_PROFILE_PHOTO_KEY_FORMAT,
    RedisVenueDAO,
)
from app.dao.venue_media_store import (
    CONTENT_HASH_KEY_LENGTH,
    PROFILE_PHOTO_CACHE_CONTROL,
    PROFILE_PHOTO_ROOT,
    VenueMediaStore,
    profile_photo_key,
)
from app.dao.venue_repository import VenueRepository
from app.db.geo_redis_client import GeoRedisClient
from app.models.instagram import VenueInstagramProfilePhoto
from app.models.venue import Venue
from app.services.redis_projection_service import _REBUILD_MODELS, RedisProjectionService
from app.services.venue_profile_photo_service import (
    ALLOWED_IMAGE_CONTENT_TYPES,
    OUTCOME_CREDIT_EXHAUSTED,
    OUTCOME_DOWNLOAD_FAILED,
    OUTCOME_FETCH_FAILED,
    OUTCOME_NO_HANDLE,
    OUTCOME_NO_PIC,
    OUTCOME_SKIPPED_FRESH,
    OUTCOME_STORED,
    OUTCOME_UNCHANGED,
    OUTCOME_UPLOAD_FAILED,
    PROFILE_PHOTO_TABLE,
    VenueProfilePhotoService,
)
from tests.rds_fake import InMemoryRdsVenueStore

_CDN = "https://media.apivibesensemiddleware.click"


# ── helpers ─────────────────────────────────────────────────────────────────


@pytest.fixture
def restore_settings():
    """Restore every setting this module pokes, so a failing test cannot leak
    a flag into the rest of the suite."""
    names = (
        "instagram_profile_photo_enabled",
        "instagram_profile_photo_refresh_days",
        "instagram_profile_photo_max_venues_per_run",
        "instagram_profile_photo_max_bytes",
        "media_bucket",
        "media_cdn_base_url",
    )
    saved = {n: getattr(settings, n) for n in names}
    settings.instagram_profile_photo_enabled = True
    settings.instagram_profile_photo_refresh_days = 30
    settings.instagram_profile_photo_max_venues_per_run = 50
    settings.instagram_profile_photo_max_bytes = 5_000_000
    settings.media_bucket = "vibesense-media-test"
    settings.media_cdn_base_url = _CDN
    yield
    for name, value in saved.items():
        setattr(settings, name, value)


class _RecordingS3:
    def __init__(self):
        self.puts: list[dict] = []
        self.fail = False

    def put_object(self, **kwargs):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.puts.append(dict(kwargs))
        return {}


class _FakeApify:
    def __init__(self):
        self.calls: list[str] = []
        self.programmed: dict[str, object] = {}

    async def fetch_profile(self, handle: str):
        self.calls.append(handle)
        item = self.programmed.get(handle, ProfileFetchResult(username=handle))
        if isinstance(item, Exception):
            raise item
        return item


def _fetcher(payloads: dict):
    async def _fetch(url, *, max_bytes=None):
        item = payloads[url]
        if isinstance(item, Exception):
            raise item
        return item

    return _fetch


def _venue(vid: str) -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=vid,
        venue_address=f"{vid} address", venue_lat=-8.05, venue_lng=-34.88,
        venue_type="BAR",
    )


def _harness():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    geo = GeoRedisClient(redis_client)
    store = InMemoryRdsVenueStore()
    repo = VenueRepository(geo, rds_store=store)
    return redis_client, geo, store, repo


def _seed(store, repo, vid, handle=None):
    repo.upsert_venue(_venue(vid))
    if handle:
        store.upsert_enrichment(
            "instagram.handle", vid, {"venue_id": vid, "instagram_handle": handle},
            history=False, promoted={"instagram_handle": handle, "source": "test"},
        )


def _service(store, repo, apify, media_store, payloads):
    return VenueProfilePhotoService(
        repo=repo,
        apify_client=apify,
        media_store=media_store,
        image_fetcher=_fetcher(payloads),
    )


def _pic(handle: str) -> str:
    return f"https://scontent.cdninstagram.com/{handle}.jpg"


# ── VenueMediaStore ─────────────────────────────────────────────────────────


def test_profile_photo_key_is_content_addressed_and_truncated():
    digest = hashlib.sha256(b"bytes").hexdigest()
    key = profile_photo_key("v-1", digest)
    assert key == f"{PROFILE_PHOTO_ROOT}/v-1/{digest[:CONTENT_HASH_KEY_LENGTH]}.jpg"
    # The truncation is idempotent: passing an already-short hash is a no-op,
    # so a caller cannot produce two different keys for the same photo.
    assert profile_photo_key("v-1", digest[:CONTENT_HASH_KEY_LENGTH]) == key


def test_identical_bytes_yield_an_identical_key_and_url():
    store = VenueMediaStore(
        bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=_RecordingS3()
    )
    first = store.profile_photo_key("v-1", hashlib.sha256(b"same").hexdigest())
    second = store.profile_photo_key("v-1", hashlib.sha256(b"same").hexdigest())
    assert first == second
    assert store.cdn_url(first) == store.cdn_url(second)
    # Different bytes must NOT collide onto the cached key.
    other = store.profile_photo_key("v-1", hashlib.sha256(b"different").hexdigest())
    assert other != first


def test_cdn_url_never_doubles_the_separator():
    store = VenueMediaStore(
        bucket="b", region="us-east-1", cdn_base_url=_CDN + "/", s3_client=_RecordingS3()
    )
    assert store.cdn_url("venue-profile-photos/v/abc.jpg") == (
        f"{_CDN}/venue-profile-photos/v/abc.jpg"
    )


def test_put_profile_photo_sets_immutable_year_long_cache_control():
    s3 = _RecordingS3()
    store = VenueMediaStore(
        bucket="vibesense-media-test", region="us-east-1", cdn_base_url=_CDN,
        s3_client=s3,
    )
    data = b"jpeg-bytes"
    digest = hashlib.sha256(data).hexdigest()
    key, url = asyncio.run(
        store.put_profile_photo(
            venue_id="v-1", content_hash=digest, data=data, content_type="image/jpeg"
        )
    )
    assert len(s3.puts) == 1
    put = s3.puts[0]
    assert put["Bucket"] == "vibesense-media-test"
    assert put["Key"] == key == f"venue-profile-photos/v-1/{digest[:16]}.jpg"
    assert put["CacheControl"] == PROFILE_PHOTO_CACHE_CONTROL
    assert put["CacheControl"] == "public, max-age=31536000, immutable"
    assert put["ContentType"] == "image/jpeg"
    assert url == f"{_CDN}/{key}"


# ── ApifyInstagramClient.fetch_profile ──────────────────────────────────────


def _client_returning(items):
    client = ApifyInstagramClient(api_token="t")
    client._run_actor_sync = AsyncMock(return_value=items)
    return client


def test_fetch_profile_prefers_the_hd_picture():
    client = _client_returning([
        {"username": "bar", "profilePicUrl": "http/low", "profilePicUrlHD": "http/hd"},
    ])
    result = asyncio.run(client.fetch_profile("bar"))
    assert result.profile_pic_url == "http/hd"
    assert result.error_code is None
    assert result.username == "bar"


def test_fetch_profile_falls_back_to_the_standard_picture():
    client = _client_returning([{"username": "bar", "profilePicUrl": "http/low"}])
    assert asyncio.run(client.fetch_profile("bar")).profile_pic_url == "http/low"


def test_fetch_profile_reports_a_profile_with_no_picture_as_an_absence():
    """No picture is NOT an error: the caller must be able to record `no_pic`
    rather than `fetch_failed`, because one is retryable and the other is not."""
    client = _client_returning([{"username": "bar"}])
    result = asyncio.run(client.fetch_profile("bar"))
    assert result.profile_pic_url is None
    assert result.error_code is None


def test_fetch_profile_requests_the_details_result_type():
    client = ApifyInstagramClient(api_token="t")
    client._run_actor_sync = AsyncMock(return_value=[{"username": "bar"}])
    asyncio.run(client.fetch_profile("bar"))
    _, run_input = client._run_actor_sync.await_args.args[:2]
    assert run_input["resultsType"] == "details"
    assert run_input["resultsLimit"] == 1
    assert run_input["directUrls"] == ["https://www.instagram.com/bar/"]


def test_fetch_profile_surfaces_an_apify_error_item():
    client = _client_returning([
        {"error": "not_found", "errorDescription": "no such profile"},
    ])
    result = asyncio.run(client.fetch_profile("ghost"))
    assert result.error_code == "not_found"
    assert result.profile_pic_url is None


def test_fetch_profile_prefers_a_real_item_over_an_error_item():
    """A dataset carrying both is a scrape that worked; discarding the profile
    because an error item shared the dataset would throw away what was paid for."""
    client = _client_returning([
        {"error": "no_items"},
        {"username": "bar", "profilePicUrlHD": "http/hd"},
    ])
    result = asyncio.run(client.fetch_profile("bar"))
    assert result.profile_pic_url == "http/hd"
    assert result.error_code is None


def test_fetch_profile_reports_an_empty_dataset_as_no_items():
    assert asyncio.run(_client_returning([]).fetch_profile("bar")).error_code == "no_items"


def test_fetch_profile_translates_a_transport_failure_into_an_error_code():
    client = ApifyInstagramClient(api_token="t")
    client._run_actor_sync = AsyncMock(side_effect=ApifyTransportFailure("timeout"))
    result = asyncio.run(client.fetch_profile("bar"))
    assert result.error_code == "timeout"
    assert result.profile_pic_url is None


def test_fetch_profile_lets_credit_exhaustion_propagate():
    """Identical to fetch_recent_posts: an exhausted balance must stop the
    caller's run, not be swallowed into a per-venue failure and re-spent."""
    client = ApifyInstagramClient(api_token="t")
    client._run_actor_sync = AsyncMock(side_effect=ApifyCreditExhaustedError("402"))
    with pytest.raises(ApifyCreditExhaustedError):
        asyncio.run(client.fetch_profile("bar"))


# ── the service: cost ordering and outcome bucketing ────────────────────────


def test_disabled_flag_makes_the_run_inert(restore_settings):
    settings.instagram_profile_photo_enabled = False
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["status"] == "disabled"
    assert apify.calls == []
    assert s3.puts == []


def test_missing_cdn_base_is_treated_as_not_configured(restore_settings):
    settings.media_cdn_base_url = ""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify = _FakeApify()
    media = VenueMediaStore(
        bucket="b", region="us-east-1", cdn_base_url="", s3_client=_RecordingS3()
    )
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["status"] == "not_configured"
    assert apify.calls == []


def test_fresh_row_is_skipped_before_any_billed_call(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, "v-1", {"content_hash": "abc"}, history=False
    )
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["outcomes"]["v-1"] == OUTCOME_SKIPPED_FRESH
    # The cost gate, asserted as the absence of a billed call.
    assert apify.calls == []
    assert summary["apify_calls"] == 0
    assert summary["estimated_cost_usd"] == 0.0


def test_row_older_than_the_window_is_refreshed(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, "v-1", {"content_hash": "old"}, history=False
    )
    store.enrichment[PROFILE_PHOTO_TABLE]["v-1"]["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=31)
    ).isoformat()
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("bar"): (b"new", "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_STORED
    assert apify.calls == ["bar"]


def test_a_soft_deleted_row_is_never_treated_as_fresh(restore_settings):
    """A soft-deleted row means the hero was withdrawn; treating it as fresh
    would freeze that venue out of the pipeline forever."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, "v-1", {"content_hash": "abc"}, history=False
    )
    store.soft_delete_enrichment(PROFILE_PHOTO_TABLE, "v-1", history=False)
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("bar"): (b"x", "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_STORED


def test_unchanged_bytes_upload_nothing_and_keep_the_url(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    data = b"identical-bytes"
    digest = hashlib.sha256(data).hexdigest()
    key = f"venue-profile-photos/v-1/{digest[:16]}.jpg"
    url = f"{_CDN}/{key}"
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, "v-1",
        {"venue_id": "v-1", "photo_url": url, "s3_key": key, "content_hash": digest,
         "content_type": "image/jpeg", "byte_size": len(data)},
        history=False,
    )
    store.enrichment[PROFILE_PHOTO_TABLE]["v-1"]["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=99)
    ).isoformat()
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("bar"): (data, "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_UNCHANGED
    assert s3.puts == []
    row = store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1")
    assert row["payload"]["photo_url"] == url
    # The row IS re-asserted, restarting the freshness clock so an unchanged
    # avatar is not re-scraped on the very next cycle.
    assert row["payload"]["content_hash"] == digest


def test_no_handle_costs_nothing_and_writes_nothing(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1")
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["outcomes"]["v-1"] == OUTCOME_NO_HANDLE
    assert apify.calls == []
    assert store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1") is None


def test_no_picture_records_no_pic_and_writes_nothing(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", profile_pic_url=None)
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["outcomes"]["v-1"] == OUTCOME_NO_PIC
    assert s3.puts == []
    assert store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1") is None


@pytest.mark.parametrize("content_type", ["text/html", "application/json", ""])
def test_disallowed_content_type_is_discarded(restore_settings, content_type):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("bar"): (b"x", content_type)}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_DOWNLOAD_FAILED
    assert s3.puts == []
    assert store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1") is None


@pytest.mark.parametrize("content_type", sorted(ALLOWED_IMAGE_CONTENT_TYPES))
def test_every_allowed_image_type_is_stored(restore_settings, content_type):
    """PNG/WebP are admitted even though the key extension is fixed at .jpg by
    the cross-repo contract: the served Content-Type is what renders."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media,
                 {_pic("bar"): (b"img", f"{content_type}; charset=binary")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_STORED
    assert s3.puts[0]["Key"].endswith(".jpg")
    assert s3.puts[0]["ContentType"] == content_type


def test_oversized_download_is_discarded_before_any_write(restore_settings):
    settings.instagram_profile_photo_max_bytes = 8
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media,
                 {_pic("bar"): (b"x" * 64, "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_DOWNLOAD_FAILED
    assert s3.puts == []
    assert store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1") is None


def test_download_transport_error_is_isolated(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media,
                 {_pic("bar"): RuntimeError("connection reset")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_DOWNLOAD_FAILED


def test_upload_failure_leaves_no_row(restore_settings):
    """Nothing partial is ever persisted: the row is written only after the
    upload returns, so a Redis key can never point at a missing object."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    s3.fail = True
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("bar"): (b"x", "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_UPLOAD_FAILED
    assert store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1") is None
    assert summary["status"] == "partial"


def test_credit_exhaustion_stops_the_run(restore_settings):
    _, _, store, repo = _harness()
    for vid, handle in (("v-1", "one"), ("v-2", "two"), ("v-3", "three")):
        _seed(store, repo, vid, handle)
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["one"] = ApifyCreditExhaustedError("402")
    for handle in ("two", "three"):
        apify.programmed[handle] = ProfileFetchResult(
            username=handle, profile_pic_url=_pic(handle)
        )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    payloads = {_pic(h): (b"x", "image/jpeg") for h in ("two", "three")}
    summary = asyncio.run(_service(store, repo, apify, media, payloads).run())
    assert apify.calls == ["one"]
    assert summary["stopped_reason"] == OUTCOME_CREDIT_EXHAUSTED
    assert summary["status"] == OUTCOME_CREDIT_EXHAUSTED
    assert summary["outcomes"]["v-1"] == OUTCOME_CREDIT_EXHAUSTED
    assert "v-2" not in summary["outcomes"]
    # A 402 never ran the actor, so it is not a billed call.
    assert summary["apify_calls"] == 0


def test_one_venue_failing_does_not_abort_the_run(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "one")
    _seed(store, repo, "v-2", "two")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["one"] = ProfileFetchResult(username="one", error_code="http_error")
    apify.programmed["two"] = ProfileFetchResult(
        username="two", profile_pic_url=_pic("two")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("two"): (b"x", "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_FETCH_FAILED
    assert summary["outcomes"]["v-2"] == OUTCOME_STORED
    assert summary["counts"] == {OUTCOME_FETCH_FAILED: 1, OUTCOME_STORED: 1}
    assert summary["status"] == "partial"


def test_the_per_run_cap_bounds_venues_that_actually_cost_money(restore_settings):
    """The cap is applied to the survivors of the freshness gate, not to the
    raw servable list — otherwise a catalog of already-fresh venues would
    consume the whole run budget and refresh nothing."""
    settings.instagram_profile_photo_max_venues_per_run = 1
    _, _, store, repo = _harness()
    _seed(store, repo, "v-fresh", "fresh")
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, "v-fresh", {"content_hash": "a"}, history=False
    )
    _seed(store, repo, "v-due", "due")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["due"] = ProfileFetchResult(
        username="due", profile_pic_url=_pic("due")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("due"): (b"x", "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-fresh"] == OUTCOME_SKIPPED_FRESH
    assert summary["outcomes"]["v-due"] == OUTCOME_STORED
    assert summary["venues_selected"] == 1


def test_estimated_cost_tracks_the_scrapes_actually_issued(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "one")
    _seed(store, repo, "v-2", "two")
    apify, s3 = _FakeApify(), _RecordingS3()
    for handle in ("one", "two"):
        apify.programmed[handle] = ProfileFetchResult(
            username=handle, profile_pic_url=_pic(handle)
        )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    payloads = {_pic(h): (b"x", "image/jpeg") for h in ("one", "two")}
    summary = asyncio.run(_service(store, repo, apify, media, payloads).run())
    assert summary["apify_calls"] == 2
    assert summary["estimated_cost_usd"] == pytest.approx(
        2 * settings.apify_instagram_profile_cost_usd
    )


def test_a_stored_row_carries_the_cdn_url_and_the_full_digest(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    data = b"real-bytes"
    asyncio.run(
        _service(store, repo, apify, media, {_pic("bar"): (data, "image/jpeg")}).run()
    )
    payload = store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1")["payload"]
    digest = hashlib.sha256(data).hexdigest()
    assert payload["content_hash"] == digest  # FULL digest, not the truncation
    assert payload["s3_key"] == f"venue-profile-photos/v-1/{digest[:16]}.jpg"
    assert payload["photo_url"] == f"{_CDN}/{payload['s3_key']}"
    assert payload["instagram_handle"] == "bar"
    assert payload["byte_size"] == len(data)


# ── projection ──────────────────────────────────────────────────────────────


def test_projector_registry_entry_is_registered():
    entry = _REBUILD_MODELS["instagram.profile_photo"]
    assert entry == (
        VenueInstagramProfilePhoto,
        "set_venue_profile_photo",
        "delete_venue_profile_photo",
    )


def _project(store, geo):
    dao = RedisVenueDAO(geo)
    return RedisProjectionService(redis_only_dao=dao, rds_store=store), dao


def test_projection_writes_the_key_without_a_ttl():
    """A TTL here would blank the hero on a random subset of cards whenever the
    projector fell behind — the exact failure the blocked Google hero plan was
    trying to avoid. The RDS row, not a clock, owns this key's lifetime."""
    redis_client, geo, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    repo.set_venue_profile_photo(
        VenueInstagramProfilePhoto(
            venue_id="v-1", instagram_handle="bar",
            photo_url=f"{_CDN}/venue-profile-photos/v-1/abc.jpg",
            s3_key="venue-profile-photos/v-1/abc.jpg", content_hash="abc",
        )
    )
    service, dao = _project(store, geo)
    service.rebuild_redis_from_rds()
    key = VENUE_PROFILE_PHOTO_KEY_FORMAT.format("v-1")
    assert redis_client.get(key) is not None
    assert redis_client.ttl(key) == -1  # -1 == present, no expiry
    assert dao.get_venue_profile_photo("v-1").photo_url.endswith("/abc.jpg")


def test_projection_deletes_the_key_when_the_row_is_soft_deleted():
    redis_client, geo, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    repo.set_venue_profile_photo(
        VenueInstagramProfilePhoto(
            venue_id="v-1", photo_url=f"{_CDN}/k.jpg", s3_key="k.jpg", content_hash="abc",
        )
    )
    service, _ = _project(store, geo)
    service.rebuild_redis_from_rds()
    assert redis_client.get(VENUE_PROFILE_PHOTO_KEY_FORMAT.format("v-1")) is not None

    repo.delete_venue_profile_photo("v-1")
    summary = service.rebuild_redis_from_rds()
    assert redis_client.get(VENUE_PROFILE_PHOTO_KEY_FORMAT.format("v-1")) is None
    assert summary["profile_photos"] == 0


def test_profile_photo_writes_stay_out_of_enrichment_history():
    """The durable artifact is the content-addressed S3 object; a history row
    could not reconstruct it, and the row is re-asserted every window by
    design, so history would grow by the whole catalog for nothing."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    repo.set_venue_profile_photo(
        VenueInstagramProfilePhoto(
            venue_id="v-1", photo_url=f"{_CDN}/k.jpg", s3_key="k.jpg", content_hash="abc",
        )
    )
    repo.delete_venue_profile_photo("v-1")
    assert store.history_count(PROFILE_PHOTO_TABLE, "v-1") == 0


def test_delete_venue_sweeps_the_profile_photo_key():
    redis_client, geo, store, repo = _harness()
    dao = RedisVenueDAO(geo)
    dao.upsert_venue(_venue("v-1"))
    dao.set_venue_profile_photo(
        VenueInstagramProfilePhoto(
            venue_id="v-1", photo_url=f"{_CDN}/k.jpg", s3_key="k.jpg", content_hash="abc",
        )
    )
    assert redis_client.get(VENUE_PROFILE_PHOTO_KEY_FORMAT.format("v-1")) is not None
    dao.delete_venue("v-1")
    assert redis_client.get(VENUE_PROFILE_PHOTO_KEY_FORMAT.format("v-1")) is None


def test_the_google_detail_photo_path_is_untouched():
    """The venue DETAIL carousel must stay byte-for-byte as it was: this
    feature adds a key family, it does not reshape one."""
    redis_client, geo, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    photos = [{"url": "https://lh3.googleusercontent.com/tok=w320", "author_name": "A"}]
    repo.set_venue_photos("v-1", photos)
    repo.set_venue_profile_photo(
        VenueInstagramProfilePhoto(
            venue_id="v-1", photo_url=f"{_CDN}/k.jpg", s3_key="k.jpg", content_hash="abc",
        )
    )
    service, dao = _project(store, geo)
    service.rebuild_redis_from_rds()
    assert dao.get_venue_photos("v-1") == photos
    # ...and the detail cache still expires, unlike the hero key.
    assert redis_client.ttl("venue_photos_v1:v-1") > 0
