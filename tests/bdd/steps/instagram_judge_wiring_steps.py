"""Behave steps for tests/bdd/enrichment/wire-the-instagram-judge.feature.

Drives the REAL cascade and the REAL InstagramJudge against a fake OpenAI
client, so the prompt path, the text-only ceiling and the failure handling are
all exercised without a network call.
"""
from __future__ import annotations

import asyncio

from behave import given, then, when  # type: ignore[import-untyped]

PROD_ACCEPT = 0.8
PROD_AMBIGUOUS_LOW = 0.5
BAR = PROD_ACCEPT - 0.15  # existence bonus is uncollectable in production


class _Venue:
    def __init__(self, name):
        self.venue_name = name
        self.venue_address = "Av. Boa Viagem, Recife"


class _Dao:
    def __init__(self, venue):
        self.venue = venue
        self.saved = None

    def list_servable_venue_ids(self):
        return ["ven_1"]

    def get_venue(self, venue_id):
        return self.venue

    def get_venue_instagram(self, venue_id):
        return None

    def set_venue_instagram(self, record):
        self.saved = record


class _Listing:
    def __init__(self, website):
        self.website = website

    async def website_for(self, venue_id, venue=None):
        return self.website


class _PaidSearch:
    def __init__(self, username=None):
        self.username = username

    async def search(self, venue):
        return [{"username": self.username, "display_name": None}] if self.username else []


class _Probe:
    def __init__(self, existence):
        self.existence = existence

    async def fetch(self, handle):
        from app.api.instagram_profile_probe import ProfileProbeResult

        return ProfileProbeResult(existence=self.existence)


class _FakeOpenAI:
    """Stands in for the judge client. Records that it was asked."""

    def __init__(self, *, is_match=True, confidence=0.9, fail=False):
        self.is_match = is_match
        self.confidence = confidence
        self.fail = fail
        self.calls = []

    async def judge_instagram_match(self, *, prompt, model, profile_image_url, venue_photos):
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("openai is down")
        return {"is_match": self.is_match, "confidence": self.confidence,
                "reason": "the bio names the venue"}


def _judge(context):
    from app.services.instagram_judge import InstagramJudge

    if context.openai is None:
        return None
    return InstagramJudge(context.openai)


# ── Background ────────────────────────────────────────────────────────────────
@given('a venue awaiting adjudication named "{name}"')
def step_venue(context, name):
    context.venue = _Venue(name)
    context.dao = _Dao(context.venue)
    context.openai = None
    context.probe = None
    context.judge_run_enabled = None
    context.listing = _Listing(None)
    context.paid = _PaidSearch()


@given("a candidate the cheap signals scored below the bar")
def step_candidate_below_bar(context):
    # venue_website provenance 0.40; a partial name match lands under 0.65.
    context.listing = _Listing(None)
    context.paid = _PaidSearch("tiopepe_recife_oficial_x")


@given("the candidate came from the paid search and scored {score:f}")
def step_paid_candidate(context, score):
    """The real production pair: Subway scored 0.428 and was discarded unseen,
    because the old floor (0.50) sat above it."""
    context.venue = _Venue("Subway")
    context.dao = _Dao(context.venue)
    context.listing = _Listing(None)
    context.paid = _PaidSearch("subwayoficialbr")
    context.expected_pre_judge = score


@given("the candidate already scores above the bar")
def step_above_bar(context):
    # The venue's own Google listing: provenance 0.75, over the bar by itself.
    context.listing = _Listing("https://instagram.com/tiopepe")
    context.paid = _PaidSearch()


@given("the profile is confirmed not to exist")
def step_absent(context):
    from app.api.instagram_profile_probe import EXIST_ABSENT

    context.probe = _Probe(EXIST_ABSENT)


@given("the judge confirms the profile belongs to the venue")
def step_judge_confirms(context):
    context.openai = _FakeOpenAI(is_match=True, confidence=0.95)


@given("the judge says the profile belongs to somebody else")
def step_judge_denies(context):
    context.openai = _FakeOpenAI(is_match=False, confidence=0.9)


@given("the judge fails to answer")
def step_judge_fails(context):
    context.openai = _FakeOpenAI(fail=True)


@given("no judge is configured")
def step_no_judge(context):
    context.openai = None


@given("no images are available to compare")
def step_no_images(context):
    context.no_images = True


@given("the judge is turned off for this run")
def step_judge_off(context):
    context.judge_run_enabled = False


# ── When ──────────────────────────────────────────────────────────────────────
@when("the venue is adjudicated")
def step_adjudicate(context):
    from app.services.instagram_cascade_service import InstagramCascadeService

    service = InstagramCascadeService(
        venue_dao=context.dao,
        google_listing=context.listing,
        paid_search=context.paid,
        probe=context.probe,
        judge=_judge(context),
        accept_threshold=PROD_ACCEPT,
        ambiguous_low=PROD_AMBIGUOUS_LOW,
    )
    config = {"force_refresh": True}
    if context.judge_run_enabled is not None:
        config["judge_enabled"] = context.judge_run_enabled
    context.result = asyncio.run(service.discover("ven_1", config))


# ── Then ──────────────────────────────────────────────────────────────────────
@then("the candidate is accepted")
def step_accepted(context):
    assert context.result.accepted, (
        f"rejected at {context.result.confidence} "
        f"(judge_mode={context.result.judge_mode})"
    )


@then("the candidate is not accepted")
def step_not_accepted(context):
    assert not context.result.accepted, context.result


@then("the stored record names the judge mode")
def step_records_mode(context):
    assert context.result.judge_mode, "no judge mode recorded"


@then("the recorded confidence is at most {ceiling:f}")
def step_capped(context, ceiling):
    assert context.result.confidence <= ceiling + 1e-9, (
        f"a verdict reached without images reported {context.result.confidence}"
    )


@then("the candidate keeps the confidence the cheap signals gave it")
def step_unchanged(context):
    signals = context.result.signals or {}
    expected = min(
        signals.get("provenance", 0) + signals.get("profile_exists", 0)
        + 0.40 * signals.get("name_similarity", 0), 1.0
    )
    assert abs(context.result.confidence - expected) < 1e-6, (
        f"confidence moved to {context.result.confidence} without a verdict"
    )


@then("the run records that the judge was unavailable")
def step_unavailable(context):
    from app.services.instagram_judge import MODE_UNAVAILABLE

    assert context.result.judge_mode == MODE_UNAVAILABLE, context.result.judge_mode


@then("the judge is never consulted")
def step_never_called(context):
    calls = context.openai.calls if context.openai else []
    assert calls == [], f"the judge was asked {len(calls)} time(s) it should not have been"
