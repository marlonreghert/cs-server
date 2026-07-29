"""Behave steps for tests/bdd/enrichment/instagram-handle-cascade.feature.

Drives the REAL InstagramCascadeService with fakes at its four boundaries: the
Google listing, the archived-payload reader, the paid Apify search, and the
Instagram profile probe. Extraction, tier ordering, confidence, judge-mode
selection and persistence are all real code.

The paid-search fake COUNTS its calls, because "resolved without spending" is
the whole point of the cascade and is only a real assertion if a scenario can
prove the paid client was never reached.
"""
from __future__ import annotations

import asyncio

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
class _FakeGoogleListing:
    def __init__(self):
        self.websites: dict[str, str] = {}
        self.calls: list[str] = []

    async def website_for(self, venue_id, venue=None):
        self.calls.append(venue_id)
        return self.websites.get(venue_id)


class _FakeArchive:
    """Archived Apify Google Maps payload reader."""

    def __init__(self):
        self.websites: dict[str, str] = {}
        self.calls: list[str] = []
        self.raises = False
        self.permitted = True

    @property
    def available(self):
        return self.permitted

    async def website_for(self, venue_id, venue=None):
        self.calls.append(venue_id)
        if self.raises:
            raise RuntimeError("archive read blew up")
        if not self.permitted:
            raise PermissionError("s3:GetObject denied")
        return self.websites.get(venue_id)


class _FakePaidSearch:
    """Apify Instagram search. Counts calls — the cost assertion depends on it."""

    def __init__(self):
        self.candidates: list[dict] = []
        self.calls = 0

    async def search(self, venue, limit=3):
        self.calls += 1
        return list(self.candidates)


class _FakeProbe:
    """Open Graph profile probe."""

    def __init__(self):
        self.profiles: dict[str, dict] = {}
        self.absent: set[str] = set()
        self.failing: set[str] = set()
        self.calls: list[str] = []

    async def fetch(self, handle):
        from app.api.instagram_profile_probe import ProfileProbeResult

        self.calls.append(handle)
        if handle in self.failing:
            return ProfileProbeResult(existence="unknown")
        if handle in self.absent or handle not in self.profiles:
            return ProfileProbeResult(existence="absent")
        p = self.profiles[handle]
        return ProfileProbeResult(
            existence="present",
            display_name=p.get("display_name"),
            followers_count=p.get("followers"),
            image_url=p.get("image_url"),
        )


class _FakeJudge:
    def __init__(self):
        self.calls: list[dict] = []
        self.verdict_is_match = True

    async def judge(self, *, venue, candidate, profile, venue_photos):
        from app.services.instagram_judge import JudgeVerdict, select_mode

        mode = select_mode(
            profile_image=(profile or {}).get("image_url"),
            venue_photos=venue_photos,
        )
        self.calls.append({"mode": mode, "handle": candidate})
        return JudgeVerdict(
            mode=mode, is_match=self.verdict_is_match, confidence=0.9, reason="fake"
        )


class _FakePhotoArchive:
    def __init__(self):
        self.photos: dict[str, list[bytes]] = {}

    async def venue_photos(self, venue_id, limit=3):
        return list(self.photos.get(venue_id, []))[:limit]


def _service_cls():
    try:
        from app.services.instagram_cascade_service import InstagramCascadeService

        return InstagramCascadeService
    except ImportError:
        return None


def _build(context):
    cls = _service_cls()
    assert cls is not None, (
        "app.services.instagram_cascade_service.InstagramCascadeService does not "
        "exist yet — the cascade must find handles from the cheapest source first"
    )
    context.service = cls(
        venue_dao=context.repository,
        google_listing=context.google_listing,
        archive=context.archive,
        paid_search=context.paid_search,
        probe=context.probe,
        judge=context.judge,
        photo_archive=context.photo_archive,
    )


def _seed(context, vid="ven_cascade", name="Bar Vibes"):
    from app.models import Venue

    context.repository.upsert_venue(
        Venue(
            forecast=True, processed=True, venue_id=vid, venue_name=name,
            venue_address="Rua Teste, Recife", venue_lat=-8.05, venue_lng=-34.88,
            priority=1,
        )
    )
    context.venue_id = vid
    return vid


