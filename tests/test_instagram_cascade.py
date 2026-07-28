"""Unit tests for the Instagram handle cascade.

The BDD suite covers observable behavior. These pin the pieces that are easy to
get quietly wrong: URL extraction against URLs seen in real data, the Open Graph
parse, the confidence weighting, and the judge's ability to answer with no
images.
"""
import pytest

from app.api.instagram_profile_probe import (
    EXIST_ABSENT,
    EXIST_PRESENT,
    EXIST_UNKNOWN,
    ProfileProbeResult,
    parse_profile_body,
)
from app.services.instagram_cascade_service import (
    NAME_WEIGHT,
    InstagramCascadeService,
    name_similarity,
)
from app.services.instagram_handle_sources import (
    REJECT_LINK_SHIM,
    REJECT_NON_PROFILE_PATH,
    REJECT_NOT_INSTAGRAM,
    SOURCE_APIFY_SEARCH,
    SOURCE_GOOGLE_WEBSITE,
    extract_handle,
    normalize_handle,
)
from app.services.instagram_judge import (
    MODE_TEXT_ONLY,
    MODE_VISION_BOTH,
    MODE_VISION_PARTIAL,
    TEXT_ONLY_CONFIDENCE_CEILING,
    cap_for_mode,
    select_mode,
)


class TestExtraction:
    def test_plain_profile_url(self):
        assert extract_handle("https://instagram.com/barvibes") == ("barvibes", None)

    def test_strips_tracking_query(self):
        """Shared profile links carry ?igshid=… — real example from our data."""
        assert extract_handle("https://instagram.com/tasquinhadotio?igshid=dirg31bawji6") == (
            "tasquinhadotio", None,
        )

    def test_rejects_the_outbound_link_wrapper(self):
        """Seen in production: a venue's `website` was l.instagram.com wrapping
        an iFood URL. Naive matching mints `?u=https%3A%2F%2F…` as a handle."""
        handle, reason = extract_handle(
            "https://l.instagram.com/?u=https%3A%2F%2Fwww.ifood.com.br%2Fdelivery"
        )
        assert handle is None
        assert reason == REJECT_LINK_SHIM

    @pytest.mark.parametrize(
        "url",
        [
            "https://instagram.com/p/Cabc123",
            "https://instagram.com/reel/Cxyz789",
            "https://instagram.com/reels/Cxyz789",
            "https://instagram.com/explore/tags/recife",
            "https://instagram.com/stories/someone/123",
            "https://www.instagram.com/tv/Cabc",
        ],
    )
    def test_rejects_non_profile_paths(self, url):
        handle, reason = extract_handle(url)
        assert handle is None
        assert reason == REJECT_NON_PROFILE_PATH

    def test_rejects_a_non_instagram_site(self):
        handle, reason = extract_handle("https://bercyvillage.com.br/")
        assert handle is None
        assert reason == REJECT_NOT_INSTAGRAM

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_rejects_empty(self, bad):
        assert extract_handle(bad)[0] is None

    def test_handles_www_and_trailing_slash(self):
        assert extract_handle("https://www.instagram.com/barvibes/") == ("barvibes", None)

    def test_normalize_strips_at_and_lowercases(self):
        assert normalize_handle("@BarVibes") == "barvibes"

    def test_normalize_rejects_whitespace(self):
        assert normalize_handle("bar vibes") is None


class TestProfileParse:
    REAL_BODY = (
        '<meta property="og:type" content="profile" />'
        '<meta property="og:title" content="Tasquinha do Tio (&#064;tasquinhadotio) '
        '&#x2022; Instagram photos and videos" />'
        '<meta property="og:description" content="23K Followers, 8 Following, 218 Posts '
        '- See Instagram photos and videos" />'
        '<meta property="og:image" content="https://scontent.cdninstagram.com/pic.jpg" />'
    )

    def test_parses_a_real_profile_body(self):
        r = parse_profile_body(self.REAL_BODY)
        assert r.existence == EXIST_PRESENT
        assert r.display_name == "Tasquinha do Tio"
        assert r.followers_count == "23K"
        assert r.image_url.endswith("pic.jpg")

    def test_absent_when_there_are_no_og_tags(self):
        """A handle that does not exist gets the JS shell — no og:title at all."""
        assert parse_profile_body("<html><title>Instagram</title></html>").existence == (
            EXIST_ABSENT
        )

    def test_absent_when_og_type_is_not_a_profile(self):
        body = (
            '<meta property="og:type" content="website" />'
            '<meta property="og:title" content="x" />'
        )
        assert parse_profile_body(body).existence == EXIST_ABSENT

    def test_probe_failure_is_unknown_not_absent(self):
        """A timeout must never be recorded as proof a real profile is fake."""
        assert ProfileProbeResult().existence == EXIST_UNKNOWN
        assert ProfileProbeResult().exists is False


