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
from types import SimpleNamespace
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
    MODE_BACKFILL,
    MODE_REFRESH_ALL,
    InvalidProfilePhotoMode,
    OUTCOME_CREDIT_EXHAUSTED,
    OUTCOME_DOWNLOAD_FAILED,
    OUTCOME_FETCH_FAILED,
    OUTCOME_NO_HANDLE,
    OUTCOME_NO_PIC,
    OUTCOME_SKIPPED_HAS_PHOTO,
    OUTCOME_SKIPPED_RECENT_FAILURE,
    OUTCOME_SKIPPED_UNAVAILABLE,
    OUTCOME_STORED,
    OUTCOME_UNCHANGED,
    OUTCOME_UPLOAD_FAILED,
    PROFILE_PHOTO_ATTEMPT_TABLE,
    PROFILE_PHOTO_TABLE,
    VenueProfilePhotoService,
    parse_mode,
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
        "instagram_profile_photo_retry_days",
        "instagram_profile_photo_max_venues_per_run",
        "instagram_profile_photo_max_bytes",
        "media_bucket",
        "media_cdn_base_url",
    )
    saved = {n: getattr(settings, n) for n in names}
    settings.instagram_profile_photo_enabled = True
    settings.instagram_profile_photo_retry_days = 7
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


def _age(store, table: str, vid: str, days: float) -> None:
    """Backdate a facet row's `updated_at` — the input to both cost gates."""
    store.enrichment[table][vid]["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()


def _seed_photo_row(store, vid: str, handle: str, data: bytes, *, days_old: float) -> str:
    """An existing profile-photo row shaped the way the service writes one, so
    the freshness gate sees a real payload (handle included). Returns the CDN
    URL it holds."""
    digest = hashlib.sha256(data).hexdigest()
    key = f"venue-profile-photos/{vid}/{digest[:16]}.jpg"
    url = f"{_CDN}/{key}"
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, vid,
        {"venue_id": vid, "instagram_handle": handle, "photo_url": url,
         "s3_key": key, "content_hash": digest, "content_type": "image/jpeg",
         "byte_size": len(data)},
        history=False,
    )
    _age(store, PROFILE_PHOTO_TABLE, vid, days_old)
    return url


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


def test_a_stored_photo_is_skipped_before_any_billed_call(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, "v-1", {"content_hash": "abc"}, history=False
    )
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["outcomes"]["v-1"] == OUTCOME_SKIPPED_HAS_PHOTO
    # The cost gate, asserted as the absence of a billed call.
    assert apify.calls == []
    assert summary["apify_calls"] == 0
    assert summary["estimated_cost_usd"] == 0.0


