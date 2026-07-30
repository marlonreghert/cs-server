"""Behave steps for tests/bdd/enrichment/instagram-candidate-loss.feature.

Drives the REAL Apify parsing path and the REAL cascade run loop. The point of
this feature is that a foreign payload change must not silently empty the
pipeline, so the parse steps feed the exact shape observed in production rather
than a tidied-up fixture.
"""
from __future__ import annotations

import asyncio

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

# The real object shape Apify's instagram-search-scraper returns today. The old
# contract was a bare string; this is what broke every linked profile.
LYNX = "https://l.instagram.com/?u=https%3A%2F%2Fexample.com%2F&e=ATx"
OBJECT_LINK = {"title": "", "lynx_url": LYNX, "link_type": "external"}


def _get(context, name, default=None):
    """behave 1.2.6's Context.__getattr__ raises KeyError, so hasattr/getattr
    with a default both blow up. Ask the underlying frame directly."""
    try:
        return getattr(context, name)
    except (AttributeError, KeyError):
        return default


def _dropped(reason=None):
    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != "instagram_search_candidates_dropped":
            continue
        for s in metric.samples:
            if not s.name.endswith("_total"):
                continue
            if reason is None or s.labels.get("reason") == reason:
                total += s.value
    return total


def _item(**over):
    base = {
        "username": "venue_handle",
        "fullName": "Venue Handle",
        "biography": "a bar",
        "followersCount": 100,
        "followsCount": 10,
        "verified": False,
    }
    base.update(over)
    return base


# ── Background ────────────────────────────────────────────────────────────────
@given("the Instagram search returns results from Apify")
def step_search_backend(context):
    context.apify_items = []
    context.dropped_before = _dropped()


# ── Parsing: given ────────────────────────────────────────────────────────────
@given("a search result whose external link is an object with a lynx url")
def step_object_link(context):
    context.apify_items.append(_item(externalUrls=[dict(OBJECT_LINK)]))


@given("a search result whose external link is a plain string")
def step_string_link(context):
    context.expected_url = "https://venue.example.com"
    context.apify_items.append(_item(externalUrls=[context.expected_url]))


@given("a search result with no external link")
def step_no_link(context):
    context.apify_items.append(_item())


@given("a search result whose external link has no recognisable url")
def step_unrecognisable_link(context):
    context.apify_items.append(_item(externalUrls=[{"title": "", "link_type": "external"}]))


@given("a search result that cannot be parsed")
def step_unparseable(context):
    # No username is recoverable from this at all — the profile itself is junk.
    context.apify_items.append(_item(username="broken", followersCount=["not", "a", "number"]))


# ── Parsing: when ─────────────────────────────────────────────────────────────
@when("the search results are parsed")
def step_parse(context):
    from app.api.apify_instagram_client import ApifyInstagramClient

    client = ApifyInstagramClient(api_token="test-token")

    async def _fake_run_actor(*args, **kwargs):
        return context.apify_items

    client._run_actor_sync = _fake_run_actor  # type: ignore[assignment]
    context.parsed = asyncio.run(client.search_users("some venue"))


# ── Parsing: then ─────────────────────────────────────────────────────────────
@then("the candidate is kept")
def step_kept(context):
    assert context.parsed, "the candidate was discarded by parsing"


@then("the candidate carries the url from that object")
def step_url_from_object(context):
    assert context.parsed[0].external_url == LYNX, context.parsed[0].external_url


@then("the candidate carries that string as its url")
def step_url_from_string(context):
    assert context.parsed[0].external_url == context.expected_url, context.parsed[0]


@then("the candidate carries no url")
def step_no_url(context):
    assert context.parsed[0].external_url is None, context.parsed[0].external_url


@then("that candidate is not returned")
def step_not_returned(context):
    assert context.parsed == [], context.parsed


@then('a dropped candidate is counted with the reason "{reason}"')
def step_dropped_counted(context, reason):
    assert _dropped(reason) > 0, (
        f"nothing counted a dropped candidate for reason={reason} — a total "
        "candidate loss would again look like an empty search"
    )


@then("exactly {n:d} candidate is kept")
def step_exactly_n_kept(context, n):
    assert len(context.parsed) == n, context.parsed


# ── Run scoping ───────────────────────────────────────────────────────────────
class _FakeVenueDao:
    def __init__(self, ids):
        self._ids = list(ids)

    def list_servable_venue_ids(self):
        return list(self._ids)


def _ids(raw):
    return [v.strip() for v in raw.split(",") if v.strip()]


@given('the servable catalogue holds the venues "{raw}"')
def step_catalogue(context, raw):
    context.catalogue = _ids(raw)


def _run_cascade(context, config):
    from app.services.instagram_cascade_service import InstagramCascadeService

    service = InstagramCascadeService(venue_dao=_FakeVenueDao(context.catalogue))
    context.attempted = []

    async def _discover(venue_id, cfg=None):
        context.attempted.append(venue_id)

        class _Res:
            accepted = False

        return _Res()

    service.discover = _discover  # type: ignore[assignment]
    context.summary = asyncio.run(service.run(config))


@when('the cascade runs for the venue ids "{raw}"')
def step_run_scoped(context, raw):
    _run_cascade(context, {"venue_ids": raw})


@when("the cascade runs with no venue ids")
def step_run_unscoped(context):
    _run_cascade(context, {})


@when("the cascade runs for an empty venue id list")
def step_run_empty_scope(context):
    _run_cascade(context, {"venue_ids": ""})


@then('the cascade is attempted for exactly the venues "{raw}"')
def step_attempted(context, raw):
    assert context.attempted == _ids(raw), (
        f"expected the run to touch {_ids(raw)}, it touched {context.attempted}"
    )


@then("the run considered {n:d} venues")
def step_considered(context, n):
    assert context.summary.get("considered") == n, context.summary


@then("the run reports {n:d} unknown venue id")
def step_unknown(context, n):
    assert context.summary.get("unknown_venue_ids") == n, context.summary
