"""Behave steps for tests/bdd/enrichment/photo-classification.feature.

Drives the REAL VenuePhotoArchiveService and the REAL PhotoClassificationService
over a fake vision client, so the validation, the confidence gate and the
fallbacks under test are all real code — only the model call is faked.

The fake is programmed PER PHOTO URL rather than per call, because the pipeline
batches and the scenarios must not depend on how the batches happen to split.

Attribute values are written in the scenarios as bare values and wrapped into
the `{"value": ..., "confidence": ...}` shape the model actually returns, at a
confidence above the floor. A scenario that cares about confidence says so by
passing the pair itself — that is the only place the shape appears, so the
scenarios stay about behaviour rather than about JSON.

Three properties are asserted by counting, because they are the ones that cost
money if they regress:
  * a source that already knows its categories must reach the classifier zero
    times,
  * a venue's photos must classify in one call rather than one per photo, and
  * a run with attributes disabled must never ask for them.
"""
from __future__ import annotations

import base64
import json

from behave import given, then, when  # type: ignore[import-untyped]

from tests.bdd.steps.venue_photo_archive_steps import (  # reuse: one set of fakes
    _run,
    _seed_venue,
)

SOURCE = "google_photos"

# Comfortably above the 0.8 attribute floor the service is built with below.
SURE = 0.95
# Comfortably below it.
UNSURE = 0.4


def _sure(value):
    """A scenario's bare value, in the shape the model answers with."""
    return {"value": value, "confidence": SURE}


def _wrap(block):
    """Wrap a scenario's plain {field: value} into per-attribute confidences.

    A field whose value is already a dict is passed through untouched, which is
    how a scenario expresses an unsure answer or a malformed one.
    """
    return {
        field: value if isinstance(value, dict) else _sure(value)
        for field, value in (block or {}).items()
    }


# The venue_photo_archive_steps._FakeDownloader convention: bytes are always
# b"\xff\xd8\xff" + url.encode(). Deterministic, so a data URI built from
# those bytes is invertible — see _registered_verdict below.
_FAKE_JPEG_MAGIC = b"\xff\xd8\xff"


def _decoded_url(ref: str):
    """Recover the ORIGINAL photo url from a data URI built by the fake
    downloader's bytes, or None if `ref` is not one of those (e.g. it is
    already a plain url, or a data URI registered directly by
    `_register_for_stored_copies` for a re-derive scenario).

    The live archive path now sends the classifier bytes, not a url (see
    PhotoClassificationService._image_reference / VenuePhotoArchiveService.
    _attach_download_bytes) — Instagram's signed CDN links 400 when OpenAI
    tries to fetch them itself. This fake's `verdicts` dict stayed keyed by
    URL for readability across ~40 scenarios, so this is what lets a live
    scenario's registration still be found under the new encoding, without
    touching any of those scenarios or weakening what they assert.
    """
    if not ref.startswith("data:"):
        return None
    _, _, b64 = ref.partition(",")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if not raw.startswith(_FAKE_JPEG_MAGIC):
        return None
    return raw[len(_FAKE_JPEG_MAGIC):].decode("utf-8", errors="replace")


