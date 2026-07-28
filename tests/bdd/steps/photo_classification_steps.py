"""Behave steps for tests/bdd/enrichment/photo-classification.feature.

Drives the REAL VenuePhotoArchiveService and the REAL PhotoClassificationService
over a fake vision client, so the validation, the fallbacks and the two-pass
ordering under test are all real code — only the model call is faked.

The fake is programmed PER PHOTO URL rather than per call, because the pipeline
batches and the scenarios must not depend on how the batches happen to split.

Two properties are asserted by counting, because they are the ones that cost
money if they regress:
  * a source that already knows its categories must reach the classifier zero
    times, and
  * a photo filed as `other` must reach the attribute pass zero times.
"""
from __future__ import annotations

import json

from behave import given, then, when  # type: ignore[import-untyped]

from tests.bdd.steps.venue_photo_archive_steps import (  # reuse: one set of fakes
    _run,
    _seed_venue,
)

SOURCE = "google_photos"


# ── the fake vision client ───────────────────────────────────────────────────
class _FakeClassifierClient:
    """Programmable stand-in for OpenAIPhotoClassifierClient.

    `verdicts` and `attributes` are keyed by photo url. Anything not registered
    falls back to a benign default, so a scenario only has to say what it cares
    about.
    """

    def __init__(self) -> None:
        self.verdicts: dict[str, dict] = {}
        self.attributes: dict[str, dict] = {}
        self.classify_calls: list[list[str]] = []
        self.attribute_calls: list[tuple[str, list[str]]] = []
        self.fail_classify = False
        self.fail_attributes = False
        self.default_verdict = {"category": "interior", "confidence": 0.9}

    async def classify_photos(self, photo_urls, *, model=None, batch_size=10):
        self.classify_calls.append(list(photo_urls))
        if self.fail_classify:
            raise RuntimeError("vision model unavailable")
        out = []
        for i, url in enumerate(photo_urls):
            verdict = dict(self.default_verdict)
            verdict.update(self.verdicts.get(url) or {})
            verdict["index"] = i
            out.append(verdict)
        return out

    async def derive_attributes(self, category, photo_urls, *, model=None, batch_size=10):
        self.attribute_calls.append((category, list(photo_urls)))
        if self.fail_attributes:
            raise RuntimeError("vision model unavailable")
        out = []
        for i, url in enumerate(photo_urls):
            entry = dict(self.attributes.get(url) or {})
            entry["index"] = i
            out.append(entry)
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


def _one_photo(context, vid, verdict=None, attributes=None, **photo_fields):
    photos = _seed_photos(context, vid, [photo_fields])
    url = photos[0]["url"]
    if verdict:
        context.classifier_client.verdicts[url] = verdict
    if attributes:
        context.classifier_client.attributes[url] = attributes
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
    _one_photo(context, f"ven_cat_{category}", verdict={"category": category, "confidence": 0.9})


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
               attributes={"attributes": {"exterior_kind": "area_externa"}})


@given("a fetched photo showing a roof overhead")
def step_photo_roof(context):
    _one_photo(context, "ven_roofed", verdict={"category": "interior", "confidence": 0.9})


@given("a fetched photo of the venue facade from the street")
def step_photo_facade(context):
    _one_photo(context, "ven_facade", verdict={"category": "exterior", "confidence": 0.9},
               attributes={"attributes": {"exterior_kind": "fachada",
                                          "venue_name_legible": True}})


@given("a fetched photo of a partially covered terrace")
def step_photo_terrace(context):
    _one_photo(context, "ven_terrace", verdict={"category": "exterior", "confidence": 0.9},
               attributes={"attributes": {"exterior_kind": "area_externa",
                                          "covered": "parcial"}})


# ── 3. People are read wherever they appear ──────────────────────────────────
@given("a fetched photo of a room with a few people at its edges")
def step_photo_room_with_people(context):
    _one_photo(context, "ven_room_people",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"attributes": {"space_type": "salao"},
                           "people": {"crowd_level": "poucas_pessoas",
                                      "clima_social": "Tranquilo"}})


@given("a fetched photo of an empty room")
def step_photo_empty_room(context):
    _one_photo(context, "ven_empty_room",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"attributes": {"space_type": "salao"}, "people": None})


@given("a fetched photo of a crowd that includes children")
def step_photo_crowd_kids(context):
    _one_photo(context, "ven_kids", verdict={"category": "crowd", "confidence": 0.9},
               attributes={"people": {"crowd_level": "movimentado", "has_kids": True,
                                      "publico": ["Família"]}})


