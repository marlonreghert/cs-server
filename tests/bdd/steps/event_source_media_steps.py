"""Behave steps for tests/bdd/api/event-source-media.feature.

Self-contained per scenario, mirroring event_cover_presign_steps.py's
pattern: a fresh FastAPI app with only admin_events_router registered, and a
fresh in-memory fake DAO + fake MediaArchiveStore per scenario (no live
Postgres/S3). Fakes RAISE on a call the scenario never set up for, rather
than returning a silent default — a fake that answers "successfully" to a
call nobody programmed is how a route regression (e.g. calling
list_run_prefixes(), which plans/260813_event-source-media.md explicitly
forbids here) slips past green.

Fixture data intentionally mirrors the plan's own production ground truth
(`NOITE DA PATROA` / Club Metrópole, three shortcodes, five images, one
never-read flyer) — see the plan's §Evidence — without hard-coding it into
any production code; only this test data uses those literals.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from behave import given, when, then  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container

RUN_PREFIX = "retrieved/source=instagram_posts/year=2026/month=08/day=07/run_id=01J000000000000EVTMEDIA/"
VENUE_ID = "venue-club-metropole"


# ── fakes at the true boundary (no live Postgres/S3) ────────────────────────
class _FakeEventStore:
    """Stands in for container.pipeline_repository's get_event/
    list_event_sources.

    `forbid_lookups` lets the "no admin credential" scenario prove the admin
    gate runs BEFORE any business logic: if the route looked the event (or
    its sources) up anyway, this raises instead of quietly returning
    nothing, which would let a missing/misordered auth dependency masquerade
    as an empty/not-found response.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.sources: dict[str, list[dict]] = {}
        self.get_event_calls: list[str] = []
        self.list_sources_calls: list[str] = []
        self.forbid_lookups = False

    def get_event(self, event_id: str):
        if self.forbid_lookups:
            raise AssertionError(
                "BDD harness: the event store must not be queried for an "
                "unauthenticated request"
            )
        self.get_event_calls.append(event_id)
        return self.events.get(event_id)

    def list_event_sources(self, event_id: str):
        if self.forbid_lookups:
            raise AssertionError(
                "BDD harness: the event store must not be queried for an "
                "unauthenticated request"
            )
        self.list_sources_calls.append(event_id)
        return list(self.sources.get(event_id, []))


class _FakeMediaStore:
    """Stands in for container.media_archive_store.

    Every read is programmed explicitly and keyed exactly like the real
    `MediaArchiveStore` (`(prefix, venue_id)`); an unprogrammed manifest read
    or an unprogrammed presign target raises rather than returning a silent
    default. `list_run_prefixes`/`read_promoter_manifest` are NEVER
    programmed by this feature's scenarios (every post here is a venue
    post, and the whole point of the plan is to avoid the batch-scan) — both
    raise unconditionally, so a regression that reintroduces either call
    fails LOUDLY here.
    """

    def __init__(self) -> None:
        self.manifest_calls: list[tuple[str, str]] = []
        self.presign_calls: list[dict] = []
        self._manifests: dict[tuple[str, str], Optional[dict]] = {}
        self._presign_failures: set[str] = set()

    def program_manifest(self, prefix: str, venue_id: str, manifest: dict) -> None:
        self._manifests[(prefix, venue_id)] = manifest

    def program_manifest_unreadable(self, prefix: str, venue_id: str) -> None:
        self._manifests[(prefix, venue_id)] = None

    def fail_presign_for(self, key: str) -> None:
        self._presign_failures.add(key)

    async def read_manifest(self, prefix: str, venue_id: str):
        self.manifest_calls.append((prefix, venue_id))
        cache_key = (prefix, venue_id)
        if cache_key not in self._manifests:
            raise AssertionError(f"BDD harness: read_manifest{cache_key!r} not programmed")
        return self._manifests[cache_key]

    async def read_promoter_manifest(self, prefix: str, handle: str):
        raise AssertionError(
            "BDD harness: read_promoter_manifest must not be called — every "
            "source in this feature is a venue post, never a promoter post"
        )

    async def list_run_prefixes(self, source: str):
        raise AssertionError(
            "BDD harness: list_run_prefixes must never be called by an "
            "interactive media request — plans/260813_event-source-media.md "
            "explicitly forbids driving the crawl-time batch scan from here"
        )

    async def presign(self, key: str, expires_in: int = 900) -> Optional[str]:
        self.presign_calls.append({"key": key, "expires_in": expires_in})
        if key in self._presign_failures:
            return None
        return f"https://datalake.example.com/signed?key={quote(key, safe='')}&sig=SECRETTOKEN&expires_in={expires_in}"


