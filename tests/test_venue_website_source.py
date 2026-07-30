"""The venue-website tier: extraction, bounds, and the fitted provenance weight.

This tier fetches arbitrary third-party pages from production during a
1,400-venue run. Two things therefore matter as much as finding handles: it must
never let a hostile or dead site fail the run, and it must not mistake the web
agency's Instagram for the restaurant's.
"""
import asyncio

import httpx
import pytest

from app.services.instagram_cascade_adapters import VenueWebsiteScrapeSource
from app.services.instagram_cascade_service import (
    EXISTENCE_BONUS,
    PROVENANCE_WEIGHT,
    NAME_WEIGHT,
    name_similarity,
)
from app.services.instagram_handle_sources import SOURCE_VENUE_WEBSITE, extract_handle


class _Vibe:
    def __init__(self, website):
        self.website_uri = website


class _Dao:
    def __init__(self, website):
        self.website = website
        self.raises = False

    def get_vibe_attributes(self, venue_id):
        if self.raises:
            raise RuntimeError("db down")
        return _Vibe(self.website)


def _source(website, *, body="", status=200, fail=None, headers=None, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(str(request.url))
        if fail:
            raise fail
        return httpx.Response(status, text=body,
                              headers=headers or {"content-type": "text/html"})

    return VenueWebsiteScrapeSource(
        _Dao(website), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _look(src):
    return asyncio.run(src.website_for("ven_1", None))


FOOTER = '<footer><a href="https://www.instagram.com/bucatrattoria/">IG</a></footer>'


class TestExtraction:
    def test_finds_the_footer_link(self):
        assert _look(_source("https://buca.com.br", body=FOOTER)) == \
            "https://www.instagram.com/bucatrattoria/"

    def test_result_survives_the_shared_handle_extractor(self):
        url = _look(_source("https://buca.com.br", body=FOOTER))
        assert extract_handle(url)[0] == "bucatrattoria"

    def test_handles_a_scheme_less_website(self):
        assert _look(_source("buca.com.br", body=FOOTER)) is not None

    @pytest.mark.parametrize("link", [
        "https://l.instagram.com/?u=https%3A%2F%2Fifood.com.br",
        "https://www.instagram.com/p/CxYzAbCdEfG/",
        "https://www.instagram.com/reel/CxYz/",
        "https://www.instagram.com/explore/tags/food/",
    ])
    def test_rejects_non_profile_links(self, link):
        body = f'<a href="{link}">IG</a>'
        result = _look(_source("https://buca.com.br", body=body))
        assert result is None or not extract_handle(result)[0]

    def test_no_instagram_on_the_page(self):
        assert _look(_source("https://buca.com.br", body="<p>hello</p>")) is None


class TestItNeverCostsARequestItDoesNotNeed:
    def test_skips_the_fetch_when_the_listing_is_already_instagram(self):
        calls = []
        src = _source("https://instagram.com/bucatrattoria", body=FOOTER, calls=calls)
        assert _look(src) is None
        assert calls == [], "spent a request on instagram.com to learn nothing"

    def test_no_website_means_no_request(self):
        calls = []
        assert _look(_source(None, calls=calls)) is None
        assert calls == []


class TestNothingCanFailTheRun:
    @pytest.mark.parametrize("boom", [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("dead"),
        httpx.TooManyRedirects("loop"),
        RuntimeError("anything at all"),
    ])
    def test_transport_failures_return_none(self, boom):
        assert _look(_source("https://buca.com.br", fail=boom)) is None

    def test_a_body_over_the_cap_is_skipped(self):
        src = _source("https://buca.com.br", body="x" * 5_000_000)
        src.max_bytes = 1000
        assert _look(src) is None

    def test_non_html_is_skipped(self):
        src = _source("https://buca.com.br", body=FOOTER,
                      headers={"content-type": "application/pdf"})
        assert _look(src) is None

    def test_a_broken_dao_returns_none(self):
        src = _source("https://buca.com.br", body=FOOTER)
        src.venue_dao.raises = True
        assert _look(src) is None


class TestTheFittedWeight:
    """Every measured true positive accepted, every measured noise case rejected.

    The values are the real similarities recorded against production venues. If
    the provenance weight drifts up, the agency that builds restaurant websites
    becomes the Instagram account of every restaurant it built.
    """

    BAR = 0.8 - EXISTENCE_BONUS  # the unverifiable bar in production

    def _confidence(self, venue, handle):
        sim = name_similarity(venue, None, handle)
        return min(PROVENANCE_WEIGHT[SOURCE_VENUE_WEBSITE] + NAME_WEIGHT * sim, 1.0)

    @pytest.mark.parametrize("venue,handle", [
        ("Ponte Nova", "ponte_nova"),
        ("Buca Trattoria", "bucatrattoria"),
        ("Don Francesco Trattoria", "donfrancescotrattoria"),
        ("Portal da Picanha", "portaldapicanha"),
        ("Casa dos Frios", "casadosfrios"),
        ("Club Metrópole", "clubmetropole"),
        ("Pizzaria Atlântico Graças", "pizzariaatlantico"),
    ])
    def test_real_matches_are_accepted(self, venue, handle):
        assert self._confidence(venue, handle) >= self.BAR

    @pytest.mark.parametrize("venue,handle", [
        ("Ordinário Bar e Música", "marketingpararestaurante"),  # the site's agency
        ("The Fisherman", "smartfit"),                            # a gym franchise
        ("University Theater Paschoal", "ufcinforma"),            # unrelated
        ("Lower Deck Bar & Nightclub", "parkelanzacbe"),          # unrelated
    ])
    def test_measured_noise_is_rejected(self, venue, handle):
        assert self._confidence(venue, handle) < self.BAR

    def test_it_ranks_below_the_venues_own_google_listing(self):
        from app.services.instagram_handle_sources import SOURCE_GOOGLE_WEBSITE

        assert (PROVENANCE_WEIGHT[SOURCE_VENUE_WEBSITE]
                < PROVENANCE_WEIGHT[SOURCE_GOOGLE_WEBSITE])

    def test_it_ranks_above_a_paid_search_guess(self):
        from app.services.instagram_handle_sources import SOURCE_APIFY_SEARCH

        assert (PROVENANCE_WEIGHT[SOURCE_VENUE_WEBSITE]
                > PROVENANCE_WEIGHT[SOURCE_APIFY_SEARCH])


class TestOrdering:
    def test_it_runs_before_the_paid_search(self):
        from app.services.instagram_handle_sources import SOURCE_APIFY_SEARCH, SOURCE_ORDER

        assert SOURCE_ORDER.index(SOURCE_VENUE_WEBSITE) < SOURCE_ORDER.index(SOURCE_APIFY_SEARCH)

    def test_it_is_not_a_paid_source(self):
        from app.services.instagram_handle_sources import PAID_SOURCES

        assert SOURCE_VENUE_WEBSITE not in PAID_SOURCES
