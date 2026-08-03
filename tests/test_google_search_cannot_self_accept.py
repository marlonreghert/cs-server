"""A Google-search candidate can never be accepted on its own.

This is the safety property behind the whole tier, and it is arithmetic rather
than a tuned threshold:

    provenance(google_search) + NAME_WEIGHT * similarity
      = 0.20 + 0.40 * 1.0
      = 0.60   <   0.65   (the production bar: 0.8 accept minus the existence
                           bonus, which cannot be collected while Instagram
                           blocks the datacenter IP)

So no name similarity, however perfect, lets a search result name a venue's
account. The judge is the only path to acceptance.

It matters because a search result is a guess. Two of the first five real results
were plausible and wrong-looking: `@saopedrorestaurante` for Patio de Sao Pedro,
a public square, and `@mosteirosaobentolinda` — Olinda, a different city.
"""
import asyncio

import pytest

from app.services.instagram_cascade_service import (
    EXISTENCE_BONUS,
    NAME_WEIGHT,
    PROVENANCE_WEIGHT,
    InstagramCascadeService,
)
from app.services.instagram_handle_sources import (
    PAID_SOURCES,
    SOURCE_APIFY_SEARCH,
    SOURCE_GOOGLE_SEARCH,
    SOURCE_ORDER,
    SOURCE_VENUE_WEBSITE,
)

PROD_ACCEPT = 0.8
BAR = PROD_ACCEPT - EXISTENCE_BONUS  # 0.65


class TestTheArithmeticGuarantee:
    @pytest.mark.parametrize("similarity", [i / 20 for i in range(21)])
    def test_no_similarity_reaches_the_bar(self, similarity):
        """0.0 through 1.0 in steps of 0.05 — none of them clear it."""
        confidence = min(
            PROVENANCE_WEIGHT[SOURCE_GOOGLE_SEARCH] + NAME_WEIGHT * similarity, 1.0
        )
        assert confidence < BAR

    def test_even_a_perfect_name_match_falls_short(self):
        ceiling = PROVENANCE_WEIGHT[SOURCE_GOOGLE_SEARCH] + NAME_WEIGHT
        assert ceiling == pytest.approx(0.60)
        assert ceiling < BAR

    def test_the_margin_is_recorded_so_drift_is_caught(self):
        """If someone raises provenance past this, the guarantee silently dies."""
        ceiling = PROVENANCE_WEIGHT[SOURCE_GOOGLE_SEARCH] + NAME_WEIGHT
        # 1e-9 tolerance: the margin is exactly 0.05 by design and binary floats
        # render it as 0.04999999999999993.
        assert BAR - ceiling >= 0.05 - 1e-9, (
            f"only {BAR - ceiling:.3f} of headroom left — a search result is "
            "close to being able to name a venue's account by itself"
        )

    def test_it_is_no_stronger_than_a_scraped_search_result(self):
        assert (PROVENANCE_WEIGHT[SOURCE_GOOGLE_SEARCH]
                <= PROVENANCE_WEIGHT[SOURCE_APIFY_SEARCH])

    def test_it_is_weaker_than_the_venues_own_website(self):
        assert (PROVENANCE_WEIGHT[SOURCE_GOOGLE_SEARCH]
                < PROVENANCE_WEIGHT[SOURCE_VENUE_WEBSITE])


class TestItRunsLastAndCosts:
    def test_it_runs_after_every_free_tier(self):
        idx = SOURCE_ORDER.index(SOURCE_GOOGLE_SEARCH)
        for free in (SOURCE_VENUE_WEBSITE,):
            assert SOURCE_ORDER.index(free) < idx

    def test_it_is_marked_paid(self):
        """So the existing tier toggle and the paid-call accounting cover it."""
        assert SOURCE_GOOGLE_SEARCH in PAID_SOURCES


class _Venue:
    venue_name = "Gildo Lanches"
    neighborhood = "Boa Viagem"
    venue_address = "Boa Viagem, Recife"


class _Dao:
    def __init__(self):
        self.saved = None

    def list_servable_venue_ids(self):
        return ["ven_1"]

    def get_venue(self, venue_id):
        return _Venue()

    def get_venue_instagram(self, venue_id):
        return None

    def set_venue_instagram(self, record):
        self.saved = record


class _Search:
    def __init__(self, handle="gildolanchespe"):
        self.handle = handle
        self.calls = 0

    async def website_for(self, venue_id, venue=None):
        self.calls += 1
        return f"https://www.instagram.com/{self.handle}/"


class _Judge:
    def __init__(self, is_match):
        self.is_match = is_match

    async def judge(self, *, venue, candidate, profile, venue_photos):
        from app.services.instagram_judge import MODE_TEXT_ONLY, JudgeVerdict

        return JudgeVerdict(mode=MODE_TEXT_ONLY, is_match=self.is_match,
                            confidence=0.95 if self.is_match else 0.1, reason="t")


def _discover(judge):
    service = InstagramCascadeService(
        venue_dao=_Dao(),
        google_search=_Search(),
        judge=judge,
        accept_threshold=PROD_ACCEPT,
        ambiguous_low=0.5,
    )
    return asyncio.run(service.discover("ven_1", {"force_refresh": True}))


class TestEndToEnd:
    def test_a_perfect_name_match_is_still_rejected_without_a_judge(self):
        """The handle IS the venue name here, and it still must not be accepted."""
        result = _discover(judge=None)
        assert not result.accepted
        assert result.source == SOURCE_GOOGLE_SEARCH

    def test_the_judge_can_accept_it(self):
        result = _discover(judge=_Judge(is_match=True))
        assert result.accepted
        assert result.source == SOURCE_GOOGLE_SEARCH

    def test_the_judge_can_reject_it(self):
        result = _discover(judge=_Judge(is_match=False))
        assert not result.accepted