def _key_from_signed_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["key"][0]


def _override_setting(context, name, value) -> None:
    """Set a global setting for the scenario, remembering the original so
    environment.after_scenario restores it (no cross-scenario leakage) —
    mirrors event_cover_presign_steps.py's helper."""
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)


def _headers(context) -> dict:
    override = getattr(context, "request_headers", None)
    return override if override is not None else context.admin_headers


def _snapshot_media_counts() -> dict:
    from app.metrics import EVENT_MEDIA_TOTAL

    return {
        r: EVENT_MEDIA_TOTAL.labels(result=r)._value.get()
        for r in ("manifest_read", "manifest_fallback", "no_media")
    }


def _snapshot_sign_counts() -> dict:
    from app.metrics import EVENT_MEDIA_IMAGE_SIGN_TOTAL

    return {
        r: EVENT_MEDIA_IMAGE_SIGN_TOTAL.labels(result=r)._value.get()
        for r in ("signed", "failed")
    }


def _request_media(context, event_id: str, *, extra_headers: Optional[dict] = None,
                    params: Optional[dict] = None) -> None:
    context._media_metric_before = _snapshot_media_counts()
    context._sign_metric_before = _snapshot_sign_counts()
    headers = dict(_headers(context))
    if extra_headers:
        headers.update(extra_headers)
    context.response = context.esm_client.get(
        f"/admin/events/{event_id}/media", headers=headers, params=params,
    )


def _body(context) -> list:
    assert context.response.status_code == 200, context.response.text
    return context.response.json()


def _all_images(body: list) -> list:
    out = []
    for source in body:
        out.extend(source.get("images") or [])
    return out


def _source_row(*, shortcode: str, handle: str, uploaded_at: datetime, cover_key: str) -> dict:
    return {
        "source_kind": "venue_post",
        "source_handle": handle,
        "source_shortcode": shortcode,
        "source_permalink": f"https://instagram.com/p/{shortcode}/",
        "cover_photo_key": cover_key,
        "first_seen_at": uploaded_at,
        "last_seen_at": uploaded_at,
        "source_media_type": "Sidecar",
        "source_uploaded_at": uploaded_at,
    }


def _manifest_entry(*, shortcode: str, suffix: Optional[str], category: str, confidence: float) -> dict:
    filename = f"{shortcode}_{suffix}.jpg" if suffix else f"{shortcode}.jpg"
    folder = "media/flyer" if category == "flyer" else "media"
    key = f"{RUN_PREFIX}venue_id={VENUE_ID}/{folder}/{filename}"
    return {
        "shortcode": shortcode,
        "key": key,
        "category": category,
        "classification_confidence": confidence,
        "attributes": {"names_time": "no"},
        "permalink": f"https://instagram.com/p/{shortcode}/",
        "uploaded_at": None,
        "post_type": "Sidecar",
    }