# ── the fake vision client ───────────────────────────────────────────────────
class _FakeClassifierClient:
    """Programmable stand-in for OpenAIPhotoClassifierClient.

    `verdicts` is keyed by photo url. Anything not registered falls back to a
    benign default, so a scenario only has to say what it cares about.
    """

    def __init__(self) -> None:
        self.verdicts: dict[str, dict] = {}
        self.classify_calls: list[list[str]] = []
        self.attribute_requests: list[bool] = []
        self.fail_classify = False
        self.default_verdict = {"category": "interior", "confidence": 0.9}

    def _registered_verdict(self, ref: str) -> dict:
        # Exact match first — covers `_register_for_stored_copies`, which
        # registers directly against the full data URI a re-derive pass
        # sends. Falls back to the decoded url — covers every OTHER
        # scenario, which registers by the provider url and now receives a
        # data URI built from bytes on the live path.
        found = self.verdicts.get(ref)
        if found is None:
            decoded = _decoded_url(ref)
            if decoded is not None:
                found = self.verdicts.get(decoded)
        return found or {}

    async def classify_photos(self, photo_urls, *, model=None, batch_size=10,
                              with_attributes=True):
        self.classify_calls.append(list(photo_urls))
        self.attribute_requests.append(bool(with_attributes))
        if self.fail_classify:
            raise RuntimeError("vision model unavailable")
        out = []
        for i, ref in enumerate(photo_urls):
            verdict = dict(self.default_verdict)
            verdict.update(self._registered_verdict(ref))
            if not with_attributes:
                # The real client does not ask for them, so the model does not
                # answer with them.
                verdict.pop("attributes", None)
                verdict.pop("people", None)
            verdict["index"] = i
            out.append(verdict)
        return out


class _FakeCategorySource:
    """A source that returns its own real categories — must never be classified."""

    def __init__(self) -> None:
        self.photos_by_place: dict[str, list[dict]] = {}

    async def fetch_venue_photos(self, place_id, categories=None, max_photos=10, hl=None):
        return {"photos": list(self.photos_by_place.get(place_id, []))[:max_photos],
                "info": {}}


# ── build ────────────────────────────────────────────────────────────────────
def _build_with_classifier(context):
    from app.dao.media_archive_store import MediaArchiveStore
    from app.services.photo_classification_service import PhotoClassificationService
    from app.services.venue_photo_archive_service import VenuePhotoArchiveService

    context.classifier_client = _FakeClassifierClient()
    context.category_source = _FakeCategorySource()
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
        google_places_client=context.google,
        venue_dao=context.repository,
        media_store=context.store,
        downloader=context.downloader,
        searchapi_photos_client=context.category_source,
        photo_classifier=context.classifier,
        max_photos_per_venue=10,
        today_provider=lambda: getattr(context, "today", "2026-07-26"),
    )


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


def _run_job(context, **over):
    context.summary = _run(context.service.run(_cfg(context, **over)))
    return context.summary


def _seed_photos(context, vid, specs, *, source_obj=None):
    """Seed one venue with photos, each spec being extra photo fields.

    Returns the photo dicts so a scenario can register verdicts by url.
    """
    pid = _seed_venue(context, vid)
    photos = []
    for i, spec in enumerate(specs):
        photo = {
            "url": f"https://lh3.googleusercontent.com/{vid}_p{i}",
            "photo_name": f"places/{vid}/photos/ref{i}",
        }
        photo.update(spec or {})
        photos.append(photo)
    target = source_obj if source_obj is not None else context.google
    target.photos_by_place[pid] = photos
    context.venue_id = vid
    context.photos = photos
    return photos


def _one_photo(context, vid, verdict=None, attributes=None, people=None,
               **photo_fields):
    """One venue, one photo, and the verdict the model will return for it.

    `attributes` and `people` are written plainly by the scenario and wrapped
    into per-attribute confidences here.
    """
    photos = _seed_photos(context, vid, [photo_fields])
    url = photos[0]["url"]
    full = dict(verdict or {})
    if attributes is not None:
        full["attributes"] = _wrap(attributes)
    if people is not None:
        full["people"] = _wrap(people)
    if full:
        context.classifier_client.verdicts[url] = full
    return photos[0]


def _manifests(context):
    return [k for k in context.fake_s3.objects if k.endswith("_manifest.json")]


def _entries(context, venue_id=None):
    vid = venue_id or context.venue_id
    keys = [k for k in _manifests(context) if f"venue_id={vid}/" in k]
    assert keys, f"no manifest was written for {vid}"
    body = json.loads(context.fake_s3.objects[keys[0]])
    return body.get("photos") or []


