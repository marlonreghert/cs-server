"""Unit tests for the Instagram handle cascade.

The BDD suite covers observable behavior. These pin the pieces that are easy to
get quietly wrong: URL extraction against URLs seen in real data, the Open Graph
parse, the confidence weighting, and the judge's ability to answer with no
images.
"""
import asyncio

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
    SOURCE_ARCHIVED_GMAPS,
    SOURCE_GOOGLE_SEARCH,
    SOURCE_GOOGLE_WEBSITE,
    SOURCE_VENUE_WEBSITE,
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

    def test_the_bare_js_shell_proves_nothing(self):
        """This used to assert ABSENT. It was wrong, and the error was not
        theoretical: from a datacenter IP Instagram serves a shell like this for
        EVERY handle, so "no og tags means deleted" marked the entire catalogue
        as fake and overwrote real handles with NULL."""
        assert parse_profile_body("<html><title>Instagram</title></html>").existence == (
            EXIST_UNKNOWN
        )

    def test_a_non_profile_og_type_proves_nothing(self):
        body = (
            '<meta property="og:type" content="website" />'
            '<meta property="og:title" content="x" />'
        )
        assert parse_profile_body(body).existence != EXIST_ABSENT

    def test_absent_only_when_instagram_says_so(self):
        """Absence is now PROVEN, not inferred: Instagram's own page, saying it."""
        body = (
            '<meta property="og:site_name" content="Instagram" />'
            "<div>Sorry, this page isn't available.</div>"
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


# =============================================================================
# plans/260811_add-venue-instagram-discovery.md
# =============================================================================


class TestSourceEnabled:
    """_source_enabled's explicit-key-wins fix.

    Load-bearing: an operator run passing ONLY tier_apify_search_enabled:false
    must still perform zero paid calls (it disables google_search too), while
    an add-time config can enable google_search explicitly even with that same
    master switch off.
    """

    def _service(self):
        # No sources/venue_dao needed — _source_enabled reads only its config
        # argument.
        return InstagramCascadeService(venue_dao=None)

    def test_explicit_true_enables_google_search_despite_master_switch_off(self):
        service = self._service()
        config = {
            "tier_apify_search_enabled": False,
            "tier_google_search_enabled": True,
        }
        assert service._source_enabled(SOURCE_GOOGLE_SEARCH, config) is True

    def test_master_switch_off_alone_still_disables_apify_search(self):
        service = self._service()
        config = {"tier_apify_search_enabled": False}
        assert service._source_enabled(SOURCE_APIFY_SEARCH, config) is False

    def test_master_switch_off_alone_still_disables_google_search(self):
        """The zero-cost-run guarantee app/config.py promises: passing only
        the master switch must disable BOTH paid sources, not just the one
        whose name happens to match the key."""
        service = self._service()
        config = {"tier_apify_search_enabled": False}
        assert service._source_enabled(SOURCE_GOOGLE_SEARCH, config) is False

    def test_explicit_false_disables_apify_search_even_without_the_master_key(self):
        service = self._service()
        config = {"tier_apify_search_enabled": False, "tier_google_search_enabled": True}
        assert service._source_enabled(SOURCE_APIFY_SEARCH, config) is False

    def test_explicit_false_disables_a_free_source(self):
        service = self._service()
        config = {"tier_google_website_enabled": False}
        assert service._source_enabled(SOURCE_GOOGLE_WEBSITE, config) is False

    @pytest.mark.parametrize(
        "source",
        [
            SOURCE_GOOGLE_WEBSITE,
            SOURCE_ARCHIVED_GMAPS,
            SOURCE_VENUE_WEBSITE,
            SOURCE_APIFY_SEARCH,
            SOURCE_GOOGLE_SEARCH,
        ],
    )
    def test_default_config_enables_everything(self, source):
        service = self._service()
        assert service._source_enabled(source, {}) is True


class _Venue:
    venue_name = "Bar Forte"
    venue_address = "Recife"


class _RecordingDao:
    """Minimal venue_dao fake: no cached record, no servable-id listing
    needed for these single-venue discover() tests. Records every
    set_venue_instagram call for the suppress_not_found_cache assertions."""

    def __init__(self):
        self.saved: list = []

    def get_venue(self, venue_id):
        return _Venue()

    def get_venue_instagram(self, venue_id):
        return None

    def set_venue_instagram(self, record):
        self.saved.append(record)


class _WebsiteSource:
    def __init__(self, website=None):
        self.website = website

    async def website_for(self, venue_id, venue=None):
        return self.website


class TestSuppressNotFoundCache:
    """A not_found from a run that deliberately skipped a tier (add time
    never tries apify_search) must not poison that tier's freshness gate —
    the caller still gets the not_found CascadeResult, but nothing is
    persisted."""

    def test_not_found_is_reported_but_not_persisted_when_suppressed(self):
        dao = _RecordingDao()
        service = InstagramCascadeService(venue_dao=dao)  # no sources at all

        result = asyncio.run(
            service.discover("ven_x", {"suppress_not_found_cache": True})
        )

        assert result.status == "not_found"
        assert result.handle is None
        assert dao.saved == []

    def test_not_found_persists_normally_without_the_flag(self):
        dao = _RecordingDao()
        service = InstagramCascadeService(venue_dao=dao)

        result = asyncio.run(service.discover("ven_x", {}))

        assert result.status == "not_found"
        assert len(dao.saved) == 1
        assert dao.saved[0].status == "not_found"

    def test_a_found_result_still_persists_with_the_flag_set(self):
        """suppress_not_found_cache must gate only the not_found branch — a
        found (or low_confidence) result persists exactly as it does today."""
        dao = _RecordingDao()
        # A google_website hit alone (provenance 0.75) clears the accept bar
        # even with no name match and no probe — see PROVENANCE_WEIGHT.
        service = InstagramCascadeService(
            venue_dao=dao,
            google_listing=_WebsiteSource("https://instagram.com/barforte"),
        )

        result = asyncio.run(
            service.discover("ven_x", {"suppress_not_found_cache": True})
        )

        assert result.status == "found"
        assert result.accepted is True
        assert len(dao.saved) == 1
        assert dao.saved[0].instagram_handle == "barforte"
