"""Behave steps for tests/bdd/enrichment/classify-bytes-and-durable-covers.feature.

Four defects, one 2026-08-07 production RCA:

1. The live archive path handed OpenAI a provider CDN url to fetch
   server-side. Fine for Google, fatal for Instagram's signed fbcdn links
   (400 invalid_image_url). Reuses `photo_classification_steps.py`'s
   established venue-photo-archive fixtures for the mechanism itself — the
   fix (app/services/photo_classification_service.py's `require_bytes`) is
   source-agnostic, so proving it once over the same real
   VenuePhotoArchiveService + PhotoClassificationService pairing that file
   already exercises is enough; only the photo urls here are styled like a
   real Instagram CDN link, to match what the RCA actually reproduced.
2. The 400 handler misreported an image-fetch failure as a rejected
   parameter. Drives the REAL OpenAIPhotoClassifierClient over a fake
   low-level OpenAI SDK object that raises a real `openai.BadRequestError`
   (mirrors tests/test_photo_classifier_error_handling.py, at the BDD layer).
3. Promoter events never carried their archived cover. Drives the REAL
   PromoterCrawlService over the same fakes
   instagram_promoter_events_steps.py already established (imported, not
   duplicated).
4. The evidence gate's default bound left most of the category-gate output
   unevaluated. Reuses event_venue_targeting_steps.py's REAL
   EventVenueTargetingService fixtures.

No live S3/OpenAI/Apify — every external boundary is a fake, per CLAUDE.md.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone

import httpx
from behave import given, then, when  # type: ignore[import-untyped]
from openai import BadRequestError

import tests.bdd.steps.event_venue_targeting_steps as evt
import tests.bdd.steps.instagram_promoter_events_steps as ipe
import tests.bdd.steps.photo_classification_steps as pcs
import tests.bdd.steps.venue_photo_archive_steps as vpa_steps
from app.dao.venue_repository import VenueRepository
from app.services.event_venue_targeting import (
    ArchivedFlyerEvidenceSource,
    EventVenueTargetingService,
    TIER_UNEVALUATED,
)
from app.services.promoter_crawl_service import PromoterCrawlService
from tests.rds_fake import InMemoryRdsVenueStore

# A realistic signed Instagram CDN url — the RCA's exact failure shape
# (host + query-string signature), never to be logged in full.
CBC_INSTAGRAM_URL = (
    "https://instagram.fper12-1.fna.fbcdn.net/v/t51.82787-15/photo.jpg"
    "?_nc_ht=instagram.fper12-1.fna.fbcdn.net&SECRETTOKEN=abc123"
)


def _prepare_media_fixtures(context) -> None:
    """The three boundary fakes venue-photo-archive scenarios need, without
    relying on a shared Background (this feature also has promoter- and
    event-targeting scenarios that need none of this)."""
    context.fake_s3 = vpa_steps._FakeS3()
    context.google = vpa_steps._FakeGoogle()
    context.downloader = vpa_steps._FakeDownloader()
    context.config_over = {}
    context.today = "2026-07-26"


# ── log capture (mirrors event_cover_presign_steps.py's pattern) ────────────
class _ListLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _install_log_capture(context) -> "_ListLogHandler":
    handler = _ListLogHandler()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    context.add_cleanup(lambda: root.removeHandler(handler))
    return handler


def _cbc_log_messages(context) -> str:
    return " ".join(r.getMessage() for r in context.cbc_log_handler.records)


# ── a REAL BadRequestError, and a fake low-level SDK that always raises one ──
def _bad_request_error(*, code, param, message) -> BadRequestError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(400, request=request)
    body = {"message": message, "type": "invalid_request_error", "param": param, "code": code}
    return BadRequestError(message, response=response, body=body)


class _CBCChat:
    def __init__(self, outer):
        self.completions = outer


class _CBCFailingSDK:
    """Always raises the same BadRequestError, whatever it was asked to
    classify — bytes or url makes no difference to THIS fake; only
    OpenAIPhotoClassifierClient's own error handling (defect 2) is under
    test here."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.chat = _CBCChat(self)

    async def create(self, **kw):
        raise self.exc


# ── given: classify from bytes (scenarios 1, 2) ──────────────────────────────
@given("an Instagram post whose image has been downloaded")
def step_given_ig_post_downloaded(context):
    _prepare_media_fixtures(context)
    pcs._build_with_classifier(context)
    pcs._one_photo(
        context, "ven_cbc_bytes",
        verdict={"category": "interior", "confidence": 0.9},
        url=CBC_INSTAGRAM_URL,
    )
    context.cbc_original_url = CBC_INSTAGRAM_URL