def _entry(context, venue_id=None):
    entries = _entries(context, venue_id)
    assert len(entries) == 1, f"expected one photo entry, got {len(entries)}"
    return entries[0]


def _image_keys(context, venue_id=None):
    keys = [k for k in context.fake_s3.objects if k.endswith(".jpg")]
    if venue_id:
        keys = [k for k in keys if f"venue_id={venue_id}/" in k]
    return keys


# ── Background ───────────────────────────────────────────────────────────────
@given("the photo classifier is available")
def step_classifier_available(context):
    _build_with_classifier(context)


# ── 1. The six categories ────────────────────────────────────────────────────
@given('a fetched photo the classifier categorizes as "{category}"')
def step_photo_categorized_as(context, category):
    _one_photo(context, f"ven_cat_{category}",
               verdict={"category": category, "confidence": 0.9})


@given("the source files photos under an authorship placeholder category")
def step_source_placeholder_category(context):
    context.placeholder_category = "by_visitor"


@given("a venue with {n:d} fetched photos")
def step_venue_n_photos(context, n):
    _seed_photos(context, f"ven_batch_{n}", [{} for _ in range(n)])


# ── 2. Exterior is decided by the sky ────────────────────────────────────────
@given("a fetched photo showing open air overhead")
def step_photo_open_air(context):
    _one_photo(context, "ven_sky", verdict={"category": "exterior", "confidence": 0.9},
               attributes={"exterior_kind": "open_air_area"})


@given("a fetched photo showing a roof overhead")
def step_photo_roof(context):
    _one_photo(context, "ven_roofed", verdict={"category": "interior", "confidence": 0.9})


@given("a fetched photo of the venue facade from the street")
def step_photo_facade(context):
    _one_photo(context, "ven_facade", verdict={"category": "exterior", "confidence": 0.9},
               attributes={"exterior_kind": "facade"})


@given("a fetched photo of a partially covered terrace")
def step_photo_terrace(context):
    _one_photo(context, "ven_terrace", verdict={"category": "exterior", "confidence": 0.9},
               attributes={"exterior_kind": "open_air_area", "covered": "partial"})


# ── 3. People are read wherever they appear ──────────────────────────────────
@given("a fetched photo of a room with a few people at its edges")
def step_photo_room_with_people(context):
    _one_photo(context, "ven_room_people",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "dining"},
               people={"crowd_level": "some"})


@given("a fetched photo of an empty room")
def step_photo_empty_room(context):
    _one_photo(context, "ven_empty_room",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "dining"})


@given("a fetched photo of a crowd that includes children")
def step_photo_crowd_kids(context):
    _one_photo(context, "ven_kids", verdict={"category": "crowd", "confidence": 0.9},
               people={"crowd_level": "busy", "has_kids": "yes",
                       "group_type": "families"})


@given("a fetched photo of a busy dance floor")
def step_photo_dance_floor(context):
    _one_photo(context, "ven_dancing", verdict={"category": "crowd", "confidence": 0.9},
               people={"crowd_level": "packed", "activity": "dancing"})


# ── 4. Per-category attributes ───────────────────────────────────────────────
@given("a fetched photo of a blurred menu")
def step_photo_blurred_menu(context):
    _one_photo(context, "ven_blurred_menu",
               verdict={"category": "menu", "confidence": 0.9,
                        "quality": _sure("poor")},
               attributes={"legible": "no"})


@given("a fetched photo of a drinks menu")
def step_photo_drinks_menu(context):
    _one_photo(context, "ven_drinks_menu",
               verdict={"category": "menu", "confidence": 0.9},
               attributes={"legible": "yes", "covers": "drinks"})


@given("a fetched photo of a menu without prices")
def step_photo_menu_no_prices(context):
    _one_photo(context, "ven_menu_noprice",
               verdict={"category": "menu", "confidence": 0.9},
               attributes={"legible": "yes", "has_prices": "no"})


