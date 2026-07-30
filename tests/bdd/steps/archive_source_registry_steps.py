"""Behave steps for tests/bdd/enrichment/archive-source-registry.feature.

Drives the REAL service and the REAL registry, faking only the two clients and
S3 — reusing the fakes the v1 archive suite already defines.

Both fakes count their calls, because the property that matters most here is
negative: a run on one source must never reach the other's API.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from behave import given, then, when  # type: ignore[import-untyped]

from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.services.archive_sources import (
    SOURCE_APIFY_GMAPS,
    SOURCE_GOOGLE_PHOTOS,
    public_catalog,
)
from app.services.venue_photo_archive_service import InvalidArchivePath
from tests.bdd.steps.photo_archive_pipeline_v2_steps import _cfg, _run_v2, _seed
from tests.bdd.steps.venue_photo_archive_steps import _run

APIFY_ROOT = f"retrieved/source={SOURCE_APIFY_GMAPS}/"


class _FakeApify:
    """Counts calls; can be told to find nothing or to be out of credit."""

    def __init__(self):
        self.photos_by_query: dict[str, list[dict]] = {}
        self.calls: list[str] = []
        self.no_credit = False

    async def fetch_venue_photos(self, search_query, max_photos=20, language="pt-BR"):
        self.calls.append(search_query)
        if self.no_credit:
            # What the REAL client raises. This fake previously raised the
            # service-layer ArchiveCreditExhausted, so the scenario passed while
            # production silently absorbed exhaustion as one venue's failure and
            # kept calling an empty balance. Faking the vendor's own exception
            # keeps the translation under test.
            raise ApifyCreditExhaustedError("Apify credits exhausted (402)")
        for key, photos in self.photos_by_query.items():
            if key in (search_query or ""):
                # The real client returns photos AND the non-image place data.
                return {
                    "photos": list(photos)[:max_photos],
                    "info": {
                        "title": "Venue", "address": "A street", "phone": "+55",
                        "website": "https://x", "categories": ["Bar"],
                        "totalScore": 4.5, "placeId": "ChIJ",
                    },
                }
        return None


def _attach_apify(context, configured=True):
    context.apify = _FakeApify() if configured else None
    context.service.apify_gmaps_extractor_client = context.apify
    return context.apify


def _apify_photos(n):
    return [
        {"url": f"https://lh3.googleusercontent.com/apify{i}",
         "author_name": "Owner", "photo_name": None}
        for i in range(n)
    ]


# ── Given ─────────────────────────────────────────────────────────────────────
@given("the Apify source is configured")
def step_apify_configured(context):
    _attach_apify(context, configured=True)


@given("the Apify token is not configured")
def step_apify_not_configured(context):
    _attach_apify(context, configured=False)


@given('the run names the source "{source}"')
def step_name_source(context, source):
    context.config_over["source"] = source
    context.venue_id = "ven_src"
    _seed(context, context.venue_id)


@given("a venue the extractor can find with {n:d} photos")
def step_findable_venue(context, n):
    context.venue_id = "ven_found"
    _seed(context, context.venue_id)
    context.apify.photos_by_query[f"Venue {context.venue_id}"] = _apify_photos(n)


@given("a second venue the extractor can find with {n:d} photos")
def step_second_findable(context, n):
    context.good_venue = "ven_found2"
    _seed(context, context.good_venue)
    context.apify.photos_by_query[f"Venue {context.good_venue}"] = _apify_photos(n)


@given("a venue the extractor cannot find")
def step_unfindable_venue(context):
    context.missing_venue = "ven_missing"
    _seed(context, context.missing_venue)   # seeded, but the fake returns None


@given("the Apify account has no credits left")
def step_no_credits(context):
    context.apify.no_credit = True
    context.venue_id = "ven_broke"
    _seed(context, context.venue_id)


# ── When ──────────────────────────────────────────────────────────────────────
@when("the archive job's sources are listed")
def step_list_sources(context):
    container = SimpleNamespace(
        google_places_client=context.google,
        apify_gmaps_extractor_client=getattr(context, "apify", None),
    )
    context.catalog = {s["id"]: s for s in public_catalog(container)}


@when("the photo archive job runs for that venue using the Apify source")
def step_run_apify_venue(context):
    _run_v2(context, venue_ids=context.venue_id, source=SOURCE_APIFY_GMAPS)


@when("the photo archive job runs using the Apify source")
def step_run_apify(context):
    _run_v2(context, source=SOURCE_APIFY_GMAPS)


@when("a cost estimate is requested for the {which} source")
def step_estimate_source(context, which):
    source = SOURCE_APIFY_GMAPS if which.lower() == "apify" else SOURCE_GOOGLE_PHOTOS
    context.estimate = _run(context.service.estimate(_cfg(context, source=source)))


# ── Then ──────────────────────────────────────────────────────────────────────
@then("both the Google and the Apify sources are offered")
def step_both_offered(context):
    assert SOURCE_GOOGLE_PHOTOS in context.catalog, context.catalog
    assert SOURCE_APIFY_GMAPS in context.catalog, context.catalog


@then("the Apify source declares its own configuration fields")
def step_apify_fields(context):
    names = {f["name"] for f in context.catalog[SOURCE_APIFY_GMAPS]["config_schema"]}
    assert {"language"} <= names, names


@then("the Google source declares no extra configuration fields")
def step_google_no_fields(context):
    assert context.catalog[SOURCE_GOOGLE_PHOTOS]["config_schema"] == []


@then("the Apify source is reported unavailable")
def step_apify_unavailable(context):
    assert context.catalog[SOURCE_APIFY_GMAPS]["available"] is False


@then("the reason names the missing Apify token")
def step_reason_names_token(context):
    assert "APIFY_API_TOKEN" in context.catalog[SOURCE_APIFY_GMAPS]["unavailable_reason"]


@then("the Apify source partition holds the images")
def step_stored_under_apify(context):
    keys = [k for k in context.fake_s3.objects if k.startswith(APIFY_ROOT)
            and k.endswith(".jpg")]
    assert keys, f"nothing stored under {APIFY_ROOT}: {list(context.fake_s3.objects)[:3]}"


@then("the unmatched venue is counted as unmatched")
def step_unmatched_counted(context):
    assert context.summary["no_match"] >= 1, context.summary


@then("the matched venue is archived")
def step_other_archived_src(context):
    keys = [k for k in context.fake_s3.objects
            if f"venue_id={context.good_venue}/" in k and k.endswith(".jpg")]
    assert keys, "the findable venue was not archived"


@then("the run stops and reports that the credits are exhausted")
def step_credits_exhausted(context):
    # A clean stop with a summary beats a stack trace: the operator still learns
    # what the run managed to archive before the budget ran out.
    assert context.summary.get("credit_exhausted") is True, context.summary
    assert not context.summary["failed"], (
        "exhausted credits were counted as per-venue failures"
    )


@then("the estimate reports {n:d} billable units")
def step_estimate_units(context, n):
    assert context.estimate["est_units"] == n, context.estimate


@then("the estimate describes the units as {what}")
def step_estimate_unit_label(context, what):
    label = context.estimate["est_unit_label"].lower()
    assert what.rstrip(".").lower().split()[0] in label, (what, label)


@then("the estimate states that the per-image charge is not published")
def step_estimate_image_caveat(context):
    blob = " ".join(context.estimate.get("assumptions", [])).lower()
    assert "not published" in blob or "per-image" in blob, context.estimate


@given("a venue the extractor finds with no photos")
def step_venue_no_photos(context):
    context.venue_id = "ven_infoonly"
    _seed(context, context.venue_id)
    context.apify.photos_by_query[f"Venue {context.venue_id}"] = []


@then("the venue's info folder holds the place data")
def step_info_stored(context):
    keys = [k for k in context.fake_s3.objects if k.endswith("info/place.json")]
    assert keys, f"no place.json written: {list(context.fake_s3.objects)[:4]}"
    context.place_json = json.loads(context.fake_s3.objects[keys[0]])


@then("the place data keeps the fields the scrape already paid for")
def step_info_fields(context):
    place = context.place_json["place"]
    # These come free with the place-scraped event; dropping them would discard
    # data already billed for.
    for f in ("title", "address", "phone", "website", "categories", "totalScore"):
        assert f in place, f"{f} missing from the stored place data: {sorted(place)}"


@then("no image payload is duplicated inside the place data")
def step_info_excludes_images(context):
    place = context.place_json["place"]
    for f in ("images", "imageUrls", "imageCategories"):
        assert f not in place, f"{f} was duplicated into the info JSON"


@then("the venue is counted as info only")
def step_info_only(context):
    assert context.summary["info_only"] >= 1, context.summary
    assert context.summary["info_stored"] >= 1, context.summary


@given("a venue whose photos are {owner:d} from the owner and {visitor:d} from visitors")
def step_mixed_authorship(context, owner, visitor):
    context.venue_id = "ven_mixed"
    _seed(context, context.venue_id)
    photos = (
        [{"url": f"https://x/own{i}", "author_name": "Owner",
          "category": "by_owner", "photo_name": None} for i in range(owner)]
        + [{"url": f"https://x/vis{i}", "author_name": "Someone",
            "category": "by_visitor", "photo_name": None} for i in range(visitor)]
    )
    context.apify.photos_by_query[f"Venue {context.venue_id}"] = photos


@given("at most {n:d} photo per category is kept")
def step_per_category_cap(context, n):
    context.config_over["max_photos_per_category"] = n


def _category_keys(context, category):
    return [k for k in context.fake_s3.objects
            if f"/media/{category}/" in k and k.endswith(".jpg")]


@then("the owner photos are stored in their own folder")
def step_owner_folder(context):
    assert _category_keys(context, "by_owner"), list(context.fake_s3.objects)[:4]


@then("the visitor photos are stored in their own folder")
def step_visitor_folder(context):
    assert _category_keys(context, "by_visitor"), list(context.fake_s3.objects)[:4]


@then("only {n:d} photo is stored in each category folder")
def step_per_category_limit(context, n):
    for category in ("by_owner", "by_visitor"):
        keys = _category_keys(context, category)
        assert len(keys) == n, f"{category}: expected {n}, got {len(keys)}"