@given("a fetched photo of a crowd at a rock night")
def step_photo_crowd_rock(context):
    _one_photo(context, "ven_rock", verdict={"category": "crowd", "confidence": 0.9},
               attributes={"people": {"crowd_level": "cheio",
                                      "dress_code": ["Alternativo"],
                                      "dress_scene": ["rock_metal"]}})


# ── 4. Per-category attributes ───────────────────────────────────────────────
@given("a fetched photo of a blurred menu")
def step_photo_blurred_menu(context):
    _one_photo(context, "ven_blurred_menu",
               verdict={"category": "menu", "confidence": 0.9, "quality": "borrada"},
               attributes={"attributes": {"legible": "nao"}})


@given("a fetched photo of the back page of a drinks menu")
def step_photo_back_drinks_menu(context):
    _one_photo(context, "ven_drinks_menu",
               verdict={"category": "menu", "confidence": 0.9},
               attributes={"attributes": {"legible": "sim", "page_side": "verso",
                                          "content_scope": "so_bebida"}})


@given("a fetched photo of a menu without prices")
def step_photo_menu_no_prices(context):
    _one_photo(context, "ven_menu_noprice",
               verdict={"category": "menu", "confidence": 0.9},
               attributes={"attributes": {"legible": "sim", "has_prices": False}})


@given("an archived menu photo marked as not legible")
def step_archived_illegible(context):
    context.menu_entries = [{"photo_id": "illegible", "key": "k/illegible.jpg",
                             "category": "menu",
                             "attributes": {"legible": "nao"}}]


@given("an archived menu photo marked as legible")
def step_archived_legible(context):
    context.menu_entries.append({"photo_id": "readable", "key": "k/readable.jpg",
                                 "category": "menu",
                                 "attributes": {"legible": "sim"}})


@given("a fetched photo of a sharing platter")
def step_photo_platter(context):
    _one_photo(context, "ven_platter",
               verdict={"category": "food_drinks", "confidence": 0.9},
               attributes={"attributes": {"subject": "comida",
                                          "portion_size": "para_dividir"}})


@given("a fetched photo of a room with a DJ booth")
def step_photo_dj(context):
    _one_photo(context, "ven_dj", verdict={"category": "interior", "confidence": 0.9},
               attributes={"attributes": {"space_type": "pista_danca",
                                          "music_format": ["DJ"]}})


@given("a fetched photo of a room with a projector screen")
def step_photo_screen(context):
    _one_photo(context, "ven_screen", verdict={"category": "interior", "confidence": 0.9},
               attributes={"attributes": {"screens": "telao"}})


@given("a fetched photo of an event flyer")
def step_photo_flyer(context):
    # `other_kind` rides on the CATEGORY verdict, not the attribute pass: pass 2
    # skips `other` entirely, so registering it as an attribute here would test a
    # channel the pipeline never reads.
    _one_photo(context, "ven_flyer",
               verdict={"category": "other", "confidence": 0.9,
                        "other_kind": "flyer_evento"})


# ── 5. Taxonomy-aligned attributes ───────────────────────────────────────────
@given("a fetched photo of a dimly lit intimate room")
def step_photo_intimate(context):
    _one_photo(context, "ven_intimate",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"attributes": {"lighting": "quente_baixa",
                                          "estetica": ["Intimista"]}})


@given("the classifier returns an aesthetic label that is not in the taxonomy")
def step_invented_label(context):
    _one_photo(context, "ven_invented",
               verdict={"category": "interior", "confidence": 0.9},
               attributes={"attributes": {"space_type": "salao",
                                          "estetica": ["Cyberpunk", "Rústico"]}})
    context.invented_label = "Cyberpunk"


# ── 6. Who took the photo ────────────────────────────────────────────────────
@given("a fetched photo the provider attributes to the venue owner")
def step_photo_by_owner(context):
    _one_photo(context, "ven_owner", verdict={"category": "menu", "confidence": 0.9},
               authorship="by_owner", author_name="Venue ven_owner")


@given("a fetched photo whose provider authorship is unknown")
def step_photo_unknown_author(context):
    _one_photo(context, "ven_unknown_author",
               verdict={"category": "interior", "confidence": 0.9,
                        "likely_authorship": "by_owner"},
               authorship="unknown")


@given("a fetched photo the provider attributes to a visitor")
def step_photo_by_visitor(context):
    _one_photo(context, "ven_visitor",
               verdict={"category": "interior", "confidence": 0.9,
                        "likely_authorship": "by_owner"},
               authorship="by_visitor")


# ── 7. Degrading ─────────────────────────────────────────────────────────────
@given("the photo classifier fails for every request")
def step_classifier_fails(context):
    context.classifier_client.fail_classify = True
    context.classifier_client.fail_attributes = True