@given("an archived menu photo marked as not legible")
def step_archived_illegible(context):
    context.menu_entries = [{"photo_id": "illegible", "key": "k/illegible.jpg",
                             "category": "menu",
                             "attributes": {"legible": "no"}}]


@given("an archived menu photo marked as legible")
def step_archived_legible(context):
    context.menu_entries.append({"photo_id": "readable", "key": "k/readable.jpg",
                                 "category": "menu",
                                 "attributes": {"legible": "yes"}})


@given("an archived menu photo whose legibility could not be classified")
def step_archived_unknown_legibility(context):
    # "Could not tell" is not "unreadable": dropping it would silently lose a
    # menu the extractor might well have read.
    context.menu_entries = [{"photo_id": "unsure", "key": "k/unsure.jpg",
                             "category": "menu",
                             "attributes": {"legible": "not_classified"}}]


@given("a fetched photo of a sharing platter")
def step_photo_platter(context):
    _one_photo(context, "ven_platter",
               verdict={"category": "food_drinks", "confidence": 0.9},
               attributes={"subject": "food", "portion": "shareable"})


@given("a fetched photo of a room with a DJ booth")
def step_photo_dj(context):
    _one_photo(context, "ven_dj", verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "dance_floor", "music_setup": "dj"})


@given("a fetched photo of a room with a projector screen")
def step_photo_screen(context):
    _one_photo(context, "ven_screen", verdict={"category": "interior", "confidence": 0.9},
               attributes={"has_screens": "yes"})


@given("a fetched photo of an event flyer")
def step_photo_flyer(context):
    _one_photo(context, "ven_flyer",
               verdict={"category": "other", "confidence": 0.9},
               attributes={"other_kind": "event_flyer"})


# ── 5. Only what the model is sure of becomes a fact ─────────────────────────
@given("a fetched photo the classifier reads confidently as a bar")
def step_photo_sure_bar(context):
    _one_photo(context, "ven_sure_bar",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "bar"})


@given("a fetched photo whose space type the classifier is unsure about")
def step_photo_unsure_space(context):
    _one_photo(context, "ven_unsure_space",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": {"value": "bar", "confidence": UNSURE}})


@given("a fetched photo read confidently as a bar but unsure about its screens")
def step_photo_mixed_confidence(context):
    _one_photo(context, "ven_mixed_conf",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "bar",
                           "has_screens": {"value": "yes", "confidence": UNSURE}})


@given("the classifier returns an attribute with no confidence")
def step_attribute_without_confidence(context):
    # Nothing to check it against, so it cannot have cleared the bar.
    _one_photo(context, "ven_no_conf",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": {"value": "bar"}})


@given("a fetched photo the classifier describes only partially")
def step_photo_partial_description(context):
    _one_photo(context, "ven_partial",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "bar"})


@given("a fetched photo of a dessert with no drink in the frame")
def step_photo_dessert(context):
    # `not_applicable` is a fact about the photograph, not a shrug: there is no
    # drink to identify, which is a different thing from a drink too dark to read.
    _one_photo(context, "ven_dessert",
               verdict={"category": "food_drinks", "confidence": 0.9},
               attributes={"subject": "food", "food_type": "dessert",
                           "drink_type": "not_applicable"})


@given("the classifier is unsure whether a drink is even in the frame")
def step_unsure_drink(context):
    _one_photo(context, "ven_unsure_drink",
               verdict={"category": "food_drinks", "confidence": 0.9},
               attributes={"drink_type": {"value": "not_applicable",
                                          "confidence": UNSURE}})


@given("a fetched photo whose author the classifier cannot confidently read")
def step_unsure_authorship(context):
    _one_photo(context, "ven_unsure_author",
               verdict={"category": "interior", "confidence": 0.9,
                        "likely_authorship": {"value": "by_visitor",
                                              "confidence": UNSURE}},
               authorship="unknown")