@pytest.mark.parametrize("age_days", [31, 365, 3650])
def test_a_stored_photo_is_never_re_scraped_however_old_it_is(
    restore_settings, age_days
):
    """The operator decision this feature turns on: a captured profile photo is
    good indefinitely, so the scheduled job must never re-buy one on a clock.

    Parameterised past every window this job ever had (30 days), and past any
    window someone might reintroduce, because the whole point is that no
    threshold exists — a single 31-day case would still pass under a 90-day
    refresh window, which is exactly the regression this guards.
    """
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    _seed_photo_row(store, "v-1", "bar", b"long-ago", days_old=age_days)
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("bar"): (b"new", "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_SKIPPED_HAS_PHOTO
    assert apify.calls == [], "an aged photo was re-bought; the refresh window is back"
    assert summary["estimated_cost_usd"] == 0.0


def test_refresh_all_re_scrapes_a_venue_backfill_skips(restore_settings):
    """The manual escape hatch. Same fixture as the test above, one config key
    apart, so the pair pins that the mode — not the age — is what decides."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    old_url = _seed_photo_row(store, "v-1", "bar", b"long-ago", days_old=3)
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, {_pic("bar"): (b"new", "image/jpeg")})

    backfill = asyncio.run(service.run())
    assert backfill["outcomes"]["v-1"] == OUTCOME_SKIPPED_HAS_PHOTO
    assert apify.calls == []

    refreshed = asyncio.run(service.run({"mode": MODE_REFRESH_ALL}))
    assert refreshed["mode"] == MODE_REFRESH_ALL
    assert refreshed["outcomes"]["v-1"] == OUTCOME_STORED
    assert apify.calls == ["bar"]
    assert store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1")["payload"]["photo_url"] != (
        old_url
    )


def test_refresh_all_still_honours_the_negative_cache(restore_settings):
    """The documented decision (see the service module docstring): refresh_all
    replaces photos the catalog HAS, and a suppressed venue has none — so
    bypassing the negative cache would inflate the bill the operator is being
    asked to approve to buy a scrape the next backfill makes anyway. The
    intended lever for that is retry_days=0, asserted below."""
    settings.instagram_profile_photo_retry_days = 7
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    store.upsert_enrichment(
        PROFILE_PHOTO_ATTEMPT_TABLE, "v-1",
        {"venue_id": "v-1", "instagram_handle": "bar", "outcome": OUTCOME_NO_PIC},
        history=False,
    )
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, {})
    summary = asyncio.run(service.run({"mode": MODE_REFRESH_ALL}))
    assert summary["outcomes"]["v-1"] == OUTCOME_SKIPPED_RECENT_FAILURE
    assert apify.calls == []

    settings.instagram_profile_photo_retry_days = 0
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    service = _service(store, repo, apify, media, {_pic("bar"): (b"x", "image/jpeg")})
    assert asyncio.run(service.run())["outcomes"]["v-1"] == OUTCOME_STORED


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
    """Driven in refresh_all, the only mode that reaches a venue which already
    has a photo — and the mode where this short-circuit earns its keep, because
    most avatars in a catalog-wide re-scrape come back byte-identical and must
    cost zero S3 writes and zero cache invalidations."""
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
        _service(store, repo, apify, media, {_pic("bar"): (data, "image/jpeg")}).run(
            {"mode": MODE_REFRESH_ALL}
        )
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_UNCHANGED
    assert s3.puts == []
    row = store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1")
    assert row["payload"]["photo_url"] == url
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
    """The cap must do two separable things, and both are asserted here.

    POSITION: it is applied to the survivors of the freshness gate, not to the
    raw servable list — otherwise a catalog of already-fresh venues would
    consume the whole run budget and refresh nothing. `v-fresh` is what pins
    that: it is skipped without ever counting against the cap of 1.

    BOUND: it actually truncates. That needs TWO due venues — with only one,
    `due[:1]` equals `due` and the assertion holds whether the cap is applied
    or not, which is exactly how a deleted cap survives a green suite. With
    two, removing the cap makes this read 2, and the second billed scrape is
    the money the cap exists to not spend.
    """
    settings.instagram_profile_photo_max_venues_per_run = 1
    _, _, store, repo = _harness()
    _seed(store, repo, "v-fresh", "fresh")
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, "v-fresh", {"content_hash": "a"}, history=False
    )
    _seed(store, repo, "v-due-a", "duea")
    _seed(store, repo, "v-due-b", "dueb")
    apify, s3 = _FakeApify(), _RecordingS3()
    for handle in ("duea", "dueb"):
        apify.programmed[handle] = ProfileFetchResult(
            username=handle, profile_pic_url=_pic(handle)
        )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    payloads = {_pic(h): (b"x", "image/jpeg") for h in ("duea", "dueb")}
    summary = asyncio.run(_service(store, repo, apify, media, payloads).run())
    assert summary["outcomes"]["v-fresh"] == OUTCOME_SKIPPED_HAS_PHOTO
    assert summary["venues_selected"] == 1
    # The bound, in money: exactly one of the two due venues was billed for,
    # and the other was left for the next run entirely untouched.
    assert len(apify.calls) == 1, apify.calls
    assert summary["apify_calls"] == 1
    assert summary["counts"][OUTCOME_STORED] == 1
    processed = [v for v in ("v-due-a", "v-due-b") if v in summary["outcomes"]]
    assert len(processed) == 1, summary["outcomes"]


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


# ── the negative cache: a failure must not be re-billed every run ───────────


def test_a_profile_with_no_picture_is_not_rescraped_within_the_retry_window(
    restore_settings,
):
    """The live-cost defect this table exists for: a venue with no photo has no
    photo row, and a venue with no row is unconditionally due — so without a
    recorded attempt it is re-scraped, and re-billed, on every single run."""
    settings.instagram_profile_photo_retry_days = 7
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", profile_pic_url=None)
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, {})

    first = asyncio.run(service.run())
    assert first["outcomes"]["v-1"] == OUTCOME_NO_PIC
    attempt = store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1")
    assert attempt is not None, "no attempt was recorded, so nothing can be skipped"
    assert attempt["payload"]["outcome"] == OUTCOME_NO_PIC
    assert attempt["payload"]["instagram_handle"] == "bar"

    second = asyncio.run(service.run())
    assert second["outcomes"]["v-1"] == OUTCOME_SKIPPED_RECENT_FAILURE
    # The cost gate, asserted as the absence of a second billed call.
    assert apify.calls == ["bar"]
    assert second["apify_calls"] == 0
    assert second["estimated_cost_usd"] == 0.0


def test_an_attempt_older_than_the_retry_window_is_scraped_again(restore_settings):
    """The suppression is a window, not a tombstone: an Instagram profile that
    gains a picture must eventually be picked up."""
    settings.instagram_profile_photo_retry_days = 7
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", profile_pic_url=None)
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, {_pic("bar"): (b"now-there-is-one", "image/jpeg")})
    asyncio.run(service.run())

    _age(store, PROFILE_PHOTO_ATTEMPT_TABLE, "v-1", 8)
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    second = asyncio.run(service.run())
    assert second["outcomes"]["v-1"] == OUTCOME_STORED
    assert apify.calls == ["bar", "bar"]


def test_a_zero_retry_window_disables_the_suppression(restore_settings):
    """The escape hatch: after fixing an infrastructure-wide failure an
    operator must be able to retry the whole catalog now, not in a week."""
    settings.instagram_profile_photo_retry_days = 0
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", profile_pic_url=None)
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, {})
    asyncio.run(service.run())
    second = asyncio.run(service.run())
    assert second["outcomes"]["v-1"] == OUTCOME_NO_PIC
    assert apify.calls == ["bar", "bar"]


def test_a_failed_scrape_is_recorded_as_an_attempt(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", error_code="http_error")
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    asyncio.run(_service(store, repo, apify, media, {}).run())
    attempt = store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1")
    assert attempt["payload"]["outcome"] == OUTCOME_FETCH_FAILED


def test_a_failed_download_is_recorded_as_an_attempt(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    asyncio.run(
        _service(store, repo, apify, media,
                 {_pic("bar"): (b"<html>login wall</html>", "text/html")}).run()
    )
    attempt = store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1")
    assert attempt["payload"]["outcome"] == OUTCOME_DOWNLOAD_FAILED


def test_a_failed_upload_is_recorded_as_an_attempt(restore_settings):
    """An IAM or bucket-policy gap fails every venue in the catalog AFTER the
    scrape is already paid for; without a recorded attempt that is the whole
    per-run budget, burned again on every tick until someone notices."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    s3.fail = True
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    asyncio.run(
        _service(store, repo, apify, media, {_pic("bar"): (b"x", "image/jpeg")}).run()
    )
    attempt = store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1")
    assert attempt["payload"]["outcome"] == OUTCOME_UPLOAD_FAILED


def test_credit_exhaustion_records_no_attempt(restore_settings):
    """A 402 never ran the actor: nothing was billed and nothing was learned
    about the venue, so suppressing its next retry would be a free coverage
    hole."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ApifyCreditExhaustedError("402")
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["outcomes"]["v-1"] == OUTCOME_CREDIT_EXHAUSTED
    assert store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1") is None


def test_a_stored_photo_clears_the_recorded_attempt(restore_settings):
    """So a venue that finally works is never shadowed by an old failure."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", profile_pic_url=None)
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, {_pic("bar"): (b"x", "image/jpeg")})
    asyncio.run(service.run())
    assert store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1") is not None

    _age(store, PROFILE_PHOTO_ATTEMPT_TABLE, "v-1", 99)
    apify.programmed["bar"] = ProfileFetchResult(
        username="bar", profile_pic_url=_pic("bar")
    )
    second = asyncio.run(service.run())
    assert second["outcomes"]["v-1"] == OUTCOME_STORED
    row = store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1")
    assert row["deleted_at"] is not None, "the attempt row outlived the success"