# ── background ───────────────────────────────────────────────────────────────
@given("an event announced by three posts on the same venue account")
def step_three_posts_background(context):
    app = FastAPI()
    app.include_router(admin_events_router)

    context.event_store = _FakeEventStore()
    context.media_store = _FakeMediaStore()
    container = SimpleNamespace(
        pipeline_repository=context.event_store,
        media_archive_store=context.media_store,
    )
    set_events_container(container)
    context.esm_client = TestClient(app)

    _override_setting(context, "admin_api_key", "bdd-admin-secret")
    _override_setting(context, "event_cover_presign_expires_seconds", 900)
    context.admin_headers = {"X-Admin-Api-Key": "bdd-admin-secret"}
    context.request_headers = None
    context.injected_key = None

    context.event_id = "evt-noite-da-patroa"

    context.dbs1 = "Dbs1FdsEWr7"
    context.dbvh = "DbvhZJqkUyf"
    context.dbts = "DbtSQngKcPm"

    # Oldest to newest — deliberately NOT the order the fake DAO returns
    # sources in below, so "oldest first" genuinely exercises the endpoint's
    # own sort rather than an accident of DAO order.
    context.published_at = {
        context.dbs1: datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc),
        context.dbvh: datetime(2026, 8, 6, 21, 0, tzinfo=timezone.utc),
        context.dbts: datetime(2026, 8, 7, 23, 0, tzinfo=timezone.utc),
    }

    context.cover_key = {
        context.dbs1: f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/{context.dbs1}_1.jpg",
        context.dbvh: f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/{context.dbvh}_1.jpg",
        context.dbts: f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/{context.dbts}.jpg",
    }

    context.event_store.events[context.event_id] = {
        "event_id": context.event_id,
        "title": "NOITE DA PATROA",
        # The DAO's real _EVENT_SELECT derives this from the most-recently-
        # seen source (a LATERAL join) — mirrored by hand here since the
        # fake has no join of its own. All three sources share one
        # last_seen_at scheme below; dbts is the latest.
        "cover_photo_key": context.cover_key[context.dbts],
    }
    context.event_store.sources[context.event_id] = [
        _source_row(
            shortcode=context.dbts, handle="clubmetropole",
            uploaded_at=context.published_at[context.dbts], cover_key=context.cover_key[context.dbts],
        ),
        _source_row(
            shortcode=context.dbs1, handle="clubmetropole",
            uploaded_at=context.published_at[context.dbs1], cover_key=context.cover_key[context.dbs1],
        ),
        _source_row(
            shortcode=context.dbvh, handle="clubmetropole",
            uploaded_at=context.published_at[context.dbvh], cover_key=context.cover_key[context.dbvh],
        ),
    ]


@given("the archive holds five classified images across those three posts")
def step_five_images_background(context):
    manifest = {"photos": [
        _manifest_entry(shortcode=context.dbs1, suffix="1", category="flyer", confidence=0.91),
        _manifest_entry(shortcode=context.dbs1, suffix="2", category="other", confidence=0.35),
        _manifest_entry(shortcode=context.dbvh, suffix="1", category="flyer", confidence=0.88),
        _manifest_entry(shortcode=context.dbvh, suffix="2", category="flyer", confidence=0.72),
        _manifest_entry(shortcode=context.dbts, suffix=None, category="flyer", confidence=0.95),
    ]}
    context.manifest = manifest
    context.media_store.program_manifest(RUN_PREFIX, VENUE_ID, manifest)
    # The one ground-truth fact this feature exists to surface: classified
    # flyer, never read by the extractor (its key never became any source's
    # cover_photo_key).
    context.unread_flyer_key = next(
        e["key"] for e in manifest["photos"]
        if e["shortcode"] == context.dbvh and e["key"] != context.cover_key[context.dbvh]
    )


# ── given ────────────────────────────────────────────────────────────────────
@given("an event whose posts archived no images at all")
def step_event_no_media(context):
    for row in context.event_store.sources[context.event_id]:
        row["cover_photo_key"] = None
    context.event_store.events[context.event_id]["cover_photo_key"] = None


@given("the archived manifest for one of the posts cannot be read")
def step_one_manifest_unreadable(context):
    # Move dbts to its OWN run (a different day) so its manifest read is
    # independent of the other two posts' shared, working manifest —
    # proving the fallback is scoped to the one broken post, not the whole
    # response.
    broken_prefix = "retrieved/source=instagram_posts/year=2026/month=08/day=01/run_id=01J000000000000BROKENRUN/"
    broken_key = f"{broken_prefix}venue_id={VENUE_ID}/media/flyer/{context.dbts}.jpg"
    for row in context.event_store.sources[context.event_id]:
        if row["source_shortcode"] == context.dbts:
            row["cover_photo_key"] = broken_key
    context.cover_key[context.dbts] = broken_key
    context.media_store.program_manifest_unreadable(broken_prefix, VENUE_ID)


@given("one image's url cannot be signed")
def step_one_image_cannot_sign(context):
    # The non-flyer image on dbs1 — arbitrary but specific, and distinct
    # from the "unread flyer" fixture so the two concerns stay independent.
    broken_key = f"{RUN_PREFIX}venue_id={VENUE_ID}/media/{context.dbs1}_2.jpg"
    context.media_store.fail_presign_for(broken_key)
    context.unsignable_key = broken_key