@given("the classifier categorizes photos but fails to derive attributes")
def step_attributes_fail(context):
    context.classifier_client.fail_attributes = True
    _one_photo(context, "ven_attr_fail",
               verdict={"category": "interior", "confidence": 0.9})


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
    _one_photo(context, "ven_no_attrs", verdict={"category": "interior", "confidence": 0.9})


# ── 9. Re-deriving from the archive ──────────────────────────────────────────
@given("a completed run whose photos are archived")
def step_completed_run(context):
    _one_photo(context, "ven_rederive", verdict={"category": "interior", "confidence": 0.9},
               attributes={"attributes": {"space_type": "salao"}})
    _run_job(context, venue_ids=context.venue_id)
    context.archived_prefix = context.summary["prefix"]
    context.classifier_client.attribute_calls.clear()
    context.google.calls.clear()


@given("the attribute schema gains a field the model can read from them")
def step_schema_gains_field(context):
    """Program the model's answer for the STORED copies, not the provider link.

    A re-derive pass never sees the provider url again — that is the point of it
    — so the second answer is registered against the presigned S3 url the
    pipeline will actually send.
    """
    for key in _image_keys(context, context.venue_id):
        signed = context.fake_s3.generate_presigned_url(
            "get_object", Params={"Key": key}
        )
        # The whole category is re-answered, not just the new field: the model
        # is asked the current schema, so a re-run replaces rather than merges.
        context.classifier_client.attributes[signed] = {
            "attributes": {"space_type": "salao", "screens": "telao"}
        }


# ── When ─────────────────────────────────────────────────────────────────────
@when("menu extraction selects photos to read")
def step_menu_selection(context):
    from app.services.menu_extraction_service import selectable_menu_photos

    context.selected = selectable_menu_photos(context.menu_entries)


@when("attribute derivation is re-run for that run")
def step_rederive(context):
    context.rederived = _run(
        context.service.rederive_attributes(SOURCE)
    )


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
    bad = [k for k in _image_keys(context) if f"/media/{context.placeholder_category}/" in k]
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


@then("the photo is filed under its classified category")
def step_filed_under_classified(context):
    keys = _image_keys(context, context.venue_id)
    assert keys, "nothing was stored"
    assert all("/media/interior/" in k for k in keys), keys


# ── Then: attributes ─────────────────────────────────────────────────────────
@then('the manifest entry records the exterior kind "{kind}"')
def step_exterior_kind(context, kind):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("exterior_kind") == kind, attrs


@then("the manifest entry records that the area is partially covered")
def step_partially_covered(context):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("covered") == "parcial", attrs


@then("the manifest entry carries a people block")
def step_has_people(context):
    assert _entry(context).get("people"), _entry(context)


@then("the people block records a crowd level")
def step_people_crowd_level(context):
    assert (_entry(context).get("people") or {}).get("crowd_level"), _entry(context)


@then("the manifest entry carries no people block")
def step_no_people(context):
    assert not _entry(context).get("people"), _entry(context)


@then("the people block records that children are present")
def step_people_kids(context):
    assert (_entry(context).get("people") or {}).get("has_kids") is True, _entry(context)


@then("the people block records a dress code from the venue taxonomy")
def step_people_dress_code(context):
    from app.models.taxonomy import TAXONOMY

    people = _entry(context).get("people") or {}
    codes = people.get("dress_code") or []
    assert codes, people
    for code in codes:
        assert code in TAXONOMY["dress_code"], f"{code!r} is not a dress_code label"


@then('the people block records the dress scene "{scene}"')
def step_people_dress_scene(context, scene):
    people = _entry(context).get("people") or {}
    assert scene in (people.get("dress_scene") or []), people


@then("the manifest entry records that the menu is not legible")
def step_menu_illegible(context):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("legible") == "nao", attrs


@then('the manifest entry records the menu page side "{side}"')
def step_menu_page_side(context, side):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("page_side") == side, attrs


@then('the manifest entry records the menu content scope "{scope}"')
def step_menu_scope(context, scope):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("content_scope") == scope, attrs


@then("the manifest entry records that the menu has no prices")
def step_menu_no_prices(context):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("has_prices") is False, attrs


@then("only the legible menu photo is selected")
def step_only_legible(context):
    ids = [e["photo_id"] for e in context.selected]
    assert ids == ["readable"], ids


@then('the manifest entry records the portion size "{size}"')
def step_portion_size(context, size):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("portion_size") == size, attrs


