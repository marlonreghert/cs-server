"""Behave steps for tests/bdd/enrichment/instagram-media-archive.feature.

Drives the REAL VenuePhotoArchiveService, the REAL instagram_posts archive
source, and the REAL PhotoClassificationService, faking only three boundaries:
the Apify Instagram client, the HTTP image downloader, and S3 — the same
boundary choice `venue_photo_archive_steps.py` makes for the Google sources,
whose fakes (`_FakeS3`, `_FakeDownloader`) and `_run` event-loop helper this
file reuses rather than duplicating.

Two properties are asserted by counting rather than by outcome alone, because
they are the ones that cost money if they regress:
  * a venue with no Instagram handle must reach ZERO Apify calls, and
  * a carousel capped by `max_photos_per_venue` must reach exactly that many
    DOWNLOADS, not just that many stored images (a fetch that over-downloads
    and trims would pass an outcome-only check).
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from behave import given, then, when  # type: ignore[import-untyped]

from app.api.apify_instagram_client import ApifyCreditExhaustedError, FetchPostsResult
from app.models.instagram import VenueInstagram
from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS
from tests.bdd.steps.photo_classification_steps import _FakeClassifierClient
from tests.bdd.steps.venue_photo_archive_steps import (  # reuse: one set of fakes
    _FakeDownloader,
    _FakeS3,
    _run,
    _seed_venue,
)

SOURCE = SOURCE_INSTAGRAM_POSTS
ROOT = f"retrieved/source={SOURCE}/"
GOOGLE_ROOTS = (
    "retrieved/source=google_photos/",
    "retrieved/source=apify_gmaps_extractor/",
    "retrieved/source=searchapi_gmaps_photos/",
)


# ── the fake Apify Instagram client ──────────────────────────────────────────
class _FakeApifyInstagram:
    """Programmable stand-in for ApifyInstagramClient.fetch_recent_posts.

    Posts are keyed by HANDLE, matching how the real source looks the venue's
    handle up before ever calling this. `exhausted_handles` raises the vendor's
    own exception — not the service-layer ArchiveCreditExhausted — so the
    translation at the source boundary stays under test, the same reasoning
    `archive_source_registry_steps.py` applies to the Maps source.
    """

    def __init__(self) -> None:
        self.posts_by_handle: dict[str, list[dict]] = {}
        # A callable beats a static list for the id-stability scenario, which
        # needs a DIFFERENT signed url on each observation of the SAME post.
        self.posts_factory_by_handle: dict[str, Callable[[], list[dict]]] = {}
        self.exhausted_handles: set[str] = set()
        self.calls: list[str] = []

    async def fetch_recent_posts(self, username: str, results_limit: int = 10):
        self.calls.append(username)
        if username in self.exhausted_handles:
            raise ApifyCreditExhaustedError("Apify credits exhausted (402)")
        factory = self.posts_factory_by_handle.get(username)
        if factory:
            return FetchPostsResult(posts=list(factory())[:results_limit])
        return FetchPostsResult(posts=list(self.posts_by_handle.get(username, []))[:results_limit])


def _single_post(shortcode: str, *, caption: str = "", likes: int = 1,
                  comments: int = 0, timestamp: str = "2026-08-01T20:00:00.000Z",
                  url: Optional[str] = None) -> dict:
    return {
        "caption": caption,
        "likes_count": likes,
        "comments_count": comments,
        "timestamp": timestamp,
        "post_type": "image",
        "shortcode": shortcode,
        "permalink": f"https://instagram.com/p/{shortcode}/",
        "image_urls": [url or f"https://instagram.cdn.example/{shortcode}.jpg"],
    }


def _carousel_post(shortcode: str, n_children: int) -> dict:
    urls = [f"https://instagram.cdn.example/{shortcode}_{i}.jpg" for i in range(n_children)]
    return {
        "caption": "",
        "likes_count": 0,
        "comments_count": 0,
        "timestamp": "2026-08-01T20:00:00.000Z",
        "post_type": "carousel",
        "shortcode": shortcode,
        "permalink": f"https://instagram.com/p/{shortcode}/",
        "image_urls": urls,
    }


# ── build ─────────────────────────────────────────────────────────────────────
def _build_instagram(context) -> None:
    from app.dao.media_archive_store import MediaArchiveStore
    from app.services.photo_classification_service import PhotoClassificationService
    from app.services.venue_photo_archive_service import VenuePhotoArchiveService

    context.fake_s3 = _FakeS3()
    context.downloader = _FakeDownloader()
    context.config_over = {}
    context.today = "2026-08-04"
    context.apify_instagram = _FakeApifyInstagram()
    # Wired unconditionally: instagram_posts never provides its own categories
    # (provides_categories=False), so every run classifies. A benign default
    # verdict keeps every scenario that does not care about the category
    # passing unaffected; the flyer scenario programs the one url it cares
    # about.
    context.classifier_client = _FakeClassifierClient()
    context.classifier = PhotoClassificationService(
        client=context.classifier_client,
        confidence_threshold=0.6,
        attribute_confidence_threshold=0.8,
        batch_size=10,
    )
    context.store = MediaArchiveStore(
        bucket="vibesense-datalake-test", region="us-east-1", s3_client=context.fake_s3
    )
    context.service = VenuePhotoArchiveService(
        google_places_client=None,
        venue_dao=context.repository,
        media_store=context.store,
        downloader=context.downloader,
        apify_instagram_client=context.apify_instagram,
        photo_classifier=context.classifier,
        max_photos_per_venue=10,
        today_provider=lambda: context.today,
    )


def _seed_instagram_venue(context, vid: str, handle: Optional[str] = None) -> str:
    handle = handle or f"handle_{vid}"
    _seed_venue(context, vid, place_id=None)
    context.repository.set_venue_instagram(
        VenueInstagram(venue_id=vid, instagram_handle=handle, status="found")
    )
    return handle


def _seed_previous_ig_run(context, venue_id: str, day: str = "2026-08-03") -> str:
    prefix = (
        f"{ROOT}year={day[:4]}/month={day[5:7]}/day={day[8:10]}/"
        f"run_id=0000000000{'0' * 16}/"
    )
    context.fake_s3.objects[f"{prefix}venue_id={venue_id}/media/old.jpg"] = b"old"
    return prefix


def _cfg(context, **over):
    cfg = {
        "source": SOURCE,
        "path_mode": "new_run",
        "max_venues": 100,
        "max_photos_per_venue": 10,
        "skip_scope": "latest_run",
        "overwrite": False,
        "dry_run": False,
    }
    cfg.update(getattr(context, "config_over", {}))
    cfg.update(over)
    return cfg


def _run_instagram(context, **over):
    context.summary = _run(context.service.run(_cfg(context, **over)))
    return context.summary


def _image_keys(context, venue_id: Optional[str] = None) -> list[str]:
    keys = [k for k in context.fake_s3.objects if k.endswith(".jpg")]
    if venue_id:
        keys = [k for k in keys if f"venue_id={venue_id}/" in k]
    return keys


def _manifest_for(context, venue_id: str, prefix: Optional[str] = None) -> Optional[dict]:
    for key, body in context.fake_s3.objects.items():
        if not key.endswith("_manifest.json"):
            continue
        if f"venue_id={venue_id}/" not in key:
            continue
        if prefix is not None and not key.startswith(prefix):
            continue
        return json.loads(body)
    return None


def _first_manifest_entry(context, venue_id: str) -> dict:
    manifest = _manifest_for(context, venue_id)
    assert manifest and manifest.get("photos"), f"no manifest entries for {venue_id}: {manifest}"
    return manifest["photos"][0]


# ── Background ────────────────────────────────────────────────────────────────
@given("the Apify Instagram client is configured")
def step_ig_client_configured(context):
    _build_instagram(context)


@given('the archive source "{source_id}" is offered in the source catalog')
def step_source_offered(context, source_id):
    from types import SimpleNamespace

    from app.services.archive_sources import public_catalog

    container = SimpleNamespace(apify_instagram_client=context.apify_instagram)
    catalog = {s["id"]: s for s in public_catalog(container)}
    assert source_id in catalog, catalog
    assert catalog[source_id]["available"] is True, catalog[source_id]


# ── Given ─────────────────────────────────────────────────────────────────────
@given("a venue with a confirmed Instagram handle")
def step_venue_with_handle(context):
    context.venue_id = "ven_ig"
    context.handle = _seed_instagram_venue(context, context.venue_id)


@given("a venue with no Instagram handle")
def step_venue_no_handle(context):
    context.venue_id = "ven_nohandle"
    _seed_venue(context, context.venue_id, place_id=None)
    # No set_venue_instagram call: get_venue_instagram(...) returns None, and
    # has_instagram() would be False even if a rejected record existed.


@given("that handle has {n:d} recent single-image posts")
def step_n_single_posts(context, n):
    posts = [_single_post(f"sc{context.venue_id}_{i}") for i in range(n)]
    context.apify_instagram.posts_by_handle[context.handle] = posts


@given("that handle has one carousel post with {n:d} child images")
def step_one_carousel(context, n):
    post = _carousel_post(f"car_{context.venue_id}", n)
    context.apify_instagram.posts_by_handle[context.handle] = [post]


@given("the run caps images per venue at {n:d}")
def step_cap_images(context, n):
    context.config_over["max_photos_per_venue"] = n


@given("that handle has {p:d} carousel posts of {c:d} child images each")
def step_multi_carousel(context, p, c):
    posts = [_carousel_post(f"car{i}_{context.venue_id}", c) for i in range(p)]
    context.apify_instagram.posts_by_handle[context.handle] = posts


@given("that handle has a post with a caption and a timestamp")
def step_post_caption_timestamp(context):
    context.expect_caption = "Great night out!"
    context.expect_timestamp = "2026-08-01T23:15:00.000Z"
    context.expect_shortcode = f"caps_{context.venue_id}"
    post = _single_post(
        context.expect_shortcode, caption=context.expect_caption,
        timestamp=context.expect_timestamp,
    )
    context.apify_instagram.posts_by_handle[context.handle] = [post]


@given("that handle has a post whose image url signature differs between "
       "two observations")
def step_rotating_signature(context):
    shortcode = f"rot_{context.venue_id}"
    counter = {"n": 0}

    def _factory():
        counter["n"] += 1
        url = f"https://instagram.cdn.example/{shortcode}.jpg?sig=v{counter['n']}"
        return [_single_post(shortcode, url=url)]

    context.apify_instagram.posts_factory_by_handle[context.handle] = _factory


@given("that handle has a post whose image is a promotional poster")
def step_poster_post(context):
    post = _single_post(f"poster_{context.venue_id}")
    context.apify_instagram.posts_by_handle[context.handle] = [post]
    url = post["image_urls"][0]
    context.classifier_client.verdicts[url] = {"category": "flyer", "confidence": 0.9}


@given("that handle has {n:d} posts and one of their images fails to download")
def step_n_posts_one_fails(context, n):
    posts = [_single_post(f"failpost{i}_{context.venue_id}") for i in range(n)]
    context.apify_instagram.posts_by_handle[context.handle] = posts
    context.downloader.fail_urls.add(posts[1]["image_urls"][0])


@given("the Apify Instagram client reports credit exhaustion on the second venue")
def step_credit_exhaustion_second(context):
    context.venue_ids = []
    for i in range(5):
        vid = f"ven_ce{i}"
        handle = _seed_instagram_venue(context, vid, handle=f"ce_handle{i}")
        context.apify_instagram.posts_by_handle[handle] = [_single_post(f"ce_sc{i}")]
        context.venue_ids.append(vid)
    # The second id in the eligibility list is the second venue processed:
    # `select_venues` for mode=venue_ids preserves the requested order, and
    # concurrency is pinned to 1 below so the run is strictly sequential —
    # otherwise "stops after the second venue" would not be a stable claim.
    second_handle = "ce_handle1"
    context.apify_instagram.exhausted_handles.add(second_handle)
    context.service.concurrency = 1


@given('a venue archived by the previous run of the source "{source}"')
def step_prev_run_venue(context, source):
    context.venue_id = "ven_prev_ig"
    context.handle = _seed_instagram_venue(context, context.venue_id)
    context.apify_instagram.posts_by_handle[context.handle] = [_single_post("prevsc")]
    _seed_previous_ig_run(context, context.venue_id)


@given("the run skips against the latest run")
def step_skip_latest(context):
    context.config_over["skip_scope"] = "latest_run"


@given("{n:d} venues with confirmed Instagram handles")
def step_n_ig_venues(context, n):
    context.venue_ids = []
    for i in range(n):
        vid = f"ven_est{i}"
        handle = _seed_instagram_venue(context, vid, handle=f"est_handle{i}")
        context.apify_instagram.posts_by_handle[handle] = [_single_post(f"est_sc{i}")]
        context.venue_ids.append(vid)


# ── When ──────────────────────────────────────────────────────────────────────
@when('the archive runs for the source "{source}"')
def step_run_archive(context, source):
    _run_instagram(context, source=source, venue_ids=context.venue_id)


@when('the archive runs for the source "{source}" over {n:d} venues')
def step_run_over_n(context, source, n):
    _run_instagram(context, source=source, venue_ids=",".join(context.venue_ids[:n]))


@when('the archive runs twice for the source "{source}"')
def step_run_twice(context, source):
    _run_instagram(context, source=source, venue_ids=context.venue_id, path_mode="new_run")
    context.first_prefix = context.summary["prefix"]
    _run_instagram(
        context, source=source, venue_ids=context.venue_id, path_mode="new_run",
        skip_scope="none", overwrite=True,
    )
    context.second_prefix = context.summary["prefix"]


@when('a run is estimated for the source "{source}"')
def step_estimate_run(context, source):
    context.estimate = _run(context.service.estimate(
        _cfg(context, source=source, venue_ids=",".join(context.venue_ids))
    ))


# ── Then ──────────────────────────────────────────────────────────────────────
@then('{n:d} images are stored under the source folder "{source}"')
def step_n_under_source_folder(context, n, source):
    root = f"retrieved/source={source}/"
    keys = [k for k in context.fake_s3.objects if k.startswith(root) and k.endswith(".jpg")]
    assert len(keys) == n, f"expected {n} under {root}, got {len(keys)}: {keys}"


@then("no image is stored under any Google source folder")
def step_no_google_folder(context):
    keys = [
        k for k in context.fake_s3.objects
        if any(k.startswith(r) for r in GOOGLE_ROOTS) and k.endswith(".jpg")
    ]
    assert not keys, keys


@then("each stored image carries its carousel index in the manifest")
def step_carousel_index_present(context):
    manifest = _manifest_for(context, context.venue_id)
    entries = (manifest or {}).get("photos") or []
    assert entries, f"no manifest entries for {context.venue_id}: {manifest}"
    for e in entries:
        assert e.get("carousel_index") is not None, e


@then("exactly {n:d} images are downloaded")
def step_n_downloaded(context, n):
    assert len(context.downloader.calls) == n, context.downloader.calls


@then('that venue is reported with the outcome "{outcome}"')
def step_reported_outcome(context, outcome):
    assert context.summary.get(outcome, 0) >= 1, context.summary


@then("no Apify actor run is started")
def step_no_apify_call(context):
    assert not context.apify_instagram.calls, context.apify_instagram.calls


@then("no Apify actor run is started for that venue")
def step_no_apify_for_venue(context):
    assert context.handle not in context.apify_instagram.calls, context.apify_instagram.calls


@then('that outcome is counted separately from "{other}"')
def step_counted_separately(context, other):
    assert context.summary.get(other, 0) == 0, context.summary


@then("the manifest entry for that image carries the caption")
def step_manifest_caption(context):
    entry = _first_manifest_entry(context, context.venue_id)
    assert entry.get("caption") == context.expect_caption, entry


@then("the manifest entry carries the post permalink")
def step_manifest_permalink(context):
    entry = _first_manifest_entry(context, context.venue_id)
    assert entry.get("permalink"), entry


@then("the manifest entry carries the post shortcode")
def step_manifest_shortcode(context):
    entry = _first_manifest_entry(context, context.venue_id)
    assert entry.get("shortcode") == context.expect_shortcode, entry


@then("the manifest entry carries the post timestamp")
def step_manifest_timestamp(context):
    entry = _first_manifest_entry(context, context.venue_id)
    assert entry.get("uploaded_at") == context.expect_timestamp, entry


@then("both runs derive the same photo id for that image")
def step_same_photo_id(context):
    m1 = _manifest_for(context, context.venue_id, prefix=context.first_prefix)
    m2 = _manifest_for(context, context.venue_id, prefix=context.second_prefix)
    assert m1 and m1.get("photos"), m1
    assert m2 and m2.get("photos"), m2
    id1 = m1["photos"][0]["photo_id"]
    id2 = m2["photos"][0]["photo_id"]
    assert id1 == id2, (id1, id2)


@then('that image is classified as "{category}"')
def step_classified_as(context, category):
    keys = [
        k for k in context.fake_s3.objects
        if f"/media/{category}/" in k and k.endswith(".jpg")
    ]
    assert keys, f"no image classified as {category}: {list(context.fake_s3.objects)[:5]}"


@then('that image is stored under the category folder "{category}"')
def step_stored_under_category(context, category):
    keys = [k for k in context.fake_s3.objects if f"/media/{category}/" in k]
    assert keys, f"nothing under folder {category}: {list(context.fake_s3.objects)[:5]}"


@then('the failed image is counted with the result "{result}"')
def step_failed_counted(context, result):
    if result == "failed":
        assert context.summary.get("photo_failures", 0) >= 1, context.summary
    else:
        assert context.summary.get(result, 0) >= 1, context.summary


@then("the run stops after the second venue")
def step_stops_after_second(context):
    assert context.summary.get("credit_exhausted") is True, context.summary
    assert context.summary["archived"] == 1, context.summary


@then("no further Apify actor run is started")
def step_no_further_calls(context):
    # ven_ce0 (archived) + ven_ce1 (raised) = 2 calls; venues 3-5 must never be
    # reached once the run recorded credit_exhausted.
    assert len(context.apify_instagram.calls) == 2, context.apify_instagram.calls


@then("the run record is saved")
def step_run_record_saved(context):
    job_id = context.summary.get("job_id")
    assert job_id, context.summary
    record = context.service.get_run_record(job_id)
    assert record is not None, "no run record was saved"


@then('an estimated cost is returned in billable units of "{label}"')
def step_estimate_label(context, label):
    assert context.estimate.get("est_unit_label") == label, context.estimate
    assert context.estimate.get("est_cost_usd") is not None, context.estimate


@then("no object is written")
def step_no_object_written(context):
    assert not context.fake_s3.objects, list(context.fake_s3.objects)[:5]
