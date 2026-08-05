"""Behave steps for tests/bdd/enrichment/venue-photo-archive.feature.

These drive the REAL VenuePhotoArchiveService with fakes at exactly three
boundaries: the Google Places client, the HTTP image downloader, and S3. The
path-resolution, skip-before-spend, failure-isolation, and accounting logic
under test is all real.

The Google fake COUNTS its calls, because the cost guarantee ("a skipped venue
costs nothing") is only meaningful if the scenario can prove Google was never
reached.
"""
from __future__ import annotations

import asyncio
import io
import json

from behave import given, then, use_step_matcher, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── fakes ─────────────────────────────────────────────────────────────────────
class _FakeS3:
    """In-memory S3 with just the surface MediaArchiveStore uses."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put_keys: set[str] = set()

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None, **kw):
        if Key in self.fail_put_keys:
            raise RuntimeError("s3 put failed")
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket=None, Key=None, **kw):
        # The JSON sidecars AND the images are read back: a re-derive hands the
        # model the stored bytes inline rather than a url it must fetch itself.
        if Key not in self.objects:
            raise KeyError(Key)
        content_type = "image/jpeg" if Key.endswith(".jpg") else "application/json"
        return {"Body": io.BytesIO(self.objects[Key]), "ContentType": content_type}

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None, **kw):
        return f"https://presigned.example/{(Params or {}).get('Key')}"

    def list_objects_v2(self, Bucket=None, Prefix="", Delimiter=None, MaxKeys=1000, **kw):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        out: dict = {}
        if Delimiter:
            prefixes = set()
            for k in keys:
                rest = k[len(Prefix):]
                if Delimiter in rest:
                    prefixes.add(Prefix + rest.split(Delimiter)[0] + Delimiter)
            out["CommonPrefixes"] = [{"Prefix": p} for p in sorted(prefixes)]
        out["Contents"] = [{"Key": k} for k in keys[:MaxKeys]]
        if not out.get("Contents"):
            out.pop("Contents", None)
        return out


class _Throttled(Exception):
    """Stands in for Google's 429. Carries `status_code` so the service's
    throttle detection sees the same shape it sees from a real HTTP error."""

    status_code = 429


class _FakeGoogle:
    """Programmable Google Places photo client that counts its calls."""

    def __init__(self) -> None:
        self.photos_by_place: dict[str, list[dict]] = {}
        self.fail_places: set[str] = set()
        self.calls: list[str] = []
        # `calls` counts venues reached; `attempts` counts underlying requests,
        # so a retry is distinguishable from a second venue.
        self.attempts = 0
        self.throttle_once: set[str] = set()
        self.throttle_always: set[str] = set()
        self.max_photos_seen: "int | None" = None

    async def get_place_photos(self, place_id, max_photos=5, max_width=800, include_ref=False):
        self.attempts += 1
        self.max_photos_seen = max_photos
        if place_id not in self.calls:
            self.calls.append(place_id)
        if place_id in self.throttle_always:
            raise _Throttled("429 Too Many Requests")
        if place_id in self.throttle_once:
            self.throttle_once.discard(place_id)
            raise _Throttled("429 Too Many Requests")
        if place_id in self.fail_places:
            raise RuntimeError("google photo fetch failed")
        return list(self.photos_by_place.get(place_id, []))[:max_photos]


class _FakeDownloader:
    """Returns bytes per URL; can be told to fail specific URLs."""

    def __init__(self) -> None:
        self.fail_urls: set[str] = set()
        self.oversized_urls: set[str] = set()
        # Every url actually downloaded, in order — the direct way to prove a
        # cap stopped downloads rather than merely trimming stored results.
        self.calls: list[str] = []

    async def download(self, url: str, *, timeout=None, max_bytes=None):
        self.calls.append(url)
        if url in self.fail_urls:
            raise RuntimeError("download failed")
        data = b"\xff\xd8\xff" + url.encode()
        if url in self.oversized_urls:
            data = b"x" * (int(max_bytes or 0) + 1)
        if max_bytes is not None and len(data) > max_bytes:
            from app.services.venue_photo_archive_service import PhotoTooLarge

            raise PhotoTooLarge(f"{len(data)} bytes exceeds {max_bytes}")
        return data, "image/jpeg"


def _service_cls():
    try:
        from app.services.venue_photo_archive_service import VenuePhotoArchiveService

        return VenuePhotoArchiveService
    except ImportError:
        return None


def _store_cls():
    try:
        from app.dao.media_archive_store import MediaArchiveStore

        return MediaArchiveStore
    except ImportError:
        return None


def _build(context):
    """Build the real service over the three fakes."""
    svc_cls, store_cls = _service_cls(), _store_cls()
    assert svc_cls is not None and store_cls is not None, (
        "VenuePhotoArchiveService / MediaArchiveStore do not exist yet — the "
        "photo archive pipeline must download venue photos into the media prefix"
    )
    context.store = store_cls(
        bucket="vibesense-datalake-test", region="us-east-1", s3_client=context.fake_s3
    )
    context.service = svc_cls(
        google_places_client=context.google,
        venue_dao=context.repository,
        media_store=context.store,
        downloader=context.downloader,
        max_photos_per_venue=10,
        today_provider=lambda: getattr(context, "today", "2026-07-26"),
    )


def _seed_venue(
    context, vid: str, *, place_id: "str | None" = "auto", lat: float = -8.05,
    lng: float = -34.88,
):
    from app.models import Venue
    from app.models.vibe_attributes import VibeAttributes

    context.repository.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id=vid,
            venue_name=f"Venue {vid}",
            venue_address=f"addr {vid}",
            venue_lat=lat,
            venue_lng=lng,
            priority=1,
        )
    )
    resolved = f"place_{vid}" if place_id == "auto" else place_id
    if resolved:
        context.repository.set_vibe_attributes(
            VibeAttributes(venue_id=vid, google_place_id=resolved)
        )
    return resolved


def _photos(n: int, *, author=None):
    return [
        {"url": f"https://lh3.googleusercontent.com/p{i}", "author_name": author,
         "photo_name": f"places/X/photos/ref{i}"}
        for i in range(n)
    ]


def _config(context, **over):
    cfg = {
        "sources": ["google_photos"],
        "venue_ids": "",
        "path_mode": "new_day",
        "path_override": "",
        "overwrite": False,
    }
    cfg.update(getattr(context, "config_over", {}))
    cfg.update(over)
    return cfg


def _run_job(context, **over):
    context.summary = _run(context.service.run(_config(context, **over)))
    return context.summary


def _image_keys(context, venue_id=None):
    keys = [k for k in context.fake_s3.objects if k.endswith(".jpg")]
    if venue_id:
        keys = [k for k in keys if f"venue_id={venue_id}/" in k]
    return keys


def _metric(name, **labels):
    v = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if v is None else float(v)


# ── Background ────────────────────────────────────────────────────────────────
@given("the media archive is enabled with a configured bucket")
def step_archive_enabled(context):
    context.fake_s3 = _FakeS3()
    context.google = _FakeGoogle()
    context.downloader = _FakeDownloader()
    context.config_over = {}
    context.today = "2026-07-26"


@given("Google Places photos are available for the catalog")
def step_photos_available(context):
    _build(context)


# ── Given ─────────────────────────────────────────────────────────────────────
@given("a venue with {n:d} available Google photos")
def step_venue_with_photos(context, n):
    context.venue_id = "ven_photos"
    pid = _seed_venue(context, context.venue_id)
    context.google.photos_by_place[pid] = _photos(n)


@given("a venue with {n:d} available Google photos carrying author attributions")
def step_venue_photos_with_authors(context, n):
    context.venue_id = "ven_attrib"
    pid = _seed_venue(context, context.venue_id)
    context.google.photos_by_place[pid] = _photos(n, author="Jane Doe")


# The bare and with-prefix forms need the REGEX matcher: with parse, `{mode}`
# greedily swallows `override" with the prefix "…` and the two registrations
# collide (AmbiguousStep). Anchoring with [^"]+ and $ keeps them distinct.
use_step_matcher("re")


@given(r'the path mode is "(?P<mode>[^"]*)"')
def step_path_mode(context, mode):
    context.config_over["path_mode"] = mode
    _seed_mode_venue(context)


@given(r'the path mode is "(?P<mode>[^"]*)" with the prefix "(?P<prefix>[^"]*)"')
def step_path_mode_prefix(context, mode, prefix):
    context.config_over["path_mode"] = mode
    context.config_over["path_override"] = prefix
    _seed_mode_venue(context)


use_step_matcher("parse")


def _seed_mode_venue(context):
    if not getattr(context, "venue_id", None):
        context.venue_id = "ven_mode"
        pid = _seed_venue(context, context.venue_id)
        context.google.photos_by_place[pid] = _photos(2)


@given("the media archive already holds partitions for {day_a} and {day_b}")
def step_existing_partitions(context, day_a, day_b):
    for day in (day_a.strip(), day_b.strip()):
        key = f"media/source=google_photos/dt={day}/venue_id=ven_old/x.jpg"
        context.fake_s3.objects[key] = b"old"


@given("the media archive holds no partitions")
def step_no_partitions(context):
    context.fake_s3.objects.clear()


@given("a venue whose images are already stored in the target partition")
def step_already_archived(context):
    context.venue_id = "ven_done"
    pid = _seed_venue(context, context.venue_id)
    context.google.photos_by_place[pid] = _photos(3)
    key = (f"media/source=google_photos/dt={context.today}/"
           f"venue_id={context.venue_id}/existing.jpg")
    context.fake_s3.objects[key] = b"already"


@given("overwrite is requested")
def step_overwrite(context):
    context.config_over["overwrite"] = True


@given('the catalog holds venues "{a}", "{b}", and "{c}"')
def step_catalog_three(context, a, b, c):
    for vid in (a, b, c):
        pid = _seed_venue(context, vid)
        context.google.photos_by_place[pid] = _photos(1)
    context.catalog = [a, b, c]


@given('the catalog holds venue "{vid}"')
def step_catalog_one(context, vid):
    pid = _seed_venue(context, vid)
    context.google.photos_by_place[pid] = _photos(1)


@given("the catalog holds {n:d} active venues")
def step_catalog_n(context, n):
    context.catalog = []
    for i in range(n):
        vid = f"ven_{i}"
        pid = _seed_venue(context, vid)
        context.google.photos_by_place[pid] = _photos(1)
        context.catalog.append(vid)


@given('the run is restricted to the venue ids "{ids}"')
def step_restrict_ids(context, ids):
    context.config_over["venue_ids"] = ids


@given("the run names no venue ids")
def step_no_ids(context):
    context.config_over["venue_ids"] = ""


@given("a venue whose Google photo fetch fails")
def step_google_fails(context):
    context.failing_venue = "ven_bad"
    pid = _seed_venue(context, context.failing_venue)
    context.google.fail_places.add(pid)


@given("a second venue with available photos")
def step_second_venue(context):
    context.good_venue = "ven_good"
    pid = _seed_venue(context, context.good_venue)
    context.google.photos_by_place[pid] = _photos(2)


@given("a venue with {n:d} available Google photos where the second download fails")
def step_download_fails(context, n):
    context.venue_id = "ven_partial"
    pid = _seed_venue(context, context.venue_id)
    photos = _photos(n)
    context.google.photos_by_place[pid] = photos
    context.downloader.fail_urls.add(photos[1]["url"])


@given("a venue with no Google place id")
def step_no_place_id(context):
    context.venue_id = "ven_noplace"
    _seed_venue(context, context.venue_id, place_id=None)


# ── When ──────────────────────────────────────────────────────────────────────
@when("the photo archive job runs for that venue")
def step_run_for_venue(context):
    _run_job(context, venue_ids=context.venue_id)


@when("the photo archive job runs")
def step_run(context):
    _run_job(context)


@when("the photo archive job is triggered")
def step_trigger(context):
    from app.services.venue_photo_archive_service import InvalidArchivePath

    context.rejection = None
    try:
        _run_job(context, venue_ids=getattr(context, "venue_id", "") or "")
    except InvalidArchivePath as e:
        context.rejection = str(e)


@when("the photo archive job completes")
def step_completes(context):
    context.venue_id = "ven_summary"
    pid = _seed_venue(context, context.venue_id)
    context.google.photos_by_place[pid] = _photos(2)
    _run_job(context, venue_ids=context.venue_id)


# ── Then ──────────────────────────────────────────────────────────────────────
@then("{n:d} images are stored for that venue")
def step_n_images(context, n):
    keys = _image_keys(context, context.venue_id)
    assert len(keys) == n, f"expected {n} images, got {len(keys)}: {keys}"


@then("each image is stored under the source, day, and venue partition")
def step_partitioned(context):
    for key in _image_keys(context, context.venue_id):
        assert key.startswith("media/"), key
        assert "source=google_photos/" in key, key
        assert f"dt={context.today}/" in key, key
        assert f"venue_id={context.venue_id}/" in key, key


@then('every media partition is expressed as a "key=value" directory')
def step_key_value(context):
    key = _image_keys(context, context.venue_id)[0]
    # `media/` and `info/` are per-venue containers, not partitions — they
    # split images from everything else and are deliberately not key=value.
    containers = {"media", "info"}
    for directory in key.split("/")[1:-1]:
        if directory in containers:
            continue
        assert "=" in directory, f"partition {directory!r} is not key=value"


@then("a manifest stored beside the images records each photo's author attribution")
def step_manifest(context):
    manifests = [k for k in context.fake_s3.objects if k.endswith("_manifest.json")]
    assert manifests, "no manifest was written"
    body = json.loads(context.fake_s3.objects[manifests[0]])
    entries = body.get("photos", body if isinstance(body, list) else [])
    assert entries, f"manifest holds no photo entries: {body}"
    assert all(e.get("author_name") == "Jane Doe" for e in entries), entries
    context.manifest_entries = entries


@then("the manifest names the photo id and content type of each image")
def step_manifest_fields(context):
    for e in context.manifest_entries:
        assert e.get("photo_id"), e
        assert e.get("content_type"), e


@then("the images are stored under today's day partition")
def step_today_partition(context):
    keys = _image_keys(context)
    assert keys, "nothing stored"
    assert all(f"dt={context.today}/" in k for k in keys), keys


@then("the images are stored under the {day} partition")
def step_specific_partition(context, day):
    keys = [k for k in _image_keys(context) if "ven_old" not in k]
    assert keys, "nothing stored"
    assert all(f"dt={day.strip()}/" in k for k in keys), keys


@then("no new day partition is created")
def step_no_new_partition(context):
    days = {k.split("dt=")[1].split("/")[0] for k in context.fake_s3.objects if "dt=" in k}
    assert context.today not in days, f"a new partition {context.today} was created: {days}"


@then('the images are stored under that prefix')
def step_override_prefix(context):
    prefix = context.config_over["path_override"]
    keys = _image_keys(context)
    assert keys, "nothing stored"
    assert all(k.startswith(prefix) for k in keys), keys


@then("the run is rejected before any Google call is made")
def step_rejected(context):
    assert context.rejection is not None, "the invalid prefix was accepted"
    assert not context.google.calls, f"Google was called: {context.google.calls}"


@then("no images are stored")
def step_no_images(context):
    assert not _image_keys(context), _image_keys(context)


@then("no Google call is made for that venue")
def step_no_google_call(context):
    assert not context.google.calls, f"Google was called: {context.google.calls}"


@then("the venue is reported as skipped")
def step_reported_skipped(context):
    assert context.summary["skipped_existing"] == 1, context.summary


@then("the venue's photos are fetched from Google again")
def step_refetched(context):
    assert context.google.calls, "Google was not called despite overwrite"


@then("the venue is reported as archived")
def step_reported_archived(context):
    assert context.summary["archived"] == 1, context.summary


@then('only venues "{a}" and "{b}" are considered')
def step_only_considered(context, a, b):
    assert context.summary["considered"] == 2, context.summary
    for vid in (a, b):
        assert _image_keys(context, vid), f"{vid} was not archived"


@then('no photos are fetched for "{vid}"')
def step_not_fetched(context, vid):
    assert f"place_{vid}" not in context.google.calls, context.google.calls
    assert not _image_keys(context, vid)


@then('venue "{vid}" is archived')
def step_venue_archived(context, vid):
    assert _image_keys(context, vid), f"{vid} produced no images"


@then('the summary reports "{vid}" as unknown')
def step_unknown_reported(context, vid):
    assert vid in context.summary.get("unknown_venue_ids", []), context.summary


@then("the run completes successfully")
def step_completed_ok(context):
    assert context.summary is not None


@then("all {n:d} venues are considered")
def step_all_considered(context, n):
    assert context.summary["considered"] == n, context.summary


@then("the failing venue is reported as failed")
def step_failed_reported(context):
    assert context.summary["failed"] >= 1, context.summary


@then("the second venue's photos are still archived")
def step_second_archived(context):
    assert _image_keys(context, context.good_venue), "healthy venue was not archived"


@then("the failed photo is counted as a download failure")
def step_download_failure_counted(context):
    assert context.summary["photo_failures"] >= 1, context.summary


@then("the venue is reported as having no place id")
def step_no_place_id_reported(context):
    assert context.summary["no_place_id"] == 1, context.summary


@then("the summary reports the venues considered, skipped, archived, and failed")
def step_summary_counts(context):
    for k in ("considered", "skipped_existing", "archived", "failed"):
        assert k in context.summary, f"summary missing {k}: {context.summary}"


@then("the summary reports the number of photos stored")
def step_summary_photos(context):
    assert context.summary.get("photos_stored", 0) >= 1, context.summary


@then("the summary names the day partition the run wrote to")
def step_summary_prefix(context):
    assert context.summary.get("prefix"), context.summary


@then("the number of photos stored is exposed per source")
def step_metric_photos(context):
    assert _metric("media_archive_photos_stored_total", source="google_photos") > 0


@then("the venue outcomes are exposed per source and result")
def step_metric_venues(context):
    assert _metric("media_archive_venues_total", source="google_photos", result="archived") > 0


@then("the bytes stored are exposed per source")
def step_metric_bytes(context):
    assert _metric("media_archive_bytes_stored_total", source="google_photos") > 0


@then("the timestamp of the last successful run is exposed")
def step_metric_last_success(context):
    assert _metric("media_archive_last_success_timestamp") > 0