# ── given: re-deriving over an archived run (scenario 3) ─────────────────────
@given("an archived run whose attributes are re-derived")
def step_given_rederive(context):
    _prepare_media_fixtures(context)
    pcs._build_with_classifier(context)
    pcs._one_photo(
        context, "ven_cbc_rederive", verdict={"category": "interior", "confidence": 0.9},
    )
    pcs._run_job(context, venue_ids=context.venue_id)
    # The classifier is about to be asked again, over the ARCHIVED copies —
    # clear so the assertions below see only the re-derive pass's calls.
    context.classifier_client.classify_calls.clear()
    context.google.calls.clear()
    pcs._register_for_stored_copies(
        context, {"category": "interior", "confidence": 0.9},
    )


# ── given: an unfetchable image (scenarios 4, 5, 6) ──────────────────────────
@given("the model cannot fetch an image")
def step_given_model_cannot_fetch(context):
    from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient
    from app.dao.media_archive_store import MediaArchiveStore
    from app.metrics import PHOTO_CLASSIFICATION_FALLBACKS_TOTAL
    from app.services.photo_classification_service import PhotoClassificationService
    from app.services.venue_photo_archive_service import VenuePhotoArchiveService

    _prepare_media_fixtures(context)

    real_client = OpenAIPhotoClassifierClient(api_key="test-key", model="gpt-5.6-luna")
    exc = _bad_request_error(
        code="invalid_image_url", param=None,
        message=f"Error while downloading {CBC_INSTAGRAM_URL}.",
    )
    real_client.client = _CBCFailingSDK(exc)
    context.classifier_client = real_client
    context.classifier = PhotoClassificationService(
        client=real_client, confidence_threshold=0.6,
        attribute_confidence_threshold=0.8, batch_size=10,
    )
    context.store = MediaArchiveStore(
        bucket="vibesense-datalake-test", region="us-east-1", s3_client=context.fake_s3,
    )
    context.service = VenuePhotoArchiveService(
        google_places_client=context.google,
        venue_dao=context.repository,
        media_store=context.store,
        downloader=context.downloader,
        photo_classifier=context.classifier,
        max_photos_per_venue=10,
        today_provider=lambda: getattr(context, "today", "2026-07-26"),
    )
    context.venue_id = "ven_cbc_unfetchable"
    place_id = vpa_steps._seed_venue(context, context.venue_id)
    context.google.photos_by_place[place_id] = [
        {"url": CBC_INSTAGRAM_URL, "photo_name": "places/x/photos/ref0"}
    ]
    context.cbc_metric_before = (
        PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="image_fetch_failed")._value.get() or 0
    )
    context.cbc_log_handler = _install_log_capture(context)


# ── when ──────────────────────────────────────────────────────────────────────
@when("the archive classifies it")
def step_when_archive_classifies(context):
    context.cbc_summary = pcs._run_job(context)


@when("the archived photos are classified")
def step_when_rederive_runs(context):
    context.cbc_rederived = pcs._run(context.service.rederive_attributes(pcs.SOURCE))


# ── then: classify from bytes ────────────────────────────────────────────────
@then("the photo is stored under its classified category")
def step_then_stored_under_category(context):
    keys = pcs._image_keys(context, context.venue_id)
    assert keys, f"nothing archived for {context.venue_id}"
    assert all("/media/interior/" in k for k in keys), keys


@then("the manifest entry records that category")
def step_then_manifest_records_category(context):
    entry = pcs._entry(context)
    assert entry["category"] == "interior", entry


@then("the model receives the image as bytes")
def step_then_receives_bytes(context):
    calls = context.classifier_client.classify_calls
    assert calls, "the classifier was never called"
    sent = calls[-1]
    assert sent, "an empty batch was sent"
    assert all(u.startswith("data:image/") for u in sent), sent


@then("the model receives each photo's own archived bytes")
def step_then_receives_archived_bytes(context):
    calls = context.classifier_client.classify_calls
    assert calls, "no re-derive pass ran"
    urls = [u for batch in calls for u in batch]
    assert urls and all(u.startswith("data:image/") for u in urls), [u[:40] for u in urls]
    keys = pcs._image_keys(context, context.venue_id)
    assert keys, "nothing archived to compare against"
    stored_b64 = base64.b64encode(context.fake_s3.objects[keys[0]]).decode("ascii")
    assert any(stored_b64 in u for u in urls), (
        "the data URI sent to the model did not match the archived bytes"
    )