@given("all three posts were archived in the same run")
def step_same_run_confirmed(context):
    # True by construction in the Background (every cover key shares
    # RUN_PREFIX/VENUE_ID) — asserted here so a future change to the
    # Background fixture that broke this precondition fails LOUDLY at setup
    # time instead of silently invalidating what this scenario claims to
    # prove.
    prefixes = {
        context.cover_key[sc].rsplit(f"venue_id={VENUE_ID}", 1)[0]
        for sc in (context.dbs1, context.dbvh, context.dbts)
    }
    assert prefixes == {RUN_PREFIX}, f"fixture drift: posts are not all under one run: {prefixes}"


# ── when ─────────────────────────────────────────────────────────────────────
@when("the event's media is requested")
def step_request_media(context):
    _request_media(context, context.event_id)


@when("the event's media is requested without the admin credential")
def step_request_media_unauth(context):
    context.request_headers = {}
    context.event_store.forbid_lookups = True
    _request_media(context, context.event_id)


@when("the event's media is requested with a storage key in the request")
def step_request_media_injected_key(context):
    context.injected_key = "raw/source=attacker/year=2099/month=01/day=01/run_id=EVIL/all-events.json.gz"
    _request_media(
        context, context.event_id,
        extra_headers={"X-Object-Key": context.injected_key},
        params={
            "key": context.injected_key, "object_key": context.injected_key,
            "s3_key": context.injected_key, "cover_photo_key": context.injected_key,
        },
    )


@when("media is requested for an event that does not exist")
def step_request_media_unknown_event(context):
    _request_media(context, "evt-does-not-exist")


@when("the event's cover is requested")
def step_request_cover_regression(context):
    headers = dict(_headers(context))
    context.response = context.esm_client.get(
        f"/admin/events/{context.event_id}/cover", headers=headers,
    )


# ── then ─────────────────────────────────────────────────────────────────────
@then("five images are returned")
def step_assert_five_images(context):
    body = _body(context)
    images = _all_images(body)
    assert len(images) == 5, f"expected 5 images, got {len(images)}: {body}"


@then("each image is reported under its own post's shortcode")
def step_assert_grouped_by_shortcode(context):
    body = _body(context)
    by_shortcode = {s["source_shortcode"]: s for s in body}
    expected = {
        context.dbs1: {
            context.cover_key[context.dbs1],
            f"{RUN_PREFIX}venue_id={VENUE_ID}/media/{context.dbs1}_2.jpg",
        },
        context.dbvh: {context.cover_key[context.dbvh], context.unread_flyer_key},
        context.dbts: {context.cover_key[context.dbts]},
    }
    for shortcode, expected_keys in expected.items():
        assert shortcode in by_shortcode, f"missing source {shortcode!r} in {list(by_shortcode)}"
        got_keys = {_key_from_signed_url(img["url"]) for img in by_shortcode[shortcode]["images"]}
        assert got_keys == expected_keys, f"{shortcode}: expected {expected_keys}, got {got_keys}"


@then("no image is reported under a post it does not belong to")
def step_assert_no_cross_contamination(context):
    body = _body(context)
    for source in body:
        shortcode = source["source_shortcode"]
        for img in source["images"]:
            key = _key_from_signed_url(img["url"])
            filename = key.rsplit("/", 1)[-1]
            assert filename.startswith(shortcode), (
                f"image {filename!r} listed under {shortcode!r}'s source"
            )


@then("the posts are ordered by when they were published, oldest first")
def step_assert_oldest_first(context):
    body = _body(context)
    got_order = [s["source_shortcode"] for s in body]
    expected_order = sorted(context.published_at, key=lambda sc: context.published_at[sc])
    assert got_order == expected_order, f"got {got_order}, expected {expected_order}"


@then("exactly one image per post is marked as the one the extractor read")
def step_assert_one_read_per_post(context):
    body = _body(context)
    for source in body:
        read_count = sum(1 for img in source["images"] if img["read_by_extractor"])
        assert read_count == 1, (
            f"source {source['source_shortcode']!r} has {read_count} read images, "
            f"expected 1: {source['images']}"
        )


