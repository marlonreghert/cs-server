"""The Google-search builder must RUN, on every branch.

Written this way deliberately: the last container builder I added referenced a
bare `settings` that only exists as a parameter of `__init__`, imported cleanly,
and crash-looped production 55 times on startup. Importing proves nothing; the
function body has to execute.
"""
import pytest

from app.config import Settings
from app.container import Container


def _container(**overrides):
    instance = Container.__new__(Container)
    instance.settings = Settings(**overrides)
    instance.pipeline_repository = object()
    return instance


class TestTheBuilderExecutes:
    def test_disabled_returns_none(self):
        c = _container(instagram_google_search_enabled=False)
        assert c._build_google_search_source() is None

    def test_enabled_without_a_token_returns_none(self):
        c = _container(instagram_google_search_enabled=True, apify_api_token="")
        assert c._build_google_search_source() is None

    def test_enabled_with_a_token_builds_the_source(self):
        c = _container(instagram_google_search_enabled=True, apify_api_token="apify_x")
        source = c._build_google_search_source()
        assert source is not None
        assert hasattr(source, "website_for")

    def test_it_passes_the_configured_actor_and_country(self):
        c = _container(
            instagram_google_search_enabled=True,
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


class TestItIsOffByDefault:
    def test_default_is_disabled(self):
        """It bills per venue; enabling it must be a deliberate act."""
        assert Settings().instagram_google_search_enabled is False