@then("no provider url is sent to the model")
def step_then_no_provider_url(context):
    calls = context.classifier_client.classify_calls
    refs = [u for batch in calls for u in batch]
    assert refs, "the classifier was never called"
    assert not any(u.startswith("http") for u in refs), refs
    original = getattr(context, "cbc_original_url", None)
    if original:
        assert not any(original in u for u in refs), (
            "the provider url leaked into what the model received"
        )


@then("the failure is reported as an image failure")
def step_then_reported_as_image_failure(context):
    messages = _cbc_log_messages(context)
    assert "image" in messages.lower() and "fetch" in messages.lower(), messages


@then("the failure is not reported as a rejected parameter")
def step_then_not_reported_as_param(context):
    messages = _cbc_log_messages(context)
    assert "rejected parameter" not in messages, messages


@then("it is counted under its own reason")
def step_then_counted_under_own_reason(context):
    from app.metrics import PHOTO_CLASSIFICATION_FALLBACKS_TOTAL

    after = PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="image_fetch_failed")._value.get() or 0
    assert after > context.cbc_metric_before, (
        "the image_fetch_failed reason was never counted"
    )


@then("the photo is still archived")
def step_then_still_archived(context):
    keys = pcs._image_keys(context, context.venue_id)
    assert keys, "the photo was lost rather than kept archived under its source category"


@then("it keeps the category its source gave it")
def step_then_keeps_source_category(context):
    entries = pcs._entries(context)
    assert entries, "nothing archived"
    for entry in entries:
        assert entry.get("category") == entry.get("source_category"), entry


@then("no signed url appears in the classifier logs")
def step_then_no_signed_url_in_logs(context):
    messages = _cbc_log_messages(context)
    assert "SECRETTOKEN" not in messages, "the signed query string leaked into a log line"
    assert CBC_INSTAGRAM_URL not in messages, "the full signed url leaked into a log line"


# ── given/when/then: promoter cover photo (scenarios 7, 8, 9) ───────────────
PROMOTER_HANDLE = "promo_cbc"
PROMOTER_SHORTCODE = "cbc_post"


def _build_promoter_fixture(context) -> None:
    context.cbc_posts_client = ipe._FakePromoterPostsClient()
    context.cbc_openai = ipe._FakeOpenAIClient()
    context.cbc_media_store = ipe._FakeMediaStore()
    context.cbc_downloader = ipe._FakeDownloader()
    context.cbc_dao = VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())
    context.cbc_dao.upsert_promoter_account(PROMOTER_HANDLE, {"status": "active"})
    context.cbc_service = PromoterCrawlService(
        venue_dao=context.cbc_dao,
        posts_client=context.cbc_posts_client,
        media_store=context.cbc_media_store,
        downloader=context.cbc_downloader,
        openai_client=context.cbc_openai,
    )
    post = {
        "shortcode": PROMOTER_SHORTCODE,
        "caption": "Ingressos abertos! Vem pro role.",
        "permalink": "https://instagram.com/p/cbc_post",
        "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
        "image_urls": ["https://instagram.cdn.example/cbc_post.jpg"],
    }
    context.cbc_posts_client.posts_by_handle[PROMOTER_HANDLE] = [post]
    context.cbc_openai.program(ipe._extraction_json())


@given("a promoter post whose image has been archived")
def step_given_promoter_post_with_image(context):
    _build_promoter_fixture(context)


@given("that post announces three events")
def step_given_post_announces_three_events(context):
    class _MultiEventClient(ipe._FakeOpenAIClient):
        """Returns the programmed multi-event payload as-is — the base fake
        wraps a single flat event dict, which cannot express a roundup."""

        async def extract_events(self, *, caption, image_data_uri=None, max_events):
            self.calls += 1
            item = self._responses.pop(0)
            return item, False

    client = _MultiEventClient()
    client.program(json.dumps({"events": [
        json.loads(ipe._extraction_json(title="Festa A")),
        json.loads(ipe._extraction_json(title="Festa B")),
        json.loads(ipe._extraction_json(title="Festa C")),
    ]}))
    context.cbc_openai = client
    context.cbc_service.openai_client = client


