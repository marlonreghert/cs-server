"""Behave steps for tests/bdd/api/add-venue-instagram-discovery.feature.

Drives the REAL AddVenueHandler (built by environment.py) plus a REAL
InstagramCascadeService wired with small fakes at its source boundaries — the
same shape instagram_handle_cascade_steps.py uses to drive the cascade on its
own. Using the real cascade here (rather than a stub returning canned
results) means the _source_enabled fix and the suppress_not_found_cache
behavior are exercised end-to-end through the add-venue handler, not just in
isolation.

Venue ids are deterministic on venue_name (see _venue_id_for) so a Given step
can pre-program a source fake for a venue before the id is known from a
BestTime response.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from behave import given, when, then  # type: ignore[import-untyped]

from app.config import settings
from app.models import NewVenueResponse, Venue, VenueFilterResponse, VenueFilterVenue
from app.services.instagram_cascade_service import InstagramCascadeService

_DEFAULT_ADDRESS = "Rua Exemplo 100, Recife - PE, 50000-000, Brazil"
_DEFAULT_LAT = -8.05
_DEFAULT_LNG = -34.88
# Far enough from _DEFAULT_LAT/_DEFAULT_LNG that AddVenueHandler._geo_lookup's
# own Redis-geo short-circuit (same default 50m radius as the geo fallback)
# never fires for a venue seeded here — only the geo-FALLBACK path (BestTime's
# venues/filter, entirely stubbed) should find it.
_FAR_LAT = -8.30
_FAR_LNG = -35.10


def _venue_id_for(venue_name: str) -> str:
    return "ven_ig_" + hashlib.sha1(venue_name.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Fakes — mirrors instagram_handle_cascade_steps.py's boundary fakes.
# ---------------------------------------------------------------------------


class _FakeWebsiteSource:
    """A free-tier (or google_search) reader: website_for(venue_id, venue)."""

    def __init__(self):
        self.websites: dict[str, str] = {}
        self.calls: list[str] = []
        self.delay: float = 0.0

    async def website_for(self, venue_id, venue=None):
        self.calls.append(venue_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.websites.get(venue_id)


class _FakePaidSearch:
    """The Apify user-search tier. Must never be called at add time — the
    calls list is exactly the add-time zero-spend assertion."""

    def __init__(self):
        self.calls: list = []

    async def search(self, venue, limit=3):
        self.calls.append(getattr(venue, "venue_id", None))
        return []


class _FakeJudge:
    def __init__(self):
        self.calls: list[dict] = []
        self.verdict_is_match = True

    async def judge(self, *, venue, candidate, profile, venue_photos):
        from app.services.instagram_judge import JudgeVerdict, select_mode

        mode = select_mode(
            profile_image=(profile or {}).get("image_url"), venue_photos=venue_photos
        )
        self.calls.append({"mode": mode, "handle": candidate})
        return JudgeVerdict(
            mode=mode, is_match=self.verdict_is_match, confidence=0.9,
            reason="bdd-fake-judge",
        )


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("the add-venue endpoint is configured")
def step_endpoint_configured(context):
    # Built fresh per-scenario by environment.py's before_scenario.
    assert context.add_venue_handler is not None


@given("Instagram discovery at add time is enabled")
def step_discovery_enabled(context):
    _override_setting(context, "add_venue_instagram_enabled", True)
    _install_dynamic_besttime(context)


@given(
    "the Instagram cascade is configured with its free sources, the "
    "Google-search tier, and the judge"
)
def step_cascade_configured(context):
    # plans/260811_instagram-discovery-admin-flags.md: the add-time cascade
    # config is now RESOLVED per add (admin config, falling back to these two
    # Settings) rather than hardcoded True. This feature's scenarios write no
    # "instagram_discovery" admin config, so — to keep meaning "the
    # Google-search tier and the judge are available and attempted", exactly
    # as it did before that plan — this fixture must also enable the Settings
    # fallback, not just wire the fake collaborators.
    _override_setting(context, "instagram_google_search_enabled", True)
    _override_setting(context, "instagram_judge_enabled", True)

    context.ig_google_listing = _FakeWebsiteSource()
    context.ig_archive = _FakeWebsiteSource()
    context.ig_venue_website = _FakeWebsiteSource()
    context.ig_google_search = _FakeWebsiteSource()
    context.ig_paid_search = _FakePaidSearch()
    context.ig_judge = _FakeJudge()

    service = InstagramCascadeService(
        venue_dao=context.venue_dao,
        google_listing=context.ig_google_listing,
        archive=context.ig_archive,
        venue_website=context.ig_venue_website,
        google_search=context.ig_google_search,
        paid_search=context.ig_paid_search,
        probe=None,  # blocked in production; every real caller also has none
        judge=context.ig_judge,
    )
    context.ig_discover_calls: list[str] = []
    real_discover = service.discover

    async def _tracked_discover(venue_id, config=None):
        context.ig_discover_calls.append(venue_id)
        return await real_discover(venue_id, config)

    service.discover = _tracked_discover
    context.instagram_cascade_service = service
    context.add_venue_handler.instagram_cascade_service = service


# ---------------------------------------------------------------------------
# BestTime create programming (dynamic per venue_name)
# ---------------------------------------------------------------------------


def _install_dynamic_besttime(context) -> None:
    # Guard by IDENTITY, not a plain flag: behave's context does not reset
    # plain attributes between scenarios, but environment.py builds a fresh
    # `context.besttime` every scenario, so a flag alone would skip
    # reinstalling onto the new instance and leave it with no
    # add_venue_to_account override at all.
    if getattr(context, "_ig_besttime_instance", None) is context.besttime:
        return
    context._ig_besttime_instance = context.besttime
    context._besttime_rejected_names = set()

    async def _dynamic_add_venue(venue_name, venue_address):
        context.besttime.calls.append(
            {
                "method": "add_venue_to_account",
                "venue_name": venue_name,
                "venue_address": venue_address,
            }
        )
        if venue_name in context._besttime_rejected_names:
            return NewVenueResponse.model_validate(
                {"status": "Error", "message": "Could not geocode address"}
            )
        return NewVenueResponse.model_validate(
            {
                "status": "OK",
                "venue_info": {
                    "venue_id": _venue_id_for(venue_name),
                    "venue_name": venue_name,
                    "venue_address": venue_address,
                    "venue_lat": _DEFAULT_LAT,
                    "venue_lon": _DEFAULT_LNG,
                },
                "analysis": [],
            }
        )

    context.besttime.add_venue_to_account = _dynamic_add_venue


@given('BestTime creates the venue "{venue_name}" successfully')
def step_besttime_creates(context, venue_name):
    context.ig_current_venue_name = venue_name
    _install_dynamic_besttime(context)
    context._besttime_rejected_names.discard(venue_name)


@given('BestTime rejects the address for "{venue_name}"')
def step_besttime_rejects(context, venue_name):
    context.ig_current_venue_name = venue_name
    _install_dynamic_besttime(context)
    context._besttime_rejected_names.add(venue_name)


# ---------------------------------------------------------------------------
# Source fixture setup
# ---------------------------------------------------------------------------


@given('the venue\'s Google listing website is "{url}"')
def step_google_listing_website(context, url):
    vid = _venue_id_for(_current_venue_name(context))
    context.ig_google_listing.websites[vid] = url


@given("no Instagram source has a handle for the venue")
def step_no_source_has_handle(context):
    # Fakes default to "no website" for any venue id — nothing to seed.
    pass


@given("no free Instagram source has a handle for the venue")
def step_no_free_source_has_handle(context):
    pass


@given('the Google-search tier returns "{url}"')
def step_google_search_returns(context, url):
    vid = _venue_id_for(_current_venue_name(context))
    context.ig_google_search.websites[vid] = url


@given("the judge confirms the candidate matches the venue")
def step_judge_confirms(context):
    context.ig_judge.verdict_is_match = True


@given("the judge rejects the candidate as not matching the venue")
def step_judge_rejects(context):
    context.ig_judge.verdict_is_match = False


@given("Instagram discovery takes longer than its add-time deadline")
def step_discovery_slow(context):
    _override_setting(context, "add_venue_instagram_deadline_seconds", 0.05)
    context.ig_google_listing.delay = 0.5


@given("Instagram discovery raises an error")
def step_discovery_raises(context):
    async def _boom(venue_id, config=None):
        context.ig_discover_calls.append(venue_id)
        raise RuntimeError("simulated cascade failure")

    context.instagram_cascade_service.discover = _boom


@given("the Instagram cascade is not configured")
def step_cascade_not_configured(context):
    context.add_venue_handler.instagram_cascade_service = None


# ---------------------------------------------------------------------------
# already-exists / geo-fallback fixture setup
# ---------------------------------------------------------------------------


def _current_venue_name(context) -> str:
    name = getattr(context, "ig_current_venue_name", None)
    if name:
        return name
    raise AssertionError(
        "no venue name pinned yet — a source-fixture Given step ran before "
        "'BestTime creates/rejects the venue \"X\"' (which pins the name)"
    )


@given('the venue "{venue_name}" is already in the catalog')
def step_venue_already_in_catalog(context, venue_name):
    vid = _venue_id_for(venue_name)
    context.venue_dao.upsert_venue(
        Venue(
            processed=True,
            forecast=True,
            venue_id=vid,
            venue_name=venue_name,
            venue_address=_DEFAULT_ADDRESS,
            venue_lat=_DEFAULT_LAT,
            venue_lng=_DEFAULT_LNG,
        )
    )
    context.add_venue_handler._save_address_cache(venue_name, _DEFAULT_ADDRESS, vid)
    context.ig_existing_venue_id = vid


@given("the geo fallback newly links the venue to a BestTime venue")
def step_geo_fallback_newly_links(context):
    venue_name = _current_venue_name(context)
    vid = _venue_id_for(venue_name)
    matched = VenueFilterVenue(
        venue_id=vid,
        venue_name=venue_name,
        venue_address=_DEFAULT_ADDRESS,
        venue_lat=_DEFAULT_LAT,
        venue_lng=_DEFAULT_LNG,
        venue_type="BAR",
        day_int=0,
        day_raw=[0] * 24,
    )
    context.besttime.programmed_venue_filter = VenueFilterResponse(
        status="OK", venues=[matched], venues_n=1
    )


@given("the geo fallback links the venue to a venue already in the catalog")
def step_geo_fallback_links_existing(context):
    venue_name = _current_venue_name(context)
    vid = _venue_id_for(venue_name)
    # Seeded FAR from the submitted coordinate so AddVenueHandler._geo_lookup
    # (our own Redis geo index, same default radius as the fallback) does not
    # short-circuit the add before BestTime is even called — only the
    # (fully-stubbed) BestTime venues/filter response below "finds" it.
    context.venue_dao.upsert_venue(
        Venue(
            processed=True,
            forecast=True,
            venue_id=vid,
            venue_name=venue_name,
            venue_address="A different, previously-catalogued address",
            venue_lat=_FAR_LAT,
            venue_lng=_FAR_LNG,
        )
    )
    matched = VenueFilterVenue(
        venue_id=vid,
        venue_name=venue_name,
        venue_address=_DEFAULT_ADDRESS,
        venue_lat=_DEFAULT_LAT,
        venue_lng=_DEFAULT_LNG,
        venue_type="BAR",
        day_int=0,
        day_raw=[0] * 24,
    )
    context.besttime.programmed_venue_filter = VenueFilterResponse(
        status="OK", venues=[matched], venues_n=1
    )


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


class _StepResponse:
    """Mirrors add_venue_by_address_steps.py's harness response shape so this
    file can reuse the global 'the response status is {status:d}' step
    (defined once, in user_activity_tracking_steps.py) instead of redefining
    an ambiguous duplicate."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