def test_a_failed_refresh_leaves_the_existing_hero_intact(restore_settings):
    """THE reason the attempt lives in its own table. A failed refresh must not
    overwrite the row the venue's live hero is projected from — that would take
    a working photo off the card over a transient Apify error."""
    redis_client, geo, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    url = _seed_photo_row(store, "v-1", "bar", b"old-but-good", days_old=40)
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", error_code="http_error")
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {}).run({"mode": MODE_REFRESH_ALL})
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_FETCH_FAILED

    row = store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1")
    assert row["deleted_at"] is None
    assert row["payload"]["photo_url"] == url
    service, dao = _project(store, geo)
    service.rebuild_redis_from_rds()
    assert dao.get_venue_profile_photo("v-1").photo_url == url


def test_an_attempt_row_never_becomes_a_redis_key(restore_settings):
    """The cross-repo contract: `venue_profile_photo_v1:{venue_id}` exists ONLY
    for a venue with a real stored photo, and its JSON carries the public
    CloudFront URL in `photo_url`. vibes_bot is built against exactly that, so
    an attempt row must be invisible to the projector."""
    redis_client, geo, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", profile_pic_url=None)
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    asyncio.run(_service(store, repo, apify, media, {}).run())
    assert store.get_enrichment(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1") is not None
    assert store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1") is None

    service, _ = _project(store, geo)
    summary = service.rebuild_redis_from_rds()
    assert redis_client.get(VENUE_PROFILE_PHOTO_KEY_FORMAT.format("v-1")) is None
    assert redis_client.keys("venue_profile_photo_v1:*") == []
    assert summary["profile_photos"] == 0
    # Structural, not incidental: the attempt table has no projector entry at
    # all, so no future setter change can start emitting a key from one.
    assert PROFILE_PHOTO_ATTEMPT_TABLE not in _REBUILD_MODELS


def test_the_attempt_table_is_read_before_any_billed_call(restore_settings):
    """Skip-before-spend: the negative cache is read during SELECTION, in bulk,
    so a suppressed venue cannot have moved the Apify counter."""
    settings.instagram_profile_photo_retry_days = 7
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    store.upsert_enrichment(
        PROFILE_PHOTO_ATTEMPT_TABLE, "v-1",
        {"venue_id": "v-1", "instagram_handle": "bar", "outcome": OUTCOME_NO_PIC},
        history=False,
    )
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["outcomes"]["v-1"] == OUTCOME_SKIPPED_RECENT_FAILURE
    assert apify.calls == []
    assert summary["venues_selected"] == 0


# ── the freshness gate must follow the handle, not just the clock ───────────


def test_a_changed_handle_forces_a_refetch_despite_a_fresh_row(restore_settings):
    """Handle discovery revises itself. A row scraped from the OLD handle holds
    another business's logo, and serving that on this venue's card for up to
    the whole refresh window is a wrong answer, not a stale one."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "newbar")  # the corrected handle
    old_url = _seed_photo_row(store, "v-1", "oldbar", b"the-other-business", days_old=1)
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["newbar"] = ProfileFetchResult(
        username="newbar", profile_pic_url=_pic("newbar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media,
                 {_pic("newbar"): (b"the-right-business", "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_STORED
    assert apify.calls == ["newbar"]
    payload = store.get_enrichment(PROFILE_PHOTO_TABLE, "v-1")["payload"]
    assert payload["instagram_handle"] == "newbar"
    assert payload["photo_url"] != old_url


def test_a_recased_handle_is_not_treated_as_a_change(restore_settings):
    """Handles are case-insensitive and `@`-prefixed by convention, so a purely
    cosmetic revision must not re-buy a scrape for every venue at once."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bardozeca")
    _seed_photo_row(store, "v-1", "@BarDoZeca", b"same-business", days_old=1)
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["outcomes"]["v-1"] == OUTCOME_SKIPPED_HAS_PHOTO
    assert apify.calls == []


def test_a_row_with_no_recorded_handle_stays_fresh(restore_settings):
    """Unknown is not mismatched: a row predating the handle being recorded is
    left alone rather than re-bought."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    store.upsert_enrichment(
        PROFILE_PHOTO_TABLE, "v-1", {"content_hash": "abc"}, history=False
    )
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(_service(store, repo, apify, media, {}).run())
    assert summary["outcomes"]["v-1"] == OUTCOME_SKIPPED_HAS_PHOTO
    assert apify.calls == []


def test_an_attempt_under_a_different_handle_does_not_suppress_the_retry(
    restore_settings,
):
    """Symmetric with the freshness gate: what the old handle failed to yield
    says nothing about the corrected one."""
    settings.instagram_profile_photo_retry_days = 7
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "newbar")
    store.upsert_enrichment(
        PROFILE_PHOTO_ATTEMPT_TABLE, "v-1",
        {"venue_id": "v-1", "instagram_handle": "oldbar", "outcome": OUTCOME_NO_PIC},
        history=False,
    )
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["newbar"] = ProfileFetchResult(
        username="newbar", profile_pic_url=_pic("newbar")
    )
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {_pic("newbar"): (b"x", "image/jpeg")}).run()
    )
    assert summary["outcomes"]["v-1"] == OUTCOME_STORED
    assert apify.calls == ["newbar"]


def test_profile_photo_attempt_writes_stay_out_of_enrichment_history(restore_settings):
    """The attempt row is a rate limiter, re-asserted every retry window by
    design; history would grow by the whole failing catalog and record nothing
    recoverable."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    apify.programmed["bar"] = ProfileFetchResult(username="bar", profile_pic_url=None)
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    asyncio.run(_service(store, repo, apify, media, {}).run())
    assert store.history_count(PROFILE_PHOTO_ATTEMPT_TABLE, "v-1") == 0


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


