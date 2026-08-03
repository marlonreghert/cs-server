"""Behave steps for tests/bdd/enrichment/google-search-instagram-tier.feature.

The load-bearing scenario is "never accept without the judge". It is driven
through the REAL cascade with the REAL weights, so it proves the arithmetic
rather than a mocked decision.
"""
from __future__ import annotations

import asyncio

from behave import given, then, when  # type: ignore[import-untyped]

PROD_ACCEPT = 0.8
PROD_AMBIGUOUS_LOW = 0.5


class _Venue:
    def __init__(self, name, neighborhood):
        self.venue_name = name
        self.neighborhood = neighborhood
        self.venue_address = f"{neighborhood}, Recife"


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

    def get_vibe_attributes(self, venue_id):
        return None


class _Listing:
    def __init__(self, website=None):
        self.website = website

    async def website_for(self, venue_id, venue=None):
        return self.website


class _FakeSearch:
    """Stands in for the Apify google-search actor."""

    def __init__(self, link=None, fail=False):
        self.link = link
        self.fail = fail
        self.queries = []

    async def search(self, query, *, results=10):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("actor run failed")
        if not self.link:
            return [{"title": "Some page", "url": "https://example.com", "description": "no ig"}]
        return [{"title": "Gildo Lanches (@x) • Instagram", "url": self.link,
                 "description": f"See posts at {self.link}"}]


class _FakeJudge:
    def __init__(self, is_match=True):
        self.is_match = is_match
        self.calls = 0

    async def judge(self, *, venue, candidate, profile, venue_photos):
        from app.services.instagram_judge import JudgeVerdict, MODE_TEXT_ONLY

        self.calls += 1
        return JudgeVerdict(mode=MODE_TEXT_ONLY, is_match=self.is_match,
                            confidence=0.9 if self.is_match else 0.2, reason="test")


def _source(context):
    from app.services.instagram_cascade_adapters import GoogleSearchInstagramSource

    return GoogleSearchInstagramSource(context.dao, search_client=context.search)


# ── Given ─────────────────────────────────────────────────────────────────────
@given('a searched venue named "{name}" in "{neighborhood}"')
def step_searched_venue(context, name, neighborhood):
    context.venue = _Venue(name, neighborhood)
    context.dao = _Dao(context.venue)
    context.search = _FakeSearch()
    context.listing = _Listing(None)
    context.judge = None
    context.search_tier_enabled = True


@given("the venue has no website and no archived payload")
def step_no_web_presence(context):
    context.listing = _Listing(None)


@given('Google returns a result linking "{link}"')
def step_google_returns(context, link):
    context.search = _FakeSearch(link=link)


@given("Google returns results with no Instagram link")
def step_google_returns_nothing(context):
    context.search = _FakeSearch(link=None)


@given("the search fails")
def step_search_fails(context):
    context.search = _FakeSearch(fail=True)


@given("no judge is available to adjudicate")
def step_no_judge_available(context):
    context.judge = None


@given("the judge confirms the searched profile")
def step_judge_confirms_search(context):
    context.judge = _FakeJudge(is_match=True)


@given("the judge rejects the searched profile")
def step_judge_rejects_search(context):
    context.judge = _FakeJudge(is_match=False)


@given('the venue\'s Google listing links "{website}"')
def step_listing_links_search(context, website):
    context.listing = _Listing(website)


@given("the search tier is turned off for this run")
def step_search_tier_off(context):
    context.search_tier_enabled = False


# ── When ──────────────────────────────────────────────────────────────────────
@when("the search tier looks for a handle")
def step_search_lookup(context):
    context.raised = None
    try:
        context.search_result = asyncio.run(
            _source(context).website_for("ven_1", context.venue)
        )
    except Exception as e:
        context.raised = e
        context.search_result = None


@when("the cascade discovers the searched venue")
def step_cascade_search(context):
    from app.services.instagram_cascade_service import InstagramCascadeService

    service = InstagramCascadeService(
        venue_dao=context.dao,
        google_listing=context.listing,
        google_search=_source(context),
        probe=None,
        judge=context.judge,
        accept_threshold=PROD_ACCEPT,
        ambiguous_low=PROD_AMBIGUOUS_LOW,
    )
    config = {"force_refresh": True}
    if not context.search_tier_enabled:
        config["tier_google_search_enabled"] = False
    context.result = asyncio.run(service.discover("ven_1", config))


# ── Then ──────────────────────────────────────────────────────────────────────
@then('the search tier offers the handle "{handle}"')
def step_search_offers(context, handle):
    from app.services.instagram_handle_sources import extract_handle

    assert context.search_result, "the search tier returned nothing"
    got, reason = extract_handle(context.search_result)
    assert got == handle, f"got {got!r} ({reason!r})"


@then("the search tier offers nothing")
def step_search_offers_nothing(context):
    result = context.search_result
    if result:
        from app.services.instagram_handle_sources import extract_handle

        handle, _ = extract_handle(result)
        assert not handle, f"offered {handle!r}"


@then('the query contained "{fragment}"')
def step_query_contained(context, fragment):
    joined = " | ".join(context.search.queries)
    assert fragment.lower() in joined.lower(), f"queries were: {joined!r}"


@then("the search lookup does not raise")
def step_search_no_raise(context):
    assert context.raised is None, context.raised


@then("the cascade accepts the searched handle")
def step_accepts_searched(context):
    assert context.result.accepted, (
        f"rejected at {context.result.confidence} "
        f"(judge_mode={context.result.judge_mode}, signals={context.result.signals})"
    )


@then("the cascade does not accept the searched handle")
def step_rejects_searched(context):
    assert not context.result.accepted, (
        f"ACCEPTED @{context.result.handle} at {context.result.confidence} without a "
        "judge verdict — a search result is a guess and must never stand alone"
    )


@then("the accepted handle came from the search tier")
def step_from_search_tier(context):
    from app.services.instagram_handle_sources import SOURCE_GOOGLE_SEARCH

    assert context.result.source == SOURCE_GOOGLE_SEARCH, context.result.source


@then("Google is never searched")
def step_never_searched(context):
    assert context.search.queries == [], (
        f"searched {len(context.search.queries)} time(s) — that is billable work "
        "for a handle already in hand"
    )