@when('I add the venue "{venue_name}"')
def step_add_venue(context, venue_name):
    context.ig_current_venue_name = venue_name
    body = {
        "venue_name": venue_name,
        "venue_address": _DEFAULT_ADDRESS,
        "venue_lat": _DEFAULT_LAT,
        "venue_lng": _DEFAULT_LNG,
    }
    from app.handlers.add_venue_handler import AddVenueByAddressRequest

    request = AddVenueByAddressRequest.model_validate(body)
    outcome = asyncio.run(context.add_venue_handler.add(request))
    context.response = _StepResponse(outcome.status_code, outcome.body)


# ---------------------------------------------------------------------------
# Then — response shape
# ---------------------------------------------------------------------------


def _dotted_get(body: dict, path: str):
    cur = body
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


@then('the response "{path}" is "{value}"')
def step_response_path_eq(context, path, value):
    actual, present = _dotted_get(context.response.json(), path)
    assert present, f"{path!r} missing in {context.response.json()}"
    assert actual == value, f"{path}: expected {value!r}, got {actual!r}"


@then('the response "{path}" is null')
def step_response_path_null(context, path):
    actual, present = _dotted_get(context.response.json(), path)
    assert present, f"{path!r} missing in {context.response.json()}"
    assert actual is None, f"{path}: expected null, got {actual!r}"