# ── modes: the scheduled job can only ever backfill ─────────────────────────


@pytest.mark.parametrize("config", [None, {}, {"mode": None}, {"mode": ""}])
def test_parse_mode_defaults_to_backfill(config):
    """The scheduler calls `run()` with no mode, so the default must be the
    free one. Any other default would turn the cron into catalog-wide spend."""
    assert parse_mode(config) == MODE_BACKFILL


@pytest.mark.parametrize("raw", ["REFRESH_ALL", " refresh_all ", "Backfill"])
def test_parse_mode_is_case_and_whitespace_tolerant(raw):
    assert parse_mode({"mode": raw}) in (MODE_BACKFILL, MODE_REFRESH_ALL)


@pytest.mark.parametrize("raw", ["refreshall", "refresh-all", "all", "yes"])
def test_an_unknown_mode_is_rejected_not_defaulted(raw):
    """Rejected rather than resolved: the two modes differ by the whole
    catalog's worth of Apify units, so a typo must not silently pick one."""
    with pytest.raises(InvalidProfilePhotoMode):
        parse_mode({"mode": raw})


def test_a_run_with_an_unknown_mode_spends_nothing(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "bar")
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    summary = asyncio.run(
        _service(store, repo, apify, media, {}).run({"mode": "refresh-everything"})
    )
    assert summary["status"] == "invalid_mode"
    assert apify.calls == []
    assert summary["apify_calls"] == 0


def test_the_scheduled_job_asks_for_backfill_explicitly():
    """The APScheduler path must be unable to reach refresh_all, and must not
    merely rely on the service's default staying cheap — that default is one
    edit away from being changed. `main.py` names the mode, and this asserts
    the name that actually arrives at the service."""
    import main as main_module

    recorded = {}

    class _Recorder:
        async def run(self, config=None):
            recorded["config"] = config
            return {"status": "success", "counts": {}}

    saved = main_module.container
    main_module.container = SimpleNamespace(venue_profile_photo_service=_Recorder())
    try:
        asyncio.run(main_module.run_instagram_profile_photo_job())
    finally:
        main_module.container = saved

    assert recorded["config"] == {"mode": MODE_BACKFILL}
    assert parse_mode(recorded["config"]) == MODE_BACKFILL


def test_the_admin_dialog_defaults_to_backfill():
    """The registry's default_config is what the admin panel pre-fills, so an
    operator who clicks Run without touching anything gets the free mode."""
    import importlib

    router_module = importlib.import_module("app.routers.admin_trigger_router")
    entry = router_module.JOB_REGISTRY["instagram_profile_photos"]
    assert entry["default_config"] == {"mode": MODE_BACKFILL}


# ── the estimate: priced from the same selection, and free ──────────────────


def _mixed_catalog(store, repo):
    """One venue in each selection state, so an estimate over it exercises
    every gate at once. Returns the ids that should actually be scraped."""
    _seed(store, repo, "v-new", "newbar")
    _seed(store, repo, "v-new-2", "newbar2")
    _seed(store, repo, "v-has-photo", "photobar")
    _seed_photo_row(store, "v-has-photo", "photobar", b"already", days_old=400)
    _seed(store, repo, "v-no-handle")
    _seed(store, repo, "v-recent-fail", "failbar")
    store.upsert_enrichment(
        PROFILE_PHOTO_ATTEMPT_TABLE, "v-recent-fail",
        {"venue_id": "v-recent-fail", "instagram_handle": "failbar",
         "outcome": OUTCOME_NO_PIC},
        history=False,
    )
    return ["v-new", "v-new-2"]


def _all_programmed(apify, payloads, handles):
    for handle in handles:
        apify.programmed[handle] = ProfileFetchResult(
            username=handle, profile_pic_url=_pic(handle)
        )
        payloads[_pic(handle)] = (f"bytes-{handle}".encode(), "image/jpeg")


def test_the_estimate_makes_no_billed_call_of_any_kind(restore_settings):
    """A separate endpoint from the trigger exists so an estimate can never
    start a run; this asserts the same thing one layer down, on the counters
    that represent money."""
    _, _, store, repo = _harness()
    _mixed_catalog(store, repo)
    apify, s3 = _FakeApify(), _RecordingS3()
    payloads: dict = {}
    _all_programmed(apify, payloads, ["newbar", "newbar2"])
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, payloads)

    for mode in (MODE_BACKFILL, MODE_REFRESH_ALL):
        estimate = service.estimate({"mode": mode})
        assert estimate["venues_to_scrape"] > 0  # it really did select something
    assert apify.calls == [], apify.calls
    assert s3.puts == []
    # And it cannot spend by accident later either: there is no await in it.
    assert not asyncio.iscoroutinefunction(service.estimate)


