"""Parsing of Apify Instagram search results.

The bug these guard against was not a crash — it was SILENCE. Apify changed
`externalUrls` entries from a bare string to an object; every profile carrying a
link then failed `InstagramProfile` validation, was logged at WARNING, and
dropped. The search returned `[]`, which is exactly what "no matches" looks like,
so the pipeline appeared merely unlucky while it was in fact totally broken.

So: the object shape must parse, the old string shape must keep parsing, an
unreadable link must not take the profile with it, and anything dropped must be
counted.
"""
import asyncio

import pytest
from prometheus_client import REGISTRY

from app.api.apify_instagram_client import ApifyInstagramClient, _external_url

# Verbatim from the live actor response that broke the pipeline.
LYNX = "https://l.instagram.com/?u=https%3A%2F%2Flinktr.ee%2Fvenue&e=AT0"
REAL_OBJECT_ENTRY = {"title": "", "lynx_url": LYNX, "link_type": "external"}


def _item(**over):
    base = {
        "username": "venue_handle",
        "fullName": "Venue Handle",
        "biography": "bar and restaurant",
        "followersCount": 1200,
        "followsCount": 30,
        "isBusinessAccount": True,
        "businessCategoryName": "Bar",
        "verified": False,
    }
    base.update(over)
    return base


def _search(items):
    client = ApifyInstagramClient(api_token="test-token")

    async def _fake(*args, **kwargs):
        return items

    client._run_actor_sync = _fake
    return asyncio.run(client.search_users("some venue"))


def _dropped(reason):
    for metric in REGISTRY.collect():
        if metric.name != "instagram_search_candidates_dropped":
            continue
        for s in metric.samples:
            if s.name.endswith("_total") and s.labels.get("reason") == reason:
                return s.value
    return 0.0


class TestExternalUrlShapes:
    def test_reads_the_object_shape_apify_returns_today(self):
        assert _external_url(REAL_OBJECT_ENTRY) == LYNX

    def test_still_reads_the_old_string_shape(self):
        assert _external_url("https://venue.example.com") == "https://venue.example.com"

    def test_unreadable_object_yields_none_rather_than_raising(self):
        assert _external_url({"title": "", "link_type": "external"}) is None

    @pytest.mark.parametrize("entry", [None, "", [], 42, {}])
    def test_anything_else_yields_none(self, entry):
        assert _external_url(entry) is None

    def test_prefers_lynx_url_over_other_keys(self):
        entry = {"url": "https://other.example", "lynx_url": LYNX}
        assert _external_url(entry) == LYNX


class TestSearchResultsSurvive:
    def test_object_linked_profile_is_kept(self):
        """The exact regression: this profile used to vanish entirely."""
        results = _search([_item(externalUrls=[dict(REAL_OBJECT_ENTRY)])])
        assert [p.username for p in results] == ["venue_handle"]
        assert results[0].external_url == LYNX

    def test_string_linked_profile_is_kept(self):
        results = _search([_item(externalUrls=["https://venue.example.com"])])
        assert results[0].external_url == "https://venue.example.com"

    def test_unlinked_profile_is_kept(self):
        results = _search([_item()])
        assert results[0].external_url is None

    def test_an_unreadable_link_does_not_discard_the_profile(self):
        results = _search([_item(externalUrls=[{"title": "x"}])])
        assert len(results) == 1
        assert results[0].external_url is None

    def test_one_bad_result_does_not_take_the_good_ones_with_it(self):
        results = _search([
            _item(username="", externalUrls=[dict(REAL_OBJECT_ENTRY)]),
            _item(username="good_one", externalUrls=[dict(REAL_OBJECT_ENTRY)]),
        ])
        assert [p.username for p in results] == ["good_one"]


class TestDropsAreCounted:
    def test_missing_username_is_counted(self):
        before = _dropped("no_username")
        _search([_item(username="")])
        assert _dropped("no_username") == before + 1

    def test_error_items_are_counted(self):
        before = _dropped("error_item")
        _search([{"error": "no_items"}])
        assert _dropped("error_item") == before + 1

    def test_a_validation_failure_is_counted_not_just_logged(self):
        before = _dropped("parse_error")
        _search([_item(followersCount=["not", "a", "number"])])
        assert _dropped("parse_error") == before + 1, (
            "an unparseable result must be counted — the whole reason this "
            "outage was invisible is that nothing counted the drops"
        )