@given("the classifier returns a lighting value that is not in the vocabulary")
def step_invented_label(context):
    _one_photo(context, "ven_invented",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "bar", "lighting": "candlelit"})


# ── 6. Who took the photo ────────────────────────────────────────────────────
@given("a fetched photo the provider attributes to the venue owner")
def step_photo_by_owner(context):
    _one_photo(context, "ven_owner", verdict={"category": "menu", "confidence": 0.9},
               authorship="by_owner", author_name="Venue ven_owner")


@given("a fetched photo whose provider authorship is unknown")
def step_photo_unknown_author(context):
    _one_photo(context, "ven_unknown_author",
               verdict={"category": "interior", "confidence": 0.9,
                        "likely_authorship": _sure("by_owner")},
               authorship="unknown")


@given("a fetched photo the provider attributes to a visitor")
def step_photo_by_visitor(context):
    _one_photo(context, "ven_visitor",
               verdict={"category": "interior", "confidence": 0.9,
                        "likely_authorship": _sure("by_owner")},
               authorship="by_visitor")


# ── 7. Degrading ─────────────────────────────────────────────────────────────
@given("the photo classifier fails for every request")
def step_classifier_fails(context):
    context.classifier_client.fail_classify = True


@given("a fetched photo the classifier categorizes but cannot describe")
def step_photo_no_attributes(context):
    _one_photo(context, "ven_attr_fail",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={})


@given("the classifier returns a verdict below the confidence threshold")
def step_low_confidence(context):
    _one_photo(context, "ven_lowconf",
               verdict={"category": "crowd", "confidence": 0.2})


@given("two eligible venues where the classifier fails for the first")
def step_two_venues_first_fails(context):
    photos = _seed_photos(context, "ven_cls_fail", [{}])
    context.failing_venue = "ven_cls_fail"
    context.classifier_client.verdicts[photos[0]["url"]] = {"category": "__boom__"}
    _seed_photos(context, "ven_cls_ok", [{}])
    context.good_venue = "ven_cls_ok"
    context.venue_id = None


# ── 8. Controlling what it costs ─────────────────────────────────────────────
@given("the source provides its own photo categories")
def step_source_has_categories(context):
    context.config_over["source"] = "searchapi_gmaps_photos"
    _seed_photos(context, "ven_searchapi", [{"category": "menu"}],
                 source_obj=context.category_source)


@given("classification is disabled for the run")
def step_classification_disabled(context):
    context.config_over["classify_photos"] = False
    _one_photo(context, "ven_no_classify")


@given("attribute derivation is disabled for the run")
def step_attributes_disabled(context):
    context.config_over["derive_photo_attributes"] = False
    _one_photo(context, "ven_no_attrs",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "bar"})


# ── 9. Re-deriving from the archive ──────────────────────────────────────────
@given("a completed run whose photos are archived")
def step_completed_run(context):
    _one_photo(context, "ven_rederive", verdict={"category": "interior", "confidence": 0.9},
               attributes={"space_type": "bar"})
    _run_job(context, venue_ids=context.venue_id)
    context.archived_prefix = context.summary["prefix"]
    context.classifier_client.classify_calls.clear()
    context.google.calls.clear()


def _register_for_stored_copies(context, verdict):
    """Program the model's answer for the STORED copies, not the provider link.

    A re-derive pass never sees the provider url again — that is the point of it
    — so the second answer is registered against the `data:` URI the pipeline
    actually sends. It inlines the stored bytes rather than presigning, because
    a presigned url built from temporary credentials is ~1,900 chars and the
    vision API rejects it outright.
    """
    import base64
    for key in _image_keys(context, context.venue_id):
        payload = base64.b64encode(context.fake_s3.objects[key]).decode("ascii")
        context.classifier_client.verdicts[f"data:image/jpeg;base64,{payload}"] = verdict


