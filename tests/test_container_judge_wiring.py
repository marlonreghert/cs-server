"""The judge builder must run, not merely import.

This exists because of a real outage: `_build_instagram_judge` referenced the
bare name `settings`, which in `container.py` is only a PARAMETER of `__init__`.
Importing the module proved nothing — the NameError needed the method to
actually execute. cs-server crash-looped 36 times on startup in production.

So these tests EXECUTE the builder against every branch. Container.__init__ is
too heavy to construct here (Redis, RDS, S3), so the instance is built without
running __init__ and given only what the method reads.

Design (plans/260811_instagram-discovery-admin-flags.md): the builder now
constructs on CREDENTIAL presence alone (OPENAI_API_KEY) — the settings flag
(``instagram_judge_enabled``) no longer gates existence, only USE at request
time (AddVenueHandler resolves the live enable/disable per add from admin
config, falling back to this setting only when the key is absent). A runtime
admin-panel enable must have something to enable. A flag left on with no key
still returns None and now ALSO logs a startup warning.
"""
import logging

import pytest

from app.config import Settings
from app.container import Container


def _container(**overrides):
    """A Container that never ran __init__, carrying only settings."""
    instance = Container.__new__(Container)
    instance.settings = Settings(**overrides)
    return instance


class TestTheBuilderExecutes:
    def test_no_key_returns_none_with_the_flag_off(self):
        c = _container(instagram_judge_enabled=False, openai_api_key="")
        assert c._build_instagram_judge() is None

    def test_no_key_returns_none_with_the_flag_on(self):
        """Enabled but unconfigured must degrade, not explode on startup."""
        c = _container(instagram_judge_enabled=True, openai_api_key="")
        assert c._build_instagram_judge() is None

    def test_key_present_builds_a_judge_with_the_flag_off(self):
        """The whole point of the change: a runtime admin-config enable must
        have something to enable, so credential presence alone builds it —
        even with the deploy-time flag off."""
        c = _container(instagram_judge_enabled=False, openai_api_key="sk-test-key")
        judge = c._build_instagram_judge()
        assert judge is not None
        assert hasattr(judge, "judge")

    def test_key_present_builds_a_judge_with_the_flag_on(self):
        c = _container(instagram_judge_enabled=True, openai_api_key="sk-test-key")
        judge = c._build_instagram_judge()
        assert judge is not None
        assert hasattr(judge, "judge")

    def test_it_passes_the_configured_model_through(self):
        c = _container(
            openai_api_key="sk-test-key",
            instagram_judge_model="gpt-5.4-mini",
        )
        assert c._build_instagram_judge().model == "gpt-5.4-mini"

    def test_it_passes_the_configured_photo_cap_through(self):
        c = _container(
            openai_api_key="sk-test-key",
            instagram_judge_max_venue_photos=2,
        )
        assert c._build_instagram_judge().max_photos == 2

    @pytest.mark.parametrize("enabled,key", [
        (False, ""), (False, "sk-x"), (True, ""), (True, "sk-x"),
    ])
    def test_no_branch_raises(self, enabled, key):
        """The point of the test: every path RUNS."""
        c = _container(instagram_judge_enabled=enabled, openai_api_key=key)
        c._build_instagram_judge()


class TestFlagOnWithoutCredentialWarnsAtStartup:
    def test_logs_a_warning(self, caplog):
        c = _container(instagram_judge_enabled=True, openai_api_key="")
        with caplog.at_level(logging.WARNING):
            result = c._build_instagram_judge()
        assert result is None
        assert any(
            "judge" in r.message.lower() and "openai" in r.message.lower()
            for r in caplog.records
        ), caplog.text

    def test_no_warning_when_the_flag_is_off(self, caplog):
        c = _container(instagram_judge_enabled=False, openai_api_key="")
        with caplog.at_level(logging.WARNING):
            c._build_instagram_judge()
        assert caplog.records == []

    def test_no_warning_when_the_key_is_present(self, caplog):
        c = _container(instagram_judge_enabled=True, openai_api_key="sk-test-key")
        with caplog.at_level(logging.WARNING):
            c._build_instagram_judge()
        assert caplog.records == []


class TestSettingsAreNotDuplicated:
    def test_the_judge_model_is_defined_once(self):
        """A field declared twice silently keeps the LAST one, so an edit to the
        first has no effect and drifts unnoticed."""
        import inspect

        source = inspect.getsource(Settings)
        assert source.count("instagram_judge_model:") == 1

    def test_every_judge_setting_is_readable(self):
        s = Settings()
        assert isinstance(s.instagram_judge_enabled, bool)
        assert isinstance(s.instagram_judge_model, str) and s.instagram_judge_model
        assert 0.0 <= s.instagram_judge_floor <= 1.0
        assert s.instagram_judge_max_venue_photos >= 0