def _cfg(context, **over):
    cfg = {"force_refresh": True}
    cfg.update(getattr(context, "config_over", {}))
    cfg.update(over)
    return cfg


def _discover(context, **over):
    context.result = _run(context.service.discover(context.venue_id, _cfg(context, **over)))
    return context.result


def _metric(name, **labels):
    v = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if v is None else float(v)


# ── Background ────────────────────────────────────────────────────────────────
@given("the Instagram handle cascade is enabled")
def step_cascade_enabled(context):
    context.google_listing = _FakeGoogleListing()
    context.archive = _FakeArchive()
    context.paid_search = _FakePaidSearch()
    context.probe = _FakeProbe()
    context.judge = _FakeJudge()
    context.photo_archive = _FakePhotoArchive()
    context.config_over = {}
    _build(context)


@given("a venue that has no Instagram handle yet")
def step_venue_no_handle(context):
    _seed(context)


# ── Given: sources ────────────────────────────────────────────────────────────
@given('the venue\'s Google listing has the website "{url}"')
def step_google_website(context, url):
    context.google_listing.websites[context.venue_id] = url
    h = url.rstrip("/").split("/")[-1].split("?")[0]
    context.probe.profiles.setdefault(h, {"display_name": "Bar Vibes"})


@given("the venue's Google listing has no website")
def step_no_google_website(context):
    context.google_listing.websites.pop(context.venue_id, None)


@given('the archived Google Maps payload has the website "{url}"')
def step_archived_website(context, url):
    context.archive.websites[context.venue_id] = url
    h = url.rstrip("/").split("/")[-1].split("?")[0]
    context.probe.profiles.setdefault(h, {"display_name": "Bar Vibes"})


@given("the archived Google Maps payload has a different Instagram website")
def step_archived_other(context):
    context.archive.websites[context.venue_id] = "https://instagram.com/otherhandle"
    context.probe.profiles.setdefault("otherhandle", {"display_name": "Other"})


@given("the venue has no archived Google Maps payload")
def step_no_archive(context):
    context.archive.websites.pop(context.venue_id, None)


@given("the paid search returns a strong candidate")
def step_paid_strong(context):
    context.paid_search.candidates = [{"username": "barvibes", "display_name": "Bar Vibes"}]
    context.probe.profiles["barvibes"] = {"display_name": "Bar Vibes", "followers": 5000}


# Regex matcher: with parse, `{handle}` greedily swallows `" with the display
# name "…` and the two registrations collide (AmbiguousStep).
use_step_matcher("re")


@given(r'the paid search returns the candidate "(?P<handle>[^"]*)"')
def step_paid_candidate(context, handle):
    context.paid_search.candidates = [{"username": handle}]


@given(
    r'the paid search returns the candidate "(?P<handle>[^"]*)"'
    r' with the display name "(?P<name>[^"]*)"'
)
def step_paid_candidate_named(context, handle, name):
    context.paid_search.candidates = [{"username": handle, "display_name": name}]
    context.probe.profiles[handle] = {"display_name": name, "followers": 5000}


use_step_matcher("parse")


@given("the venue already has a recently checked handle")
def step_fresh_handle(context):
    from app.models.instagram import VenueInstagram

    context.repository.set_venue_instagram(
        VenueInstagram(
            venue_id=context.venue_id, instagram_handle="already", status="found",
            confidence_score=0.99,
        )
    )
    context.config_over["force_refresh"] = False


@given('the profile "{handle}" publishes the display name "{name}"')
def step_profile_name(context, handle, name):
    context.probe.profiles[handle] = {"display_name": name, "followers": 1000}


@given('the profile "{handle}" publishes no profile metadata')
def step_profile_absent(context, handle):
    context.probe.profiles.pop(handle, None)
    context.probe.absent.add(handle)


@given('looking up the profile "{handle}" fails')
def step_profile_fails(context, handle):
    context.probe.failing.add(handle)