@given("the attribute schema gains a field the model can read from them")
def step_schema_gains_field(context):
    # The whole category is re-answered, not just the new field: the model is
    # asked the current schema, so a re-run replaces rather than merges.
    _register_for_stored_copies(context, {
        "category": "interior", "confidence": 0.9,
        "attributes": _wrap({"space_type": "bar", "has_screens": "yes"}),
    })


@given("the classifier would now categorize those photos differently")
def step_classifier_recategorizes(context):
    # The category is in the S3 key of an object that already exists, so a
    # manifest that took this answer would disagree with its own key.
    _register_for_stored_copies(context, {
        "category": "crowd", "confidence": 0.99,
        "attributes": _wrap({"crowd_level": "packed"}),
    })


# ── When ─────────────────────────────────────────────────────────────────────
@when("menu extraction selects photos to read")
def step_menu_selection(context):
    from app.services.menu_extraction_service import selectable_menu_photos

    context.selected = selectable_menu_photos(context.menu_entries)


@when("attribute derivation is re-run for that run")
def step_rederive(context):
    context.rederived = _run(context.service.rederive_attributes(SOURCE))


# ── Then: categories and filing ──────────────────────────────────────────────
@then('the photo is filed under the "{category}" category')
def step_filed_under(context, category):
    keys = _image_keys(context, context.venue_id)
    assert keys, f"nothing was stored for {context.venue_id}"
    assert all(f"/media/{category}/" in k for k in keys), keys


@then('the manifest entry records the category "{category}"')
def step_entry_category(context, category):
    assert _entry(context)["category"] == category, _entry(context)


@then("no photo is filed under an authorship placeholder category")
def step_no_placeholder_folder(context):
    bad = [k for k in _image_keys(context)
           if f"/media/{context.placeholder_category}/" in k]
    assert not bad, f"photos were filed under the placeholder category: {bad}"


@then("the classifier receives the photos in batches")
def step_batched(context):
    calls = context.classifier_client.classify_calls
    assert calls, "the classifier was never called"
    assert max(len(c) for c in calls) > 1, f"no batch held more than one photo: {calls}"


@then("the classifier is not called once per photo")
def step_not_per_photo(context):
    calls = context.classifier_client.classify_calls
    photos = sum(len(c) for c in calls)
    assert len(calls) < photos, f"{len(calls)} calls for {photos} photos"


@then("the classifier is called exactly once")
def step_called_once(context):
    calls = context.classifier_client.classify_calls
    assert len(calls) == 1, f"expected one call, got {len(calls)}: {calls}"


@then("the photo is filed under its classified category")
def step_filed_under_classified(context):
    keys = _image_keys(context, context.venue_id)
    assert keys, "nothing was stored"
    assert all("/media/interior/" in k for k in keys), keys


# ── Then: attributes ─────────────────────────────────────────────────────────
@then('the manifest entry records the attribute "{field}" as "{value}"')
def step_entry_attribute(context, field, value):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get(field) == value, attrs


@then("the manifest entry carries attributes")
def step_entry_has_attributes(context):
    assert _entry(context).get("attributes"), _entry(context)


@then("the manifest entry carries no attributes")
def step_entry_no_attributes(context):
    assert not _entry(context).get("attributes"), _entry(context)


@then('the manifest entry carries only the "{category}" attributes')
def step_entry_only_own_attributes(context, category):
    from app.models.photo_taxonomy import attributes_for

    expected = {spec.name for spec in attributes_for(category)}
    actual = set((_entry(context).get("attributes") or {}))
    assert actual == expected, f"expected {sorted(expected)}, got {sorted(actual)}"


@then('every attribute of the "{category}" schema is present in the manifest entry')
def step_entry_schema_complete(context, category):
    from app.models.photo_taxonomy import attributes_for

    attrs = _entry(context).get("attributes") or {}
    for spec in attributes_for(category):
        assert spec.name in attrs, f"{spec.name} is missing from {attrs}"


