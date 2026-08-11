"""The Google-search builder must RUN, on every branch.

Written this way deliberately: the last container builder I added referenced a
bare `settings` that only exists as a parameter of `__init__`, imported cleanly,
and crash-looped production 55 times on startup. Importing proves nothing; the
function body has to execute.

Design (plans/260811_instagram-discovery-admin-flags.md): the builder now
constructs on CREDENTIAL presence alone (APIFY_API_TOKEN) — the settings flag
(``instagram_google_search_enabled``) no longer gates existence, only USE at
request time (AddVenueHandler resolves the live enable/disable per add from
admin config, falling back to this setting only when the key is absent). A
runtime admin-panel enable must have something to enable, hence dropping the
flag from the construction condition. A flag left on with no token still
returns None (nothing to build) and now ALSO logs a startup warning, since
that is the one silent-failure state this design introduces: the panel says
"on" and nothing happens.
"""
import logging

import pytest

from app.config import Settings
from app.container import Container


def _container(**overrides):
    instance = Container.__new__(Container)
    instance.settings = Settings(**overrides)
    instance.pipeline_repository = object()
    return instance


class TestTheBuilderExecutes:
    def test_no_token_returns_none_with_the_flag_off(self):
        c = _container(instagram_google_search_enabled=False, apify_api_token="")
        assert c._build_google_search_source() is None

    def test_no_token_returns_none_with_the_flag_on(self):
        c = _container(instagram_google_search_enabled=True, apify_api_token="")
        assert c._build_google_search_source() is None

    def test_token_present_builds_the_source_with_the_flag_off(self):
        """The whole point of the change: a runtime admin-config enable must
        have something to enable, so credential presence alone builds it —
        even with the deploy-time flag off."""
        c = _container(instagram_google_search_enabled=False, apify_api_token="apify_x")
        source = c._build_google_search_source()
        assert source is not None
        assert hasattr(source, "website_for")

    def test_token_present_builds_the_source_with_the_flag_on(self):
        c = _container(instagram_google_search_enabled=True, apify_api_token="apify_x")
        source = c._build_google_search_source()
        assert source is not None
        assert hasattr(source, "website_for")

    def test_it_passes_the_configured_actor_and_country(self):
        c = _container(
            apify_api_token="apify_x",
            instagram_google_search_actor="apify~custom",
            instagram_google_search_country="pt",
        )
        client = c._build_google_search_source().search_client
        assert client.actor == "apify~custom"
        assert client.country_code == "pt"

    @pytest.mark.parametrize("enabled,token", [
        (False, ""), (False, "apify_x"), (True, ""), (True, "apify_x"),
    ])
    def test_no_branch_raises(self, enabled, token):
        c = _container(instagram_google_search_enabled=enabled, apify_api_token=token)
        c._build_google_search_source()


class TestFlagOnWithoutCredentialWarnsAtStartup:
    """The one silent-failure state the credential-only gate introduces: the
    admin panel can say "on" while nothing happens if the deploy-time
    credential was never configured."""

    def test_logs_a_warning(self, caplog):
        c = _container(instagram_google_search_enabled=True, apify_api_token="")
        with caplog.at_level(logging.WARNING):
            result = c._build_google_search_source()
        assert result is None
        assert any(
            "google" in r.message.lower() and "token" in r.message.lower()
            for r in caplog.records
        ), caplog.text

    def test_no_warning_when_the_flag_is_off(self, caplog):
        c = _container(instagram_google_search_enabled=False, apify_api_token="")
        with caplog.at_level(logging.WARNING):
            c._build_google_search_source()
        assert caplog.records == []

    def test_no_warning_when_the_token_is_present(self, caplog):
        c = _container(instagram_google_search_enabled=True, apify_api_token="apify_x")
        with caplog.at_level(logging.WARNING):
            c._build_google_search_source()
        assert caplog.records == []


class TestItIsOffByDefault:
    def test_default_is_disabled(self):
        """It bills per venue; enabling the deploy-time fallback flag must
        still be a deliberate act, even though the builder itself now runs on
        credential presence alone."""
        assert Settings().instagram_google_search_enabled is False