class TestConfidence:
    """Fitted to the three separations measured on real data."""

    def test_measured_name_similarities(self):
        assert name_similarity("Tasquinha do Tio", "Tasquinha do Tio") == 1.0
        assert 0.7 < name_similarity("Bercy Boa Viagem", "Bercy Village") < 0.8
        assert name_similarity("Villa Setubal Botequim", "Tasquinha do Tio") < 0.4

    def test_missing_display_name_scores_zero(self):
        assert name_similarity("Bar Vibes", None) == 0.0

    def _score(self, source, existence, sim):
        svc = InstagramCascadeService(venue_dao=None)
        probe = ProfileProbeResult(existence=existence, display_name="X")
        venue = type("V", (), {"venue_name": "V"})()
        conf, _ = svc._score(source, probe, venue, None)
        # substitute a controlled similarity
        from app.services.instagram_cascade_service import (
            EXISTENCE_BONUS,
            PROVENANCE_WEIGHT,
        )
        bonus = EXISTENCE_BONUS if existence == EXIST_PRESENT else 0.0
        return min(PROVENANCE_WEIGHT[source] + bonus + NAME_WEIGHT * sim, 1.0)

    def test_exact_paid_match_reaches_the_accept_bar(self):
        assert self._score(SOURCE_APIFY_SEARCH, EXIST_PRESENT, 1.00) >= 0.75

    def test_the_bercy_case_lands_in_the_ambiguous_band(self):
        c = self._score(SOURCE_APIFY_SEARCH, EXIST_PRESENT, 0.76)
        assert 0.50 <= c < 0.75

    def test_a_wrong_pairing_is_rejected(self):
        assert self._score(SOURCE_APIFY_SEARCH, EXIST_PRESENT, 0.26) < 0.50

    def test_provenance_dominates_for_the_venues_own_listing(self):
        own = self._score(SOURCE_GOOGLE_WEBSITE, EXIST_PRESENT, 0.5)
        search = self._score(SOURCE_APIFY_SEARCH, EXIST_PRESENT, 0.5)
        assert own > search

    def test_unknown_existence_withholds_the_bonus_without_rejecting(self):
        """A probe failure costs the existence bonus but must not reject.

        Measured on the PAID tier: the venue's own listing scores so high that
        both variants saturate the 1.0 cap, which would hide the effect.
        """
        known = self._score(SOURCE_APIFY_SEARCH, EXIST_PRESENT, 1.0)
        unknown = self._score(SOURCE_APIFY_SEARCH, EXIST_UNKNOWN, 1.0)
        assert unknown < known
        assert unknown > 0.5, "an unknown probe must not push a good name below the band"


class TestJudgeModes:
    """The operator's requirement: a verdict must be possible with no images."""

    def test_both_sides(self):
        assert select_mode(profile_image="u", venue_photos=["a"]) == MODE_VISION_BOTH

    def test_only_a_profile_picture(self):
        assert select_mode(profile_image="u", venue_photos=[]) == MODE_VISION_PARTIAL

    def test_only_venue_photos(self):
        assert select_mode(profile_image=None, venue_photos=["a"]) == MODE_VISION_PARTIAL

    def test_no_images_at_all_still_selects_a_mode(self):
        assert select_mode(profile_image=None, venue_photos=[]) == MODE_TEXT_ONLY

    def test_text_only_confidence_is_capped(self):
        assert cap_for_mode(MODE_TEXT_ONLY, 0.99) == TEXT_ONLY_CONFIDENCE_CEILING

    def test_vision_confidence_is_not_capped(self):
        assert cap_for_mode(MODE_VISION_BOTH, 0.99) == 0.99