@then('the response "{path}" is not "{value}"')
def step_response_path_ne(context, path, value):
    actual, present = _dotted_get(context.response.json(), path)
    assert present, f"{path!r} missing in {context.response.json()}"
    assert actual != value, f"{path}: expected != {value!r}, got {actual!r}"


@then('the response "{path}" is true')
def step_response_path_true(context, path):
    actual, present = _dotted_get(context.response.json(), path)
    assert present, f"{path!r} missing in {context.response.json()}"
    assert actual is True, f"{path}: expected true, got {actual!r}"


@then('the response "{path}" is false')
def step_response_path_false(context, path):
    actual, present = _dotted_get(context.response.json(), path)
    assert present, f"{path!r} missing in {context.response.json()}"
    assert actual is False, f"{path}: expected false, got {actual!r}"


@then('the response has no "{field}" field')
def step_response_no_field(context, field):
    assert field not in context.response.json(), context.response.json()


@then("the venue is persisted in the catalog")
def step_venue_persisted(context):
    vid = context.response.json().get("venue_id")
    assert vid, context.response.json()
    assert context.venue_dao.get_venue(vid) is not None, vid


@then('the venue\'s Instagram handle is persisted as "{handle}"')
def step_persisted_handle(context, handle):
    vid = context.response.json().get("venue_id")
    record = context.venue_dao.get_venue_instagram(vid)
    assert record is not None, f"no Instagram record persisted for {vid}"
    assert record.instagram_handle == handle, record.instagram_handle