@then("the rest of the classifier verdict is stored")
def step_rest_of_verdict(context):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("space_type") == "bar", attrs


@then("the classifier is not asked for attributes")
def step_no_attributes_requested(context):
    asked = context.classifier_client.attribute_requests
    assert asked, "the classifier was never called"
    assert not any(asked), f"attributes were requested anyway: {asked}"


# ── Then: people ─────────────────────────────────────────────────────────────
@then("the manifest entry carries a people block")
def step_has_people(context):
    assert _entry(context).get("people"), _entry(context)


@then("the manifest entry carries no people block")
def step_no_people(context):
    assert not _entry(context).get("people"), _entry(context)


@then('the people block records "{field}" as "{value}"')
def step_people_field(context, field, value):
    people = _entry(context).get("people") or {}
    assert people.get(field) == value, people


# ── Then: who took the photo ─────────────────────────────────────────────────
@then('the manifest entry still records the authorship "{value}"')
def step_authorship_kept(context, value):
    assert _entry(context).get("authorship") == value, _entry(context)


@then("the manifest entry records a likely authorship")
def step_likely_authorship(context):
    assert _entry(context).get("likely_authorship"), _entry(context)


@then("the manifest entry records no likely authorship")
def step_no_likely_authorship(context):
    assert not _entry(context).get("likely_authorship"), _entry(context)


# ── Then: menu selection ─────────────────────────────────────────────────────
@then("only the legible menu photo is selected")
def step_only_legible(context):
    ids = [e["photo_id"] for e in context.selected]
    assert ids == ["readable"], ids


@then("that menu photo is selected")
def step_that_photo_selected(context):
    ids = [e["photo_id"] for e in context.selected]
    assert ids == ["unsure"], ids


# ── Then: degrading ──────────────────────────────────────────────────────────
@then("all {n:d} photos are still archived")
def step_all_archived(context, n):
    keys = _image_keys(context, context.venue_id)
    assert len(keys) == n, f"expected {n} photos, got {len(keys)}: {keys}"


@then("the photos keep the category the source gave them")
def step_keep_source_category(context):
    for entry in _entries(context):
        assert entry.get("category") == entry.get("source_category"), entry


@then("both venues are archived")
def step_both_archived(context):
    for vid in (context.failing_venue, context.good_venue):
        assert _image_keys(context, vid), f"{vid} was not archived"


@then("the second venue's photos carry a classified category")
def step_second_venue_classified(context):
    entry = _entry(context, context.good_venue)
    assert entry["category"] == "interior", entry


# ── Then: cost control ───────────────────────────────────────────────────────
@then("the classifier is not called")
def step_classifier_not_called(context):
    calls = context.classifier_client.classify_calls
    assert not calls, f"the classifier was called: {calls}"


@then("the run summary reports the number of photos classified")
def step_summary_classified(context):
    assert context.summary.get("photos_classified"), context.summary


@then("the run summary reports an estimated classification cost")
def step_summary_cost(context):
    assert context.summary.get("classification_cost_usd") is not None, context.summary
    assert context.summary["classification_cost_usd"] >= 0, context.summary


# ── Then: re-deriving ────────────────────────────────────────────────────────
@then("the archived photos are read from the bucket")
def step_read_from_bucket(context):
    calls = context.classifier_client.classify_calls
    assert calls, "no pass ran over the archived run"
    urls = [u for batch in calls for u in batch]
    # Inlined bytes, not a url the model has to go and fetch — and therefore
    # carrying no credentials off to a third party.
    assert all(u.startswith("data:image/") for u in urls), [u[:40] for u in urls]
    for marker in ("AWSAccessKeyId", "x-amz-security-token", "Signature"):
        assert not any(marker in u for u in urls), f"{marker} leaked into the payload"


@then("no provider request is made")
def step_no_provider_request(context):
    assert not context.google.calls, f"the provider was called: {context.google.calls}"