def test_the_estimate_count_equals_what_the_run_scrapes(restore_settings):
    """THE anti-drift assertion. An estimate an operator approves is worthless
    if the run can scrape a different set, so the promised number and the
    billed number are compared directly over one fixture."""
    _, _, store, repo = _harness()
    expected = _mixed_catalog(store, repo)
    apify, s3 = _FakeApify(), _RecordingS3()
    payloads: dict = {}
    _all_programmed(apify, payloads, ["newbar", "newbar2"])
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, payloads)

    estimate = service.estimate({"mode": MODE_BACKFILL})
    assert estimate["venues_to_scrape"] == len(expected)
    assert estimate["skipped"] == {
        OUTCOME_NO_HANDLE: 1,
        OUTCOME_SKIPPED_HAS_PHOTO: 1,
        OUTCOME_SKIPPED_RECENT_FAILURE: 1,
    }

    summary = asyncio.run(service.run())
    assert summary["apify_calls"] == estimate["venues_to_scrape"]
    assert len(apify.calls) == estimate["venues_to_scrape"]
    assert summary["estimated_cost_usd"] == pytest.approx(estimate["est_cost_usd"])
    assert sorted(v for v, o in summary["outcomes"].items() if o == OUTCOME_STORED) == (
        sorted(expected)
    )


def test_the_refresh_all_estimate_counts_what_that_run_scrapes(restore_settings):
    """Same assertion for the expensive mode, which is the one an operator is
    actually deciding about."""
    _, _, store, repo = _harness()
    _mixed_catalog(store, repo)
    apify, s3 = _FakeApify(), _RecordingS3()
    payloads: dict = {}
    _all_programmed(apify, payloads, ["newbar", "newbar2", "photobar"])
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, payloads)

    estimate = service.estimate({"mode": MODE_REFRESH_ALL})
    # The venue that already has a photo is now in scope; the negative-cached
    # one still is not (documented decision, see the service module docstring).
    assert estimate["venues_to_scrape"] == 3
    assert estimate["skipped"] == {
        OUTCOME_NO_HANDLE: 1, OUTCOME_SKIPPED_RECENT_FAILURE: 1,
    }

    summary = asyncio.run(service.run({"mode": MODE_REFRESH_ALL}))
    assert summary["apify_calls"] == estimate["venues_to_scrape"] == len(apify.calls)


def test_the_estimate_and_the_run_go_through_the_same_selection_function(
    restore_settings,
):
    """Structural, not incidental. Counting equal on one fixture could be luck;
    this pins that there is only ONE gate, so a future edit to it moves the
    estimate and the run together or not at all."""
    _, _, store, repo = _harness()
    _mixed_catalog(store, repo)
    apify, s3 = _FakeApify(), _RecordingS3()
    payloads: dict = {}
    _all_programmed(apify, payloads, ["newbar", "newbar2", "photobar"])
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, payloads)

    seen: list[str] = []
    original = service.select

    def _spy(mode=MODE_BACKFILL):
        seen.append(mode)
        return original(mode)

    service.select = _spy
    service.estimate({"mode": MODE_REFRESH_ALL})
    asyncio.run(service.run({"mode": MODE_REFRESH_ALL}))
    assert seen == [MODE_REFRESH_ALL, MODE_REFRESH_ALL]


def test_the_estimate_never_promises_more_than_the_per_run_cap_allows(
    restore_settings,
):
    """The cap truncates the run, so an estimate that reported the uncapped
    figure would overstate this run's cost and understate how many runs the
    backfill takes. Both numbers are reported, and the headline one is the
    number of scrapes this run will actually make."""
    settings.instagram_profile_photo_max_venues_per_run = 1
    _, _, store, repo = _harness()
    _mixed_catalog(store, repo)
    apify, s3 = _FakeApify(), _RecordingS3()
    payloads: dict = {}
    _all_programmed(apify, payloads, ["newbar", "newbar2"])
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, payloads)

    estimate = service.estimate({"mode": MODE_BACKFILL})
    assert estimate["venues_due"] == 2
    assert estimate["venues_to_scrape"] == 1
    assert estimate["venues_deferred"] == 1
    assert estimate["est_cost_usd_all_due"] == pytest.approx(
        2 * settings.apify_instagram_profile_cost_usd
    )

    summary = asyncio.run(service.run())
    assert len(apify.calls) == estimate["venues_to_scrape"]
    assert summary["apify_calls"] == estimate["venues_to_scrape"]


def test_the_estimate_prices_at_the_profile_unit_cost(restore_settings):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", "one")
    _seed(store, repo, "v-2", "two")
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    estimate = _service(store, repo, apify, media, {}).estimate()
    unit = settings.apify_instagram_profile_cost_usd
    assert estimate["unit_cost_usd"] == unit
    assert estimate["est_cost_usd"] == pytest.approx(2 * unit)
    assert estimate["mode"] == MODE_BACKFILL


def test_only_refresh_all_carries_a_warning(restore_settings):
    """The operator asked for a warning sign with the cost estimate. Backfill
    deliberately has none: it is the free routine mode, and a warning shown on
    every run is a warning nobody reads."""
    _, _, store, repo = _harness()
    _mixed_catalog(store, repo)
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    service = _service(store, repo, apify, media, {})

    assert not service.estimate({"mode": MODE_BACKFILL})["warning"]
    warning = service.estimate({"mode": MODE_REFRESH_ALL})["warning"]
    assert warning
    assert "refresh_all" in warning
    # The cost has to be IN the warning: a warning sign without the number is
    # not the thing that was asked for.
    assert "$" in warning


def test_an_inert_run_is_explained_rather_than_priced_at_zero(restore_settings):
    """"$0.00, 0 venues" reads identically to "nothing to do" and to "the flag
    is off"; only one of those is a problem, so the estimate says which."""
    settings.instagram_profile_photo_enabled = False
    _, _, store, repo = _harness()
    _mixed_catalog(store, repo)
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    estimate = _service(store, repo, apify, media, {}).estimate()
    assert estimate["status"] == "disabled"
    assert estimate["venues_to_scrape"] == 0
    assert estimate["est_cost_usd"] == 0
    assert "INSTAGRAM_PROFILE_PHOTO_ENABLED" in estimate["warning"]


