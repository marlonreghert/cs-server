"""The judge builder must run, not merely import.

This exists because of a real outage: `_build_instagram_judge` referenced the
bare name `settings`, which in `container.py` is only a PARAMETER of `__init__`.
Importing the module proved nothing — the NameError needed the method to
actually execute. cs-server crash-looped 36 times on startup in production.

So these tests EXECUTE the builder against every branch. Container.__init__ is
too heavy to construct here (Redis, RDS, S3), so the instance is built without
running __init__ and given only what the method reads.
"""
import pytest

from app.config import Settings
from app.container import Container


def _container(**overrides):
    """A Container that never ran __init__, carrying only settings."""
    instance = Container.__new__(Container)
    instance.settings = Settings(**overrides)
    return instance


class TestTheBuilderExecutes:
    def test_disabled_returns_none(self):
        assert _container(instagram_judge_enabled=False)._build_instagram_judge() is None

    def test_enabled_without_a_key_returns_none(self):
        """Enabled but unconfigured must degrade, not explode on startup."""
        c = _container(instagram_judge_enabled=True, openai_api_key="")
        assert c._build_instagram_judge() is None

    def test_enabled_with_a_key_builds_a_judge(self):
        c = _container(instagram_judge_enabled=True, openai_api_key="sk-test-key")
        judge = c._build_instagram_judge()
        assert judge is not None
        assert hasattr(judge, "judge")

    def test_it_passes_the_configured_model_through(self):
        c = _container(
            instagram_judge_enabled=True,
            openai_api_key="sk-test-key",
            instagram_judge_model="gpt-5.4-mini",
        )
        assert c._build_instagram_judge().model == "gpt-5.4-mini"

    def test_it_passes_the_configured_photo_cap_through(self):
        c = _container(
            instagram_judge_enabled=True,
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