@given('the venue is named "{name}"')
def step_venue_named(context, name):
    _seed(context, vid=context.venue_id, name=name)


@given("the candidate is ambiguous")
def step_ambiguous(context):
    _seed(context, vid=context.venue_id, name="Bercy Boa Viagem")
    context.paid_search.candidates = [
        {"username": "bercyvillage", "display_name": "Bercy Village"}
    ]
    context.probe.profiles["bercyvillage"] = {
        "display_name": "Bercy Village", "followers": 76000,
        "image_url": "https://img/profile.jpg",
    }


@given("the profile publishes a usable profile picture")
def step_usable_pic(context):
    context.probe.profiles["bercyvillage"]["image_url"] = "https://img/profile.jpg"


@given("the profile publishes no usable profile picture")
def step_no_pic(context):
    context.probe.profiles["bercyvillage"]["image_url"] = None


@given("the venue has archived photos")
def step_has_photos(context):
    context.photo_archive.photos[context.venue_id] = [b"jpeg1", b"jpeg2"]


@given("the venue has no archived photos")
def step_no_photos(context):
    context.photo_archive.photos.pop(context.venue_id, None)


@given("the judge is not configured")
def step_no_judge(context):
    context.judge = None
    _build(context)


@given("the archived payload source raises an error")
def step_archive_raises(context):
    context.archive.raises = True


@given("reading the archived payload is not permitted")
def step_archive_denied(context):
    context.archive.permitted = False


@given("the paid source is disabled")
def step_paid_disabled(context):
    context.config_over["tier_apify_search_enabled"] = False


# ── When ──────────────────────────────────────────────────────────────────────
@when("the cascade runs for that venue")
def step_run_cascade(context):
    _discover(context)


@when("the cascade runs for every venue needing a handle")
def step_run_all(context):
    context.summary = _run(context.service.run(_cfg(context)))


@when("the judge is consulted")
def step_judge_consulted(context):
    _discover(context)


@when("a handle is accepted")
def step_handle_accepted(context):
    context.google_listing.websites[context.venue_id] = "https://instagram.com/barvibes"
    context.probe.profiles["barvibes"] = {"display_name": "Bar Vibes"}
    _discover(context)


@when("the cascade has run over several venues")
def step_run_several(context):
    context.google_listing.websites[context.venue_id] = "https://instagram.com/barvibes"
    context.probe.profiles["barvibes"] = {"display_name": "Bar Vibes"}
    _discover(context)


# ── Then ──────────────────────────────────────────────────────────────────────
@then("the handle is accepted")
def step_accepted_any(context):
    assert context.result.accepted, context.result
    assert context.result.handle, context.result


@then('the handle "{handle}" is accepted')
def step_accepted(context, handle):
    assert context.result.handle == handle, context.result
    assert context.result.accepted, context.result


@then('the handle is recorded with the source "{source}"')
def step_recorded_source(context, source):
    assert context.result.source == source, context.result
    stored = context.repository.get_venue_instagram(context.venue_id)
    assert stored is not None and stored.instagram_handle == context.result.handle


@then("no paid search is performed")
def step_no_paid(context):
    assert context.paid_search.calls == 0, (
        f"the paid search ran {context.paid_search.calls} time(s) when it should not have"
    )


@then("the paid search is performed exactly once")
def step_paid_once(context):
    assert context.paid_search.calls == 1, context.paid_search.calls


@then("only the first source is consulted")
def step_only_first(context):
    assert context.archive.calls == [], f"archive consulted: {context.archive.calls}"
    assert context.paid_search.calls == 0


@then("no source is consulted at all")
def step_no_source(context):
    assert context.google_listing.calls == []
    assert context.archive.calls == []
    assert context.paid_search.calls == 0


@then("no handle is extracted from that website")
def step_no_handle(context):
    assert not context.result.handle, context.result


@then('the rejection is counted with the reason "{reason}"')
def step_rejection_counted(context, reason):
    assert reason in (context.result.rejections or []), context.result


@then("the profile is confirmed to exist")
def step_profile_confirmed(context):
    assert context.result.profile_exists == "present", context.result