def test_the_estimate_route_is_separate_from_the_trigger_route():
    """Mirrors POST /trigger/venue_photo_archive/estimate: pricing a run lives
    on its own path so it can never accidentally start one. Both the
    underscore form (what the admin panel builds from the job name) and the
    hyphenated alias must resolve."""
    import importlib

    router_module = importlib.import_module("app.routers.admin_trigger_router")
    paths = {r.path for r in router_module.router.routes}
    assert "/admin/trigger/instagram_profile_photos/estimate" in paths
    assert "/admin/trigger/instagram-profile-photos/estimate" in paths


def test_the_estimate_endpoint_returns_the_service_estimate():
    import importlib

    router_module = importlib.import_module("app.routers.admin_trigger_router")

    class _Service:
        def estimate(self, config=None):
            return {"mode": (config or {}).get("mode"), "venues_to_scrape": 7}

    router_module.set_container(SimpleNamespace(venue_profile_photo_service=_Service()))
    result = asyncio.new_event_loop().run_until_complete(
        router_module.estimate_instagram_profile_photos({"mode": MODE_REFRESH_ALL})
    )
    assert result == {"mode": MODE_REFRESH_ALL, "venues_to_scrape": 7}


def test_the_estimate_endpoint_rejects_an_unknown_mode():
    import importlib

    from fastapi import HTTPException

    router_module = importlib.import_module("app.routers.admin_trigger_router")

    class _Service:
        def estimate(self, config=None):
            return parse_mode(config)

    router_module.set_container(SimpleNamespace(venue_profile_photo_service=_Service()))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.new_event_loop().run_until_complete(
            router_module.estimate_instagram_profile_photos({"mode": "nope"})
        )
    assert excinfo.value.status_code == 400


# ── the edge-colour backfill mode (free: no Apify, no upload) ───────────────


def _edge_avatar(border_hex: str, size: int = 60) -> bytes:
    """A square avatar with a flat frame of `border_hex`, PNG-encoded."""
    import io as _io

    from PIL import Image

    rgb = tuple(int(border_hex[i:i + 2], 16) for i in (1, 3, 5))
    img = Image.new("RGB", (size, size), rgb)
    inset = size // 4
    img.paste(
        Image.new("RGB", (size - 2 * inset,) * 2, (255 - rgb[0], 255 - rgb[1], 255 - rgb[2])),
        (inset, inset),
    )
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _edge_harness(restore_settings, rows: dict):
    """A catalog of already-stored photo rows, with the image fetcher
    programmed against each row's own CDN URL — the way the backfill reads."""
    settings.instagram_profile_photo_enabled = True
    settings.media_bucket = "vibesense-media-000000000000"
    settings.media_cdn_base_url = _CDN
    settings.instagram_profile_photo_max_venues_per_run = 50

    _redis, _geo, store, repo = _harness()
    s3 = _RecordingS3()
    media_store = VenueMediaStore(
        bucket=settings.media_bucket, region="us-east-1",
        cdn_base_url=_CDN, s3_client=s3,
    )
    apify = _FakeApify()
    payloads = {}
    for vid, (handle, data) in rows.items():
        _seed(store, repo, vid, handle)
        url = _seed_photo_row(store, vid, handle, data, days_old=1)
        payloads[url] = (data, "image/png")
    return store, repo, apify, media_store, payloads, s3


def _payload_of(store, vid):
    return store.enrichment[PROFILE_PHOTO_TABLE][vid]["payload"]


def test_edge_color_mode_fills_rows_without_apify_or_upload(restore_settings):
    data = _edge_avatar("#004A9D")
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings, {"v-1": ("onebar", data)}
    )
    service = _service(store, repo, apify, media_store, payloads)

    summary = asyncio.run(service.run({"mode": "edge_color"}))

    assert _payload_of(store, "v-1")["edge_color"] == "#004A9D"
    # The two cost guarantees, asserted at the boundaries themselves rather
    # than inferred from a log line.
    assert apify.calls == []
    assert s3.puts == []
    assert summary["counts"]["edge_color_sampled"] == 1


def test_edge_color_mode_preserves_the_hash_and_handle(restore_settings):
    """`_has_current_photo` reads both. Rewriting either would make the next
    scheduled backfill re-buy an Apify scrape for every row this job touched."""
    data = _edge_avatar("#171717")
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings, {"v-1": ("onebar", data)}
    )
    before = dict(_payload_of(store, "v-1"))
    service = _service(store, repo, apify, media_store, payloads)

    asyncio.run(service.run({"mode": "edge_color"}))

    after = _payload_of(store, "v-1")
    for field in ("photo_url", "s3_key", "content_hash", "byte_size", "instagram_handle"):
        assert after[field] == before[field], field

    # And the proof of what that preservation buys: a following BACKFILL run
    # skips the venue instead of scraping it.
    apify.calls.clear()
    summary = asyncio.run(service.run())
    assert summary["outcomes"]["v-1"] == "skipped_has_photo"
    assert apify.calls == []


def test_edge_color_mode_skips_a_row_that_already_has_a_colour(restore_settings):
    data = _edge_avatar("#FFFFFF")
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings, {"v-1": ("onebar", data)}
    )
    _payload_of(store, "v-1")["edge_color"] = "#ABCDEF"
    service = _service(store, repo, apify, media_store, payloads)

    summary = asyncio.run(service.run({"mode": "edge_color"}))

    assert _payload_of(store, "v-1")["edge_color"] == "#ABCDEF"
    assert summary["venues_selected"] == 0


def test_edge_color_mode_leaves_a_failed_row_untouched_and_retries(restore_settings):
    """A failure must not stamp a null over the row: the next run has to pick
    it up again, and that is affordable precisely because the run is free."""
    data = _edge_avatar("#004A9D")
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings, {"v-1": ("onebar", data)}
    )
    url = _payload_of(store, "v-1")["photo_url"]
    payloads[url] = RuntimeError("CDN unreachable")
    service = _service(store, repo, apify, media_store, payloads)

    summary = asyncio.run(service.run({"mode": "edge_color"}))

    assert "edge_color" not in _payload_of(store, "v-1")
    assert summary["counts"]["edge_color_fetch_failed"] == 1
    assert service.estimate({"mode": "edge_color"})["venues_to_process"] == 1