@given("a promoter post whose images could not be archived")
def step_given_promoter_post_no_image(context):
    _build_promoter_fixture(context)
    context.cbc_posts_client.posts_by_handle[PROMOTER_HANDLE][0]["image_urls"] = []


@when("the promoter crawl extracts an event from it")
def step_when_promoter_extracts_one(context):
    context.cbc_report = ipe._run(context.cbc_service.run({}))


@when("the promoter crawl extracts them")
def step_when_promoter_extracts_many(context):
    context.cbc_report = ipe._run(context.cbc_service.run({}))


@then("the event records the archived cover key")
def step_then_event_has_cover(context):
    row = context.cbc_dao.get_event_by_source(PROMOTER_HANDLE, PROMOTER_SHORTCODE)
    assert row is not None, "no event was persisted"
    assert row["cover_photo_key"], row
    context.cbc_event_row = row


@then("the event still records its source permalink")
def step_then_event_has_permalink(context):
    row = getattr(context, "cbc_event_row", None) or context.cbc_dao.get_event_by_source(
        PROMOTER_HANDLE, PROMOTER_SHORTCODE
    )
    assert row["source_permalink"] == "https://instagram.com/p/cbc_post", row


@then("all three events record the archived cover key")
def step_then_all_three_have_cover(context):
    rows = context.cbc_dao.list_events_by_source(PROMOTER_HANDLE, PROMOTER_SHORTCODE)
    assert len(rows) == 3, rows
    covers = {r["cover_photo_key"] for r in rows}
    assert len(covers) == 1 and all(covers), rows


@then("the event records no cover key")
def step_then_event_no_cover(context):
    row = context.cbc_dao.get_event_by_source(PROMOTER_HANDLE, PROMOTER_SHORTCODE)
    assert row is not None, "no event was persisted"
    assert row["cover_photo_key"] is None, row
    context.cbc_event_row = row


@then("the event is still persisted")
def step_then_event_persisted(context):
    row = getattr(context, "cbc_event_row", None) or context.cbc_dao.get_event_by_source(
        PROMOTER_HANDLE, PROMOTER_SHORTCODE
    )
    assert row is not None, "the event must persist even without a cover"


# ── given/when/then: the evidence gate's default bound (scenarios 10, 11) ───
@when("event targeting runs with its default bound")
def step_when_default_bound(context):
    evt._run_targeting(context)


@then("all {n:d} venues are evidence-evaluated")
def step_then_all_venues_evaluated(context, n):
    assert context.event_result is not None
    assert context.event_result["evidence_evaluated"] == n, context.event_result


class _ListingCountingMediaStore:
    """Counts S3 listing calls so the handle-less short-circuit is proven on
    the metric the plan cares about — the LISTING CALL COUNT, not just the
    outcome. Raises on read_manifest: a handle-less venue must never get far
    enough to need one."""

    def __init__(self) -> None:
        self.list_run_prefixes_calls = 0

    async def list_run_prefixes(self, source):
        self.list_run_prefixes_calls += 1
        return []

    async def read_manifest(self, prefix, venue_id):
        raise AssertionError(
            "BDD harness: read_manifest must not run for a handle-less venue"
        )


@given("a venue that passes the category gate but has no Instagram handle")
def step_given_venue_no_handle(context):
    evt._ensure_context(context)
    evt._create_venue(context, "NIGHTCLUB")
    # Swapped for the REAL ArchivedFlyerEvidenceSource (not the generic
    # in-memory fake this feature's Background otherwise wires) so the
    # assertion below is on an actual S3-listing call count, not a stand-in.
    context.cbc_no_handle_media_store = _ListingCountingMediaStore()
    context.event_service = EventVenueTargetingService(
        venue_dao=context.event_dao,
        redis_client=context.event_redis,
        flyer_evidence_source=ArchivedFlyerEvidenceSource(
            context.cbc_no_handle_media_store, archive_source="instagram_posts",
        ),
    )


@then("that venue is recorded as unevaluated")
def step_then_venue_recorded_unevaluated(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile is not None, "no venue_event_profile was written"
    assert profile["tier"] == TIER_UNEVALUATED, profile


@then("no archive listing is performed for it")
def step_then_no_archive_listing(context):
    assert context.cbc_no_handle_media_store.list_run_prefixes_calls == 0, (
        "the handle-less short-circuit must run BEFORE any S3 listing"
    )
