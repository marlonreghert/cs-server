"""Scoring when the existence check cannot run.

The production numbers are the test cases. Deployed config sets the accept
threshold to 0.8; tier 1 scores 0.75 provenance and Instagram blocks the
datacenter IP, so the +0.15 existence bonus is permanently unreachable. Tier 1 —
the strongest free evidence the platform has, the Instagram URL the venue itself
publishes on its own Google listing — could therefore never accept a single
venue. These tests pin both halves of the fix.
"""
import pytest

from app.api.instagram_profile_probe import (
    EXIST_ABSENT,
    EXIST_BLOCKED,
    EXIST_PRESENT,
    EXIST_UNKNOWN,
    ProfileProbeResult,
)
from app.services.instagram_cascade_service import (
    EXISTENCE_BONUS,
    InstagramCascadeService,
    handle_as_words,
    name_similarity,
)

PROD_ACCEPT = 0.8


def _service(accept=PROD_ACCEPT):
    return InstagramCascadeService(venue_dao=object(), accept_threshold=accept)


class TestHandleFallback:
    def test_reads_dots_and_underscores_as_word_breaks(self):
        assert handle_as_words("bar.do_cuscuz") == "bar do cuscuz"

    def test_no_handle_is_none(self):
        assert handle_as_words(None) is None
        assert handle_as_words("") is None

    @pytest.mark.parametrize("venue,handle,floor", [
        ("Bar do Cuscuz", "bardocuscuzrecife", 0.7),
        ("Entre Amigos O Bode", "entreamigosobode", 0.9),
        ("Teatro Jorge Amado", "teatrojorgeamado", 0.9),
        ("Villa Neukölln", "villaneukoelln", 0.8),
        ("Pizzaria Atlântico Graças", "pizzariaatlantico", 0.7),
    ])
    def test_real_venue_handle_pairs_score_high(self, venue, handle, floor):
        assert name_similarity(venue, None, handle) > floor

    def test_display_name_wins_when_both_exist(self):
        """The handle is a fallback, not a replacement."""
        with_display = name_similarity("Bar do Cuscuz", "Bar do Cuscuz", "somethingelse")
        assert with_display == 1.0

    def test_without_a_handle_it_still_scores_zero(self):
        assert name_similarity("Bar do Cuscuz", None, None) == 0.0


class TestEffectiveThreshold:
    @pytest.mark.parametrize("existence", [EXIST_BLOCKED, EXIST_UNKNOWN])
    def test_bar_drops_by_the_bonus_when_existence_is_unanswerable(self, existence):
        s = _service()
        bar = s._threshold_for(ProfileProbeResult(existence=existence))
        assert bar == pytest.approx(PROD_ACCEPT - EXISTENCE_BONUS)

    def test_bar_drops_when_there_is_no_probe_at_all(self):
        assert _service()._threshold_for(None) == pytest.approx(PROD_ACCEPT - EXISTENCE_BONUS)

    @pytest.mark.parametrize("existence", [EXIST_PRESENT, EXIST_ABSENT])
    def test_bar_is_untouched_when_the_check_answered(self, existence):
        s = _service()
        assert s._threshold_for(ProfileProbeResult(existence=existence)) == PROD_ACCEPT

    def test_the_bar_is_never_raised(self):
        s = _service()
        for existence in (EXIST_PRESENT, EXIST_ABSENT, EXIST_UNKNOWN, EXIST_BLOCKED):
            assert s._threshold_for(ProfileProbeResult(existence=existence)) <= PROD_ACCEPT

    def test_a_tiny_threshold_never_goes_negative(self):
        assert _service(accept=0.05)._threshold_for(None) == 0.0


class TestTheProductionCase:
    """Bar do Cuscuz: resolved from its own Google listing, rejected anyway."""

    class _Venue:
        venue_name = "Bar do Cuscuz"

    def test_tier_one_used_to_land_exactly_below_the_bar(self):
        """The gap this feature closes: 0.750 measured against 0.800."""
        s = _service()
        conf, _ = s._score("google_website", ProfileProbeResult(existence=EXIST_BLOCKED),
                           self._Venue(), None, None)
        assert conf == pytest.approx(0.75)
        assert conf < PROD_ACCEPT

    def test_with_the_handle_compared_it_clears_the_bar(self):
        s = _service()
        conf, signals = s._score("google_website", ProfileProbeResult(existence=EXIST_BLOCKED),
                                 self._Venue(), None, "bardocuscuzrecife")
        assert conf >= signals["effective_threshold"]

    def test_the_record_explains_itself(self):
        s = _service()
        _, signals = s._score("google_website", ProfileProbeResult(existence=EXIST_BLOCKED),
                              self._Venue(), None, "bardocuscuzrecife")
        assert signals["existence_checked"] is False
        assert signals["effective_threshold"] == pytest.approx(0.65)

    def test_an_unverified_paid_candidate_stays_below_the_bar(self):
        """Lowering the bar must not let a scraped guess through."""
        s = _service()
        conf, signals = s._score("apify_search", ProfileProbeResult(existence=EXIST_BLOCKED),
                                 self._Venue(), None, "algumbarqualquer")
        assert conf < signals["effective_threshold"]