def test_edge_color_mode_isolates_one_bad_row_from_the_rest(restore_settings):
    good = _edge_avatar("#3154A5")
    bad = _edge_avatar("#000000")
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings, {"v-good": ("goodbar", good), "v-bad": ("badbar", bad)}
    )
    payloads[_payload_of(store, "v-bad")["photo_url"]] = RuntimeError("boom")
    service = _service(store, repo, apify, media_store, payloads)

    asyncio.run(service.run({"mode": "edge_color"}))

    assert _payload_of(store, "v-good")["edge_color"] == "#3154A5"


def test_edge_color_mode_selects_a_venue_whose_handle_is_gone(restore_settings):
    """The row holds its own URL, so a venue whose handle discovery later lost
    still has a photo the app serves — and it must still get a colour."""
    data = _edge_avatar("#004A9D")
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings, {"v-1": (None, data)}
    )
    service = _service(store, repo, apify, media_store, payloads)

    asyncio.run(service.run({"mode": "edge_color"}))

    assert _payload_of(store, "v-1")["edge_color"] == "#004A9D"


def test_edge_color_estimate_is_free_and_matches_the_run(restore_settings):
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings,
        {"v-1": ("onebar", _edge_avatar("#004A9D")),
         "v-2": ("twobar", _edge_avatar("#FFFFFF"))},
    )
    service = _service(store, repo, apify, media_store, payloads)

    estimate = service.estimate({"mode": "edge_color"})

    assert estimate["venues_to_process"] == 2
    assert estimate["est_cost_usd"] == 0.0
    assert apify.calls == [] and s3.puts == []

    summary = asyncio.run(service.run({"mode": "edge_color"}))
    assert summary["venues_selected"] == estimate["venues_to_process"]


def test_edge_color_mode_honours_the_per_run_cap(restore_settings):
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings,
        {"v-1": ("onebar", _edge_avatar("#004A9D")),
         "v-2": ("twobar", _edge_avatar("#FFFFFF"))},
    )
    settings.instagram_profile_photo_max_venues_per_run = 1
    service = _service(store, repo, apify, media_store, payloads)

    summary = asyncio.run(service.run({"mode": "edge_color"}))

    coloured = sum(
        1 for vid in ("v-1", "v-2") if _payload_of(store, vid).get("edge_color")
    )
    assert coloured == 1
    assert summary["deferred"] == 1


def test_a_stored_photo_records_its_edge_colour(restore_settings):
    """The store path samples from the bytes it already holds — no extra call."""
    data = _edge_avatar("#3154A5")
    settings.instagram_profile_photo_enabled = True
    settings.media_bucket = "vibesense-media-000000000000"
    settings.media_cdn_base_url = _CDN
    settings.instagram_profile_photo_max_venues_per_run = 50

    _redis, _geo, store, repo = _harness()
    _seed(store, repo, "v-1", "onebar")
    s3 = _RecordingS3()
    media_store = VenueMediaStore(
        bucket=settings.media_bucket, region="us-east-1",
        cdn_base_url=_CDN, s3_client=s3,
    )
    apify = _FakeApify()
    apify.programmed["onebar"] = ProfileFetchResult(
        username="onebar", profile_pic_url=_pic("onebar")
    )
    service = _service(
        store, repo, apify, media_store, {_pic("onebar"): (data, "image/png")}
    )

    summary = asyncio.run(service.run())

    assert summary["outcomes"]["v-1"] == "stored"
    assert _payload_of(store, "v-1")["edge_color"] == "#3154A5"


def test_an_undecodable_image_is_still_stored_without_a_colour(restore_settings):
    """A missing colour is an ABSENCE. Refusing to store a paid-for scrape over
    an unreadable colour would be strictly worse."""
    settings.instagram_profile_photo_enabled = True
    settings.media_bucket = "vibesense-media-000000000000"
    settings.media_cdn_base_url = _CDN
    settings.instagram_profile_photo_max_venues_per_run = 50

    _redis, _geo, store, repo = _harness()
    _seed(store, repo, "v-1", "onebar")
    s3 = _RecordingS3()
    media_store = VenueMediaStore(
        bucket=settings.media_bucket, region="us-east-1",
        cdn_base_url=_CDN, s3_client=s3,
    )
    apify = _FakeApify()
    apify.programmed["onebar"] = ProfileFetchResult(
        username="onebar", profile_pic_url=_pic("onebar")
    )
    service = _service(
        store, repo, apify, media_store,
        {_pic("onebar"): (b"\xff\xd8\xff\xe0not-a-jpeg", "image/jpeg")},
    )

    summary = asyncio.run(service.run())

    assert summary["outcomes"]["v-1"] == "stored"
    assert _payload_of(store, "v-1").get("edge_color") is None


def test_an_unknown_mode_is_rejected_rather_than_defaulted(restore_settings):
    store, repo, apify, media_store, payloads, s3 = _edge_harness(
        restore_settings, {"v-1": ("onebar", _edge_avatar("#004A9D"))}
    )
    service = _service(store, repo, apify, media_store, payloads)

    summary = asyncio.run(service.run({"mode": "edge_colour"}))

    assert summary["status"] == "invalid_mode"
    assert "edge_color" not in _payload_of(store, "v-1")
    assert apify.calls == []
    with pytest.raises(InvalidProfilePhotoMode):
        service.estimate({"mode": "edge_colour"})


