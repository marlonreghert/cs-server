"""The probe must never invent an absence.

This is the highest-consequence classification in the pipeline. A venue that
finalizes `not_found` has its stored handle overwritten with NULL, so a probe
that wrongly reports ABSENT does not just fail — it deletes data. And the
failure is systematic, not random: from a datacenter IP Instagram serves the
login wall for EVERY handle, so a single wrong default wipes the whole catalogue.

Hence the asymmetry these tests enforce: PRESENT and ABSENT both require
positive evidence, and everything else is unknown or blocked.
"""
import pytest

from app.api.instagram_profile_probe import (
    EXIST_ABSENT,
    EXIST_BLOCKED,
    EXIST_PRESENT,
    EXIST_UNKNOWN,
    parse_profile_body,
)

PROFILE = (
    '<meta property="og:type" content="profile" />'
    '<meta property="og:title" content="Tasquinha do Tio (&#064;tasquinhadotio)'
    ' &#x2022; Instagram photos and videos" />'
    '<meta property="og:description" content="23K Followers, 8 Following, 218 Posts" />'
    '<meta property="og:image" content="https://scontent.cdninstagram.com/p.jpg" />'
)

# Captured from production: what the EC2 box gets for every handle it asks about.
LOGIN_WALL = (
    '<html><head><title>Login &#x2022; Instagram</title></head>'
    '<body><div id="loginForm">Log in to Instagram</div></body></html>'
)
LOGIN_URL = "https://www.instagram.com/accounts/login/"
PROFILE_URL = "https://www.instagram.com/tasquinhadotio/"

MISSING = (
    '<meta property="og:site_name" content="Instagram" />'
    "<div>Sorry, this page isn't available.</div>"
)


class TestPresence:
    def test_a_real_profile_is_present(self):
        assert parse_profile_body(PROFILE, final_url=PROFILE_URL).existence == EXIST_PRESENT

    def test_carries_the_display_name_and_image(self):
        r = parse_profile_body(PROFILE, final_url=PROFILE_URL)
        assert r.display_name == "Tasquinha do Tio"
        assert r.image_url.endswith("p.jpg")
        assert r.followers_count == "23K"

    def test_present_even_without_a_final_url(self):
        """og tags are proof on their own; the url only matters when they are absent."""
        assert parse_profile_body(PROFILE).existence == EXIST_PRESENT


class TestTheProductionBlock:
    def test_login_wall_body_is_blocked_not_absent(self):
        assert parse_profile_body(LOGIN_WALL, final_url=LOGIN_URL).existence == EXIST_BLOCKED

    def test_redirect_to_login_is_blocked_even_with_an_innocuous_body(self):
        assert parse_profile_body("<html></html>", final_url=LOGIN_URL).existence == EXIST_BLOCKED

    def test_challenge_page_is_blocked(self):
        body = "<html>Please wait a few minutes before you try again.</html>"
        assert parse_profile_body(body, final_url=PROFILE_URL).existence == EXIST_BLOCKED

    def test_the_exact_production_symptom_does_not_read_as_absent(self):
        """Real handle, production egress: must NOT look like a deleted account."""
        assert parse_profile_body(LOGIN_WALL, final_url=LOGIN_URL).existence != EXIST_ABSENT


class TestAbsenceRequiresProof:
    def test_instagrams_own_not_found_page_is_absent(self):
        assert parse_profile_body(MISSING, final_url=PROFILE_URL).existence == EXIST_ABSENT

    @pytest.mark.parametrize("body", [
        "",
        "   ",
        "<html><body></body></html>",
        "<div>partial fragment</div>",
        "null",
        "<html><head><title>Something Else</title></head></html>",
    ])
    def test_nothing_else_is_ever_absent(self, body):
        assert parse_profile_body(body, final_url=PROFILE_URL).existence != EXIST_ABSENT

    def test_an_empty_body_is_unknown(self):
        assert parse_profile_body("", final_url=PROFILE_URL).existence == EXIST_UNKNOWN

    def test_an_unrecognisable_page_is_unknown(self):
        body = "<html><head><title>Something Else</title></head></html>"
        assert parse_profile_body(body, final_url=PROFILE_URL).existence == EXIST_UNKNOWN

    def test_a_non_profile_og_type_is_not_absent(self):
        """og:type=website is Instagram's generic shell, not a verdict."""
        body = '<meta property="og:type" content="website" /><meta property="og:title" content="Instagram" />'
        assert parse_profile_body(body, final_url=PROFILE_URL).existence != EXIST_ABSENT


class TestBlockedScoresLikeUnknown:
    """Blocked is a separate LABEL, not a separate behaviour: it must neither
    confirm a profile nor reject one."""

    def test_blocked_is_not_present(self):
        assert parse_profile_body(LOGIN_WALL, final_url=LOGIN_URL).existence != EXIST_PRESENT

    def test_blocked_is_not_absent(self):
        assert parse_profile_body(LOGIN_WALL, final_url=LOGIN_URL).existence != EXIST_ABSENT

    def test_blocked_carries_no_profile_data(self):
        r = parse_profile_body(LOGIN_WALL, final_url=LOGIN_URL)
        assert r.display_name is None and r.image_url is None
