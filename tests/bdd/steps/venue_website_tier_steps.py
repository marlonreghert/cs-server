"""Behave steps for tests/bdd/enrichment/venue-website-instagram-tier.feature.

Drives the REAL source and the REAL cascade against fake HTTP, never the network.
The footer markup is the shape venue sites actually use.
"""
from __future__ import annotations

import asyncio

import httpx
from behave import given, then, when  # type: ignore[import-untyped]

PROD_ACCEPT = 0.8
PROD_AMBIGUOUS_LOW = 0.5


def _page(link=None):
    body = ['<html><body><footer><ul>']
    if link:
        body.append(f'<li><a href="{link}" target="_blank">Instagram</a></li>')
    body.append('<li><a href="https://facebook.com/x">Facebook</a></li>')
    body.append('</ul></footer></body></html>')
    return "".join(body)


class _Venue:
    def __init__(self, name):
        self.venue_name = name


class _Dao:
    def __init__(self, venue):
        self.venue = venue
        self.saved = None
        self.website_uri = None
        self.listing_website = None

    def list_servable_venue_ids(self):
        return ["ven_1"]

    def get_venue(self, venue_id):
        return self.venue

    def get_venue_instagram(self, venue_id):
        return None

    def set_venue_instagram(self, record):
        self.saved = record

    def get_vibe_attributes(self, venue_id):
        class _V:
            pass

        v = _V()
        v.website_uri = self.website_uri
        return v


class _Listing:
    def __init__(self, website=None):
        self.website = website

    async def website_for(self, venue_id, venue=None):
        return self.website


class _PaidSearch:
    def __init__(self):
        self.called = False

    async def search(self, venue):
        self.called = True
        return [{"username": "somethingelse", "display_name": "Something Else"}]


def _client(context):
    """A fake internet: one page, or a failure mode."""
    def handler(request):
        context.fetched.append(str(request.url))
        mode = context.site_mode
        if mode == "timeout":
            raise httpx.ReadTimeout("too slow", request=request)
        if mode == "huge":
            return httpx.Response(200, text="x" * 5_000_000)
        return httpx.Response(200, text=_page(context.page_link),
                              headers={"content-type": "text/html"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _source(context):
    from app.services.instagram_cascade_adapters import VenueWebsiteScrapeSource

    return VenueWebsiteScrapeSource(context.dao, client=_client(context))


# ── Given ─────────────────────────────────────────────────────────────────────
@given('a venue named "{name}"')
def step_named(context, name):
    context.venue = _Venue(name)
    context.dao = _Dao(context.venue)
    context.fetched = []
    context.page_link = None
    context.site_mode = "ok"
    context.listing = _Listing(None)
    context.paid = _PaidSearch()
    context.website_tier_enabled = True


@given('the venue\'s listed website is "{website}"')
def step_listed_website(context, website):
    context.dao.website_uri = website


@given('that page links "{link}"')
def step_page_links(context, link):
    context.page_link = link


@given("that page links nothing")
def step_page_links_nothing(context):
    context.page_link = None


@given("that website times out")
def step_site_times_out(context):
    context.site_mode = "timeout"


@given("that website returns a body larger than the cap")
def step_site_huge(context):
    context.site_mode = "huge"


@given('the venue\'s Google listing already links "{website}"')
def step_listing_has(context, website):
    context.listing = _Listing(website)


@given("the website tier is turned off for this run")
def step_tier_off(context):
    context.website_tier_enabled = False


# ── When ──────────────────────────────────────────────────────────────────────
@when("the website tier looks for a handle")
def step_tier_lookup(context):
    context.raised = None
    try:
        context.website_result = asyncio.run(
            _source(context).website_for("ven_1", context.venue)
        )
    except Exception as e:  # a hostile site must never reach the caller
        context.raised = e
        context.website_result = None


@when("the cascade runs every free tier")
def step_cascade_free_tiers(context):
    from app.services.instagram_cascade_service import InstagramCascadeService

    service = InstagramCascadeService(
        venue_dao=context.dao,
        google_listing=context.listing,
        venue_website=_source(context),
        paid_search=context.paid,
        probe=None,
        accept_threshold=PROD_ACCEPT,
        ambiguous_low=PROD_AMBIGUOUS_LOW,
    )
    config = {"force_refresh": True}
    if not context.website_tier_enabled:
        config["tier_venue_website_enabled"] = False
    context.result = asyncio.run(service.discover("ven_1", config))


# ── Then ──────────────────────────────────────────────────────────────────────
@then('the website tier offers the handle "{handle}"')
def step_offers_handle(context, handle):
    from app.services.instagram_handle_sources import extract_handle

    assert context.website_result, "the tier returned no instagram url"
    got, reason = extract_handle(context.website_result)
    assert got == handle, f"got {got!r} ({reason!r}) from {context.website_result!r}"


@then("the website tier offers nothing")
def step_offers_nothing(context):
    result = context.website_result
    if result:
        from app.services.instagram_handle_sources import extract_handle

        handle, _ = extract_handle(result)
        assert not handle, f"offered {handle!r} from {result!r}"


@then("the venue's website is never fetched")
def step_never_fetched(context):
    assert context.fetched == [], (
        f"fetched {context.fetched} — the listing already had the handle, so the "
        "request was pure waste"
    )


@then("the lookup does not raise")
def step_no_raise(context):
    assert context.raised is None, context.raised


@then("the cascade accepts a handle from the venue website")
def step_accepts_from_website(context):
    from app.services.instagram_handle_sources import SOURCE_VENUE_WEBSITE

    assert context.result.accepted, (
        f"rejected at {context.result.confidence} (signals={context.result.signals})"
    )
    assert context.result.source == SOURCE_VENUE_WEBSITE, context.result.source


@then("the cascade accepts a handle from the Google listing")
def step_accepts_from_listing(context):
    from app.services.instagram_handle_sources import SOURCE_GOOGLE_WEBSITE

    assert context.result.accepted, context.result
    assert context.result.source == SOURCE_GOOGLE_WEBSITE, context.result.source


@then("the paid search is never called")
def step_paid_not_called(context):
    assert not context.paid.called, "the paid search billed for a handle already found free"


@then("the cascade rejects every free-tier candidate")
def step_no_accept(context):
    assert not context.result.accepted, (
        f"accepted @{context.result.handle} at {context.result.confidence} "
        f"(signals={context.result.signals})"
    )