# ── add-time capture (capture_for_venue) ────────────────────────────────────
# The scheduled job runs every 24h behind a 200-venue cap, so a venue added
# today could show an emoji placeholder for a day. These cover the one-venue
# path that closes that window — and, more importantly, that it reuses the
# job's spend gates rather than carrying a second, drifting copy of them.


def _capture_harness(handle="bar", *, pic=True):
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1", handle)
    apify, s3 = _FakeApify(), _RecordingS3()
    if pic:
        apify.programmed[handle] = ProfileFetchResult(
            username=handle, profile_pic_url=_pic(handle)
        )
    media = VenueMediaStore(
        bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3
    )
    service = _service(store, repo, apify, media, {_pic(handle): (b"x", "image/jpeg")})
    return store, repo, apify, s3, service


def test_add_time_capture_stores_the_photo_for_a_new_venue(restore_settings):
    store, _, apify, s3, service = _capture_harness()
    summary = asyncio.run(service.capture_for_venue("v-1", "bar"))
    assert summary["outcome"] == OUTCOME_STORED
    assert apify.calls == ["bar"]
    assert len(s3.puts) == 1
    assert summary["estimated_cost_usd"] == settings.apify_instagram_profile_cost_usd


def test_add_time_capture_records_the_edge_colour_like_the_job_does(restore_settings):
    """The grey-border treatment is not a separate pass: the colour the card
    paints behind a fitted avatar is sampled from the bytes at capture time."""
    store, _, _, _, service = _capture_harness()
    asyncio.run(service.capture_for_venue("v-1", "bar"))
    row = store.enrichment[PROFILE_PHOTO_TABLE]["v-1"]
    assert "edge_color" in (row.get("payload") or {})


def test_add_time_capture_does_not_re_buy_a_photo_the_venue_already_has(restore_settings):
    """The gate that makes this safe to call on every add, including re-adds."""
    store, _, apify, s3, service = _capture_harness()
    _seed_photo_row(store, "v-1", "bar", b"x", days_old=400)
    summary = asyncio.run(service.capture_for_venue("v-1", "bar"))
    assert summary["outcome"] == OUTCOME_SKIPPED_HAS_PHOTO
    assert apify.calls == []          # nothing bought
    assert s3.puts == []
    assert summary["estimated_cost_usd"] == 0.0


def test_add_time_capture_still_re_buys_when_the_handle_changed(restore_settings):
    """Age-blind, but NOT handle-blind — a stored photo for a superseded handle
    is another business's logo, so this one must spend."""
    store, _, apify, _, service = _capture_harness()
    # Deliberately DIFFERENT bytes from what the fetcher returns: seeding the
    # same bytes makes the capture short-circuit to `unchanged` (no re-upload,
    # URL kept), which would still prove the gate opened but would hide the
    # store. This asserts the whole path.
    _seed_photo_row(store, "v-1", "old-handle", b"old-bytes", days_old=1)
    assert asyncio.run(service.capture_for_venue("v-1", "bar"))["outcome"] == (
        OUTCOME_STORED
    )
    assert apify.calls == ["bar"]


def test_add_time_capture_honours_the_negative_cache(restore_settings):
    """A handle that failed yesterday is not re-billed just because someone
    re-added the venue."""
    store, _, apify, _, service = _capture_harness()
    store.upsert_enrichment(
        PROFILE_PHOTO_ATTEMPT_TABLE, "v-1",
        {"venue_id": "v-1", "instagram_handle": "bar"}, history=False,
    )
    _age(store, PROFILE_PHOTO_ATTEMPT_TABLE, "v-1", 1)
    summary = asyncio.run(service.capture_for_venue("v-1", "bar"))
    assert summary["outcome"] == OUTCOME_SKIPPED_RECENT_FAILURE
    assert apify.calls == []


def test_add_time_capture_with_no_handle_anywhere_spends_nothing(restore_settings):
    """No handle from the caller AND none in the store — nothing to scrape."""
    _, _, store, repo = _harness()
    _seed(store, repo, "v-1")  # venue, but no instagram.handle row
    apify, s3 = _FakeApify(), _RecordingS3()
    media = VenueMediaStore(
        bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3
    )
    service = _service(store, repo, apify, media, {})
    for handle in (None, "", "   "):
        summary = asyncio.run(service.capture_for_venue("v-1", handle))
        assert summary["outcome"] == OUTCOME_NO_HANDLE
    assert apify.calls == []


def test_add_time_capture_falls_back_to_the_stored_handle(restore_settings):
    """The recovered / geo-linked add: discovery reports "skipped" because the
    venue already HAS a handle, so the caller passes None. Treating that as
    "no handle" would leave exactly those venues without a photo — the ones
    most likely to be re-added by an operator who noticed something missing."""
    _, _, apify, _, service = _capture_harness()
    summary = asyncio.run(service.capture_for_venue("v-1", None))
    assert summary["outcome"] == OUTCOME_STORED
    assert apify.calls == ["bar"]


def test_add_time_capture_is_inert_when_the_feature_is_off(restore_settings):
    """Same kill switch as the job: off means nothing is bought, not that the
    add fails."""
    _, _, apify, _, service = _capture_harness()
    settings.instagram_profile_photo_enabled = False
    summary = asyncio.run(service.capture_for_venue("v-1", "bar"))
    assert summary["outcome"] == OUTCOME_SKIPPED_UNAVAILABLE
    assert apify.calls == []
    assert summary["estimated_cost_usd"] == 0.0


def test_add_time_capture_ignores_the_per_run_cap(restore_settings):
    """The cap bounds a catalog sweep. This is one venue the operator just paid
    to add, so a cap of zero must not silently skip it — that would make the
    add-time path invisible exactly when the backfill queue is longest."""
    _, _, apify, _, service = _capture_harness()
    settings.instagram_profile_photo_max_venues_per_run = 0
    assert asyncio.run(service.capture_for_venue("v-1", "bar"))["outcome"] == (
        OUTCOME_STORED
    )
    assert apify.calls == ["bar"]