@then("the venue has no Instagram handle attached")
def step_no_handle_attached(context):
    vid = context.response.json().get("venue_id")
    record = context.venue_dao.get_venue_instagram(vid)
    assert record is None or not record.instagram_handle, record


@then("no not-found result is cached for the venue")
def step_no_not_found_cached(context):
    vid = context.response.json().get("venue_id")
    record = context.venue_dao.get_venue_instagram(vid)
    assert record is None, f"expected no cached record for {vid}, found {record}"


@then("a later full cascade run for the venue consults its sources again")
def step_full_cascade_consults_again(context):
    vid = context.response.json().get("venue_id")
    before = len(context.ig_google_listing.calls)
    asyncio.run(context.instagram_cascade_service.discover(vid, {}))
    after = len(context.ig_google_listing.calls)
    assert after > before, (
        "expected the free sources to be consulted again on a fresh full "
        f"cascade run (freshness gate should not have short-circuited); "
        f"calls before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Then — source attempt / no-attempt
# ---------------------------------------------------------------------------

_SOURCE_FAKES = {
    "google_website": "ig_google_listing",
    "archived_gmaps_website": "ig_archive",
    "venue_website": "ig_venue_website",
    "google_search": "ig_google_search",
}


@then('the cascade attempts the "{source}" source')
def step_cascade_attempts(context, source):
    if source == "apify_search":
        assert context.ig_paid_search.calls, "expected apify_search to be attempted"
        return
    fake = getattr(context, _SOURCE_FAKES[source])
    assert fake.calls, f"expected {source} to be attempted"


@then('the cascade does not attempt the "{source}" source')
def step_cascade_does_not_attempt(context, source):
    if source == "apify_search":
        assert not context.ig_paid_search.calls, (
            f"apify_search must never be attempted here: {context.ig_paid_search.calls}"
        )
        return
    fake = getattr(context, _SOURCE_FAKES[source])
    assert not fake.calls, f"{source} must not be attempted here: {fake.calls}"


@then("no Instagram discovery is attempted")
def step_no_discovery_attempted(context):
    assert context.ig_discover_calls == [], context.ig_discover_calls


# ---------------------------------------------------------------------------
# Batch add
# ---------------------------------------------------------------------------


@given("a batch add job for the venues:")
def step_batch_job(context):
    venues = []
    for row in context.table:
        name = row["venue_name"]
        available = row["instagram_available"].strip().lower() == "yes"
        vid = _venue_id_for(name)
        context._besttime_rejected_names.discard(name)
        if available:
            context.ig_google_listing.websites[vid] = (
                f"https://instagram.com/{_slug(name)}"
            )
        venues.append(
            {
                "venue_name": name,
                "venue_address": f"{_DEFAULT_ADDRESS} ({name})",
                "venue_lat": _DEFAULT_LAT,
                "venue_lng": _DEFAULT_LNG,
            }
        )
    context.ig_batch_venues = venues


def _slug(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


@when("the batch job completes")
def step_run_batch_job(context):
    import time as _time

    # Driven over the real HTTP endpoint (not batch_add_service.start_job()
    # directly): start_job() calls asyncio.create_task, which needs a running
    # event loop — the ASGI TestClient provides one, a bare asyncio.run() call
    # does not.
    body = {
        "label": "bdd-ig-batch",
        "resolve_coords": False,
        "venues": context.ig_batch_venues,
    }
    resp = context.client.post("/admin/venues/batch-add", json=body)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    last = None
    for _ in range(200):
        poll = context.client.get(f"/admin/venues/batch-add/{job_id}")
        assert poll.status_code == 200, poll.text
        last = poll.json()
        if last.get("status") in ("done", "stopped", "failed"):
            break
        _time.sleep(0.02)
    assert last is not None and last["status"] == "done", last
    context.ig_batch_job = last


def _batch_row(context, venue_name: str) -> dict:
    for row in context.ig_batch_job["results"]:
        if row.get("venue_name") == venue_name:
            return row
    raise AssertionError(f"no batch row for {venue_name!r}: {context.ig_batch_job['results']}")


@then('the row for "{venue_name}" reports outcome "{outcome}"')
def step_batch_row_outcome(context, venue_name, outcome):
    row = _batch_row(context, venue_name)
    assert row.get("outcome") == outcome, row


@then('the row for "{venue_name}" reports "instagram.status" as "{status}"')
def step_batch_row_instagram_status(context, venue_name, status):
    row = _batch_row(context, venue_name)
    instagram = row.get("instagram") or {}
    assert instagram.get("status") == status, row


# ---------------------------------------------------------------------------
# Zero-cost operator run
# ---------------------------------------------------------------------------


@given("an operator triggers a cascade run with the paid tier disabled")
def step_operator_zero_cost_run(context):
    # Seed one servable venue so the run has something to iterate — an empty
    # run would make the "does not attempt" assertions vacuously true.
    vid = "ven_ig_operator_run_001"
    context.venue_dao.upsert_venue(
        Venue(
            processed=True,
            forecast=True,
            venue_id=vid,
            venue_name="Operator Run Venue",
            venue_address=_DEFAULT_ADDRESS,
            venue_lat=_DEFAULT_LAT,
            venue_lng=_DEFAULT_LNG,
        )
    )
    context.ig_operator_run_config = {"tier_apify_search_enabled": False}


@when("the run completes")
def step_operator_run_completes(context):
    asyncio.run(context.instagram_cascade_service.run(context.ig_operator_run_config))


# ---------------------------------------------------------------------------
# Settings override bookkeeping (restored in environment.py's after_scenario)
# ---------------------------------------------------------------------------


def _override_setting(context, name: str, value) -> None:
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)