@then("the manifest entry records a music format from the venue taxonomy")
def step_music_format(context):
    from app.models.taxonomy import TAXONOMY

    attrs = _entry(context).get("attributes") or {}
    formats = attrs.get("music_format") or []
    assert formats, attrs
    for value in formats:
        assert value in TAXONOMY["music_format"], f"{value!r} is not a music_format label"


@then('the manifest entry records the screens value "{value}"')
def step_screens(context, value):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("screens") == value, attrs


@then("the manifest entry carries no interior attributes")
def step_no_interior_attrs(context):
    attrs = _entry(context).get("attributes") or {}
    for field in ("space_type", "lighting", "decor_style", "screens", "capacity"):
        assert field not in attrs, f"{field} belongs to interior, not menu: {attrs}"


@then('the manifest entry records the other kind "{kind}"')
def step_other_kind(context, kind):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("other_kind") == kind, attrs


@then("the manifest entry records an aesthetic from the venue taxonomy")
def step_aesthetic(context):
    from app.models.taxonomy import TAXONOMY

    attrs = _entry(context).get("attributes") or {}
    values = attrs.get("estetica") or []
    assert values, attrs
    for value in values:
        assert value in TAXONOMY["estetica"], f"{value!r} is not an estetica label"


@then("that aesthetic label is not stored")
def step_invented_dropped(context):
    attrs = _entry(context).get("attributes") or {}
    assert context.invented_label not in (attrs.get("estetica") or []), attrs


@then("the rest of the classifier verdict is stored")
def step_rest_kept(context):
    attrs = _entry(context).get("attributes") or {}
    assert "Rústico" in (attrs.get("estetica") or []), attrs
    assert attrs.get("space_type") == "salao", attrs


# ── Then: authorship ─────────────────────────────────────────────────────────
@then('the manifest entry still records the authorship "{value}"')
def step_authorship_kept(context, value):
    assert _entry(context).get("authorship") == value, _entry(context)


@then("the manifest entry records a likely authorship")
def step_likely_authorship(context):
    assert _entry(context).get("likely_authorship"), _entry(context)


@then("the manifest entry records no likely authorship")
def step_no_likely_authorship(context):
    assert "likely_authorship" not in _entry(context), _entry(context)


# ── Then: degrading ──────────────────────────────────────────────────────────
@then("all {n:d} photos are still archived")
def step_all_archived(context, n):
    keys = _image_keys(context, context.venue_id)
    assert len(keys) == n, f"expected {n} images, got {len(keys)}: {keys}"


@then("the photos keep the category the source gave them")
def step_keep_source_category(context):
    for entry in _entries(context):
        assert entry.get("category") == entry.get("source_category"), entry


@then("the manifest entry carries no attributes")
def step_no_attributes(context):
    assert not _entry(context).get("attributes"), _entry(context)


@then("both venues are archived")
def step_both_archived(context):
    assert _image_keys(context, context.failing_venue), "the failing venue lost its photos"
    assert _image_keys(context, context.good_venue), "the healthy venue was not archived"


@then("the second venue's photos carry a classified category")
def step_second_classified(context):
    entries = _entries(context, context.good_venue)
    assert entries and entries[0].get("category") == "interior", entries


# ── Then: cost control ───────────────────────────────────────────────────────
@then("the classifier is not called")
def step_classifier_not_called(context):
    assert not context.classifier_client.classify_calls, (
        f"the classifier was called: {context.classifier_client.classify_calls}"
    )


@then("no attribute request is made for that photo")
def step_no_attribute_request(context):
    calls = context.classifier_client.attribute_calls
    assert not calls, f"attributes were derived for an `other` photo: {calls}"


@then("the run summary reports the number of photos classified")
def step_summary_classified(context):
    assert context.summary.get("photos_classified") == 6, context.summary


@then("the run summary reports an estimated classification cost")
def step_summary_cost(context):
    assert context.summary.get("classification_cost_usd") is not None, context.summary
    assert context.summary["classification_cost_usd"] >= 0, context.summary


# ── Then: re-deriving ────────────────────────────────────────────────────────
@then("the archived photos are read from the bucket")
def step_read_from_bucket(context):
    calls = context.classifier_client.attribute_calls
    assert calls, "no attribute pass ran over the archived run"
    urls = [u for _, batch in calls for u in batch]
    assert all("presigned" in u for u in urls), urls


@then("no provider request is made")
def step_no_provider_request(context):
    assert not context.google.calls, f"the provider was called: {context.google.calls}"


@then("the manifest entry records the new attribute")
def step_rederived_attribute(context):
    attrs = _entry(context).get("attributes") or {}
    assert attrs.get("screens") == "telao", attrs
    assert context.rederived["photos_attributed"] == 1, context.rederived
