"""Behave steps for tests/bdd/enrichment/cascade-scores-without-a-probe.feature.

Drives the REAL cascade with the REAL production numbers: the deployed
`instagram_auto_accept_threshold` is 0.8, and tier 1 tops out at 0.75 when the
existence check cannot run. That gap is the whole feature.
"""
from __future__ import annotations

import asyncio

from behave import given, then, when  # type: ignore[import-untyped]

PROD_ACCEPT = 0.8  # what config/cs-server.json actually deploys
PROD_AMBIGUOUS_LOW = 0.5


def _probe_mod():
    from app.api import instagram_profile_probe as m

    return m


class _Venue:
    def __init__(self, name):
        self.venue_name = name


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
    def __init__(self, username=None, display_name=None):
        self.username = username
        self.display_name = display_name

    async def search(self, venue):
        if not self.username:
            return []
        return [{"username": self.username, "display_name": self.display_name}]


class _Probe:
    def __init__(self, existence, display_name=None):
        self.existence = existence
        self.display_name = display_name

    async def fetch(self, handle):
        m = _probe_mod()
        return m.ProfileProbeResult(
            existence=self.existence, display_name=self.display_name
        )


# ── Background ────────────────────────────────────────────────────────────────
@given('the venue being scored is named "{name}"')
def step_venue_named(context, name):
    context.venue = _Venue(name)
    context.dao = _Dao(context.venue)
    context.paid = _PaidSearch()
    context.cascade_probe = None


@given('the scored venue\'s own Google listing links "{website}"')
def step_listing_links_url(context, website):
    context.listing = _Listing(website)


@given("the scored venue's own Google listing links nothing")
def step_listing_links_nothing(context):
    context.listing = _Listing(None)


# ── Probe states ──────────────────────────────────────────────────────────────
@given("Instagram refuses to answer existence checks")
def step_probe_blocked(context):
    m = _probe_mod()
    context.cascade_probe = _Probe(m.EXIST_BLOCKED)


@given("the existence check could not be completed")
def step_probe_unknown_state(context):
    m = _probe_mod()
    context.cascade_probe = _Probe(m.EXIST_UNKNOWN)


@given("the existence check confirms the profile does not exist")
def step_probe_absent_state(context):
    m = _probe_mod()
    context.cascade_probe = _Probe(m.EXIST_ABSENT)


@given("the existence check confirms the profile exists")
def step_probe_present_state(context):
    m = _probe_mod()
    context.cascade_probe = _Probe(m.EXIST_PRESENT)


@given('the existence check returns the display name "{name}"')
def step_probe_display_name(context, name):
    m = _probe_mod()
    context.cascade_probe = _Probe(m.EXIST_PRESENT, display_name=name)


@given('the paid search proposes the handle "{handle}"')
def step_paid_proposes(context, handle):
    context.paid = _PaidSearch(username=handle)


# ── When ──────────────────────────────────────────────────────────────────────
@when("the cascade scores the venue's handle")
def step_discover_handle(context):
    from app.services.instagram_cascade_service import InstagramCascadeService

    service = InstagramCascadeService(
        venue_dao=context.dao,
        google_listing=context.listing,
        paid_search=context.paid,
        probe=context.cascade_probe,
        accept_threshold=PROD_ACCEPT,
        ambiguous_low=PROD_AMBIGUOUS_LOW,
    )
    context.result = asyncio.run(service.discover("ven_1", {"force_refresh": True}))


# ── Then ──────────────────────────────────────────────────────────────────────
@then("the cascade accepts the scored handle")
def step_accepts_handle(context):
    assert context.result.accepted, (
        f"rejected at confidence {context.result.confidence} against a bar of "
        f"{PROD_ACCEPT} — the venue's own listing is the strongest free evidence "
        "the platform has"
    )


@then("the cascade does not accept the handle")
def step_rejects_handle(context):
    assert not context.result.accepted, context.result


@then('the stored record has the status "{status}"')
def step_stored_status(context, status):
    saved = context.dao.saved
    assert saved is not None, "nothing was persisted"
    assert saved.status == status, f"status={saved.status!r}"


@then("the acceptance bar was not lowered")
def step_bar_not_lowered(context):
    signals = context.result.signals or {}
    bar = signals.get("effective_threshold", PROD_ACCEPT)
    assert bar == PROD_ACCEPT, f"bar was moved to {bar} while the probe worked"


@then("the stored record shows the acceptance bar was lowered")
def step_bar_lowered(context):
    signals = context.result.signals or {}
    bar = signals.get("effective_threshold")
    assert bar is not None and bar < PROD_ACCEPT, (
        f"the record does not say the bar moved (signals={signals}) — a past "
        "acceptance must be explainable without re-running anything"
    )


@then("the recorded name similarity is above {value:f}")
def step_similarity_above(context, value):
    sim = (context.result.signals or {}).get("name_similarity")
    assert sim is not None and sim > value, (
        f"name_similarity={sim} — the handle itself was available to compare "
        "against and was ignored"
    )