@then("the image classified as a flyer that was never read is marked as unread")
def step_assert_unread_flyer_marked(context):
    body = _body(context)
    dbvh_source = next(s for s in body if s["source_shortcode"] == context.dbvh)
    unread_img = next(
        img for img in dbvh_source["images"]
        if _key_from_signed_url(img["url"]) == context.unread_flyer_key
    )
    assert unread_img["category"] == "flyer", unread_img
    assert unread_img["read_by_extractor"] is False, unread_img


@then("each image reports its classified category and its confidence")
def step_assert_category_and_confidence(context):
    body = _body(context)
    expected_by_key = {
        e["key"]: (e["category"], e["classification_confidence"])
        for e in context.manifest["photos"]
    }
    checked = 0
    for source in body:
        for img in source["images"]:
            key = _key_from_signed_url(img["url"])
            if key not in expected_by_key:
                continue
            expected_category, expected_confidence = expected_by_key[key]
            assert img["category"] == expected_category, img
            assert img["confidence"] == expected_confidence, img
            checked += 1
    assert checked == 5, f"expected to check 5 classified images, checked {checked}"


@then("the request succeeds")
def step_assert_request_succeeds(context):
    _body(context)


@then("no images are returned")
def step_assert_no_images(context):
    body = _body(context)
    assert _all_images(body) == [], body


@then("that post still reports its stored cover image")
def step_assert_fallback_reports_cover(context):
    body = _body(context)
    dbts_source = next(s for s in body if s["source_shortcode"] == context.dbts)
    assert len(dbts_source["images"]) == 1, dbts_source
    only_image = dbts_source["images"][0]
    assert _key_from_signed_url(only_image["url"]) == context.cover_key[context.dbts]
    assert only_image["read_by_extractor"] is True, only_image


@then("the manifest fallback is counted")
def step_assert_fallback_counted(context):
    from app.metrics import EVENT_MEDIA_TOTAL

    after = EVENT_MEDIA_TOTAL.labels(result="manifest_fallback")._value.get()
    before = context._media_metric_before["manifest_fallback"]
    assert after == before + 1, f"before={before} after={after}"


@then("the remaining images are still returned")
def step_assert_remaining_images_returned(context):
    body = _body(context)
    images = _all_images(body)
    assert len(images) == 4, f"expected 4 images (5 - 1 unsignable), got {len(images)}: {body}"
    keys = {_key_from_signed_url(img["url"]) for img in images}
    assert context.unsignable_key not in keys, keys


@then("the signing failure is counted")
def step_assert_signing_failure_counted(context):
    from app.metrics import EVENT_MEDIA_IMAGE_SIGN_TOTAL

    after = EVENT_MEDIA_IMAGE_SIGN_TOTAL.labels(result="failed")._value.get()
    before = context._sign_metric_before["failed"]
    assert after == before + 1, f"before={before} after={after}"


@then("the archive is read once")
def step_assert_manifest_read_once(context):
    assert context.response.status_code == 200, context.response.text
    assert context.media_store.manifest_calls == [(RUN_PREFIX, VENUE_ID)], context.media_store.manifest_calls


@then("the request is refused")
def step_assert_refused(context):
    assert context.response.status_code == 401, context.response.text


@then("no url is signed for that key")
def step_assert_injected_key_not_signed(context):
    assert context.response.status_code == 200, context.response.text
    signed_keys = {c["key"] for c in context.media_store.presign_calls}
    assert context.injected_key not in signed_keys, signed_keys
    body = context.response.json()
    for source in body:
        for img in source["images"]:
            assert context.injected_key not in img["url"], img["url"]


@then("the request reports the event was not found")
def step_assert_event_not_found(context):
    assert context.response.status_code == 404, context.response.text
    assert context.response.json().get("detail") == "Event not found", context.response.text


@then("a single signed url for the event's own cover is returned")
def step_assert_cover_unchanged(context):
    assert context.response.status_code == 200, context.response.text
    body = context.response.json()
    assert set(body.keys()) == {"url", "expires_at", "expires_in"}, body
    assert _key_from_signed_url(body["url"]) == context.cover_key[context.dbts], body
