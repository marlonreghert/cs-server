"""Tests for the per-run `scrape_image_authors` toggle.

Why this option exists, measured on Bar do Cuscuz (1,941 images) against the
real actor:

    scrapeImageAuthors=true   1,729s
    scrapeImageAuthors=false    135s

12.8x, for the same images. The actor looks up an author PER IMAGE, which
dominates the run on photo-heavy venues and pushes them past the poll budget
entirely — so the choice is attribution, or those venues at all.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.archive_sources import _as_bool, get_source


class _CapturingClient:
    """Records the kwargs the descriptor passes down."""

    def __init__(self):
        self.calls: list[dict] = []

    async def fetch_venue_photos(self, search_query, **kwargs):
        self.calls.append(dict(search_query=search_query, **kwargs))
        return {"photos": [], "info": {}}


def _fetch(source_config):
    client = _CapturingClient()
    cfg = {"max_photos_per_venue": 50, "source_config": source_config}
    venue = {"search_query": "Bar do Cuscuz, Recife"}
    asyncio.run(get_source("apify_gmaps_extractor").fetch(client, venue, cfg))
    return client.calls[0]


class TestToggleReachesTheClient:
    def test_defaults_to_on_when_unset(self):
        # Absent config must not silently change existing behavior.
        assert _fetch({})["scrape_image_authors"] is True

    def test_yes_turns_it_on(self):
        assert _fetch({"scrape_image_authors": "yes"})["scrape_image_authors"] is True

    def test_no_turns_it_off(self):
        assert _fetch({"scrape_image_authors": "no"})["scrape_image_authors"] is False

    def test_a_real_bool_is_respected(self):
        assert _fetch({"scrape_image_authors": False})["scrape_image_authors"] is False


class TestBoolCoercion:
    @pytest.mark.parametrize("value", ["no", "No", "NO", "false", "False", "0", "off", ""])
    def test_falsey_admin_values(self, value):
        # The admin panel sends a `select` as a STRING, and bool("no") is True —
        # without coercion the off switch would be an on switch.
        assert _as_bool(value, default=True) is False

    @pytest.mark.parametrize("value", ["yes", "true", "1", "on"])
    def test_truthy_admin_values(self, value):
        assert _as_bool(value, default=True) is True

    def test_none_takes_the_default(self):
        assert _as_bool(None, default=True) is True
        assert _as_bool(None, default=False) is False

    def test_a_bool_passes_through_untouched(self):
        assert _as_bool(True, default=False) is True
        assert _as_bool(False, default=True) is False


class TestPublishedToTheAdminPanel:
    def test_the_field_is_offered_so_an_operator_can_reach_it(self):
        names = {f.name for f in get_source("apify_gmaps_extractor").config_schema}
        assert "scrape_image_authors" in names

    def test_it_defaults_to_yes_in_the_ui(self):
        field = next(
            f for f in get_source("apify_gmaps_extractor").config_schema
            if f.name == "scrape_image_authors"
        )
        assert field.default == "yes"
        assert set(field.options or []) == {"yes", "no"}