@then('the recorded evidence names the display name "{name}"')
def step_evidence_name(context, name):
    assert context.result.display_name == name, context.result


@then("the handle is not accepted")
def step_not_accepted(context):
    assert not context.result.accepted, context.result


@then("the venue is recorded as not found")
def step_recorded_not_found(context):
    stored = context.repository.get_venue_instagram(context.venue_id)
    assert stored is not None and stored.status == "not_found", stored


@then("the profile existence is recorded as unknown")
def step_existence_unknown(context):
    assert context.result.profile_exists == "unknown", context.result


@then("the handle is not rejected merely because the lookup failed")
def step_not_rejected_on_probe_failure(context):
    assert context.result.handle, context.result


@then("the judge is not consulted")
def step_judge_not_consulted(context):
    assert context.judge is None or context.judge.calls == [], context.judge.calls


@then("the judge is consulted")
def step_judge_was_consulted(context):
    assert context.judge.calls, "the judge was never consulted"


@then("the judge's verdict decides the outcome")
def step_verdict_decides(context):
    assert context.result.judge is not None, context.result


@then('the judge runs in the mode "{mode}"')
def step_judge_mode(context, mode):
    assert context.judge.calls, "the judge was not consulted"
    assert context.judge.calls[-1]["mode"] == mode, context.judge.calls[-1]


@then("a verdict is returned")
def step_verdict_returned(context):
    assert context.result.judge is not None, context.result


@then("the recorded confidence does not exceed the text-only ceiling")
def step_text_only_ceiling(context):
    from app.services.instagram_judge import TEXT_ONLY_CONFIDENCE_CEILING

    assert context.result.confidence <= TEXT_ONLY_CONFIDENCE_CEILING, context.result


@then("the outcome is decided by the cheap signals alone")
def step_cheap_signals(context):
    assert context.result.confidence is not None, context.result


@then("the judge is recorded as unavailable")
def step_judge_unavailable(context):
    assert context.result.judge_mode == "unavailable", context.result


@then("the venue is not failed")
def step_not_failed(context):
    assert context.result.error is None, context.result


@then("the failing source is counted as an error")
def step_source_error(context):
    assert "archived_gmaps_website" in (context.result.tier_errors or []), context.result


@then("the cascade still reaches the paid search")
def step_reaches_paid(context):
    assert context.paid_search.calls == 1, context.paid_search.calls


@then("the cascade run completes successfully")
def step_run_ok(context):
    assert context.result is not None


@then("the archived source is reported as unavailable")
def step_archive_unavailable(context):
    assert "archived_gmaps_website" in (context.result.tier_unavailable or []), context.result


@then("the cascade continues to the remaining sources")
def step_cascade_continues(context):
    assert context.paid_search.calls >= 1


@then("the handles found come only from the free sources")
def step_only_free(context):
    assert context.paid_search.calls == 0


@then("the stored record names the source it came from")
def step_stored_source(context):
    stored = context.repository.get_venue_instagram(context.venue_id)
    assert getattr(stored, "source", None) or context.result.source, stored


@then("the stored record carries the confidence and the signals behind it")
def step_stored_evidence(context):
    assert context.result.confidence is not None
    assert context.result.signals, context.result


@then("the source is queryable without reading the payload")
def step_source_column(context):
    rows = context.rds_store.list_instagram_sources() if hasattr(
        context.rds_store, "list_instagram_sources"
    ) else None
    assert rows is not None, (
        "instagram.handle needs a promoted `source` column queryable without "
        "jsonb extraction"
    )


@then("the attempts are exposed per source")
def step_metric_attempts(context):
    assert _metric("instagram_cascade_tier_attempts_total", source="google_website") > 0


@then("the number of paid calls is exposed")
def step_metric_paid(context):
    assert REGISTRY.get_sample_value("instagram_cascade_paid_calls_total") is not None


@then("the handle rejections are exposed by reason")
def step_metric_rejections(context):
    assert REGISTRY.get_sample_value(
        "instagram_handle_rejected_total", {"reason": "link_shim"}
    ) is not None
