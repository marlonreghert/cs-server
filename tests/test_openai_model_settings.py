"""Pins which OpenAI model each path uses.

Model ids are strings scattered across settings, clients, and service
constructors, so they drift silently — a default left behind on an old model
keeps costing the old rate and nothing fails. These tests make a stale one fail
loudly instead.

GPT-5.4 list rates (2026-07):
    gpt-5.4-nano   $0.20 / 1M input   $1.25 / 1M output
    gpt-5.4-mini   $0.75 / 1M input   $4.50 / 1M output
"""
from __future__ import annotations

import inspect

import pytest

from app.config import Settings

NANO = "gpt-5.4-nano"
MINI = "gpt-5.4-mini"


def _settings():
    return Settings(_env_file=None)


class TestConfiguredModels:
    @pytest.mark.parametrize("field", [
        "photo_classification_model",
        "menu_extraction_model",
        "vibe_classifier_stage_a_model",
    ])
    def test_high_volume_paths_use_nano(self, field):
        # Classification and extraction over many photos: nano is the tier
        # OpenAI positions for exactly this, and the cheapest input rate.
        assert getattr(_settings(), field) == NANO

    def test_vibe_stage_b_is_stronger_than_stage_a(self):
        # Stage B exists to settle what Stage A could not. If both tiers were
        # the same model, escalation would cost a second call and buy nothing —
        # the two-stage design collapses into one weak stage.
        s = _settings()
        assert s.vibe_classifier_stage_b_model == MINI
        assert s.vibe_classifier_stage_b_model != s.vibe_classifier_stage_a_model

    def test_no_setting_is_left_on_a_gpt4_model(self):
        # Instagram is deliberately excluded: it is owned elsewhere right now.
        stale = {
            name: value
            for name, value in _settings().model_dump().items()
            if name.endswith("_model")
            and isinstance(value, str)
            and value.startswith("gpt-4")
            and "instagram" not in name
        }
        assert stale == {}, f"still on a GPT-4 model: {stale}"


class TestPricingMatchesTheModel:
    def test_input_rate_matches_nano_list_price(self):
        # $0.20 / 1M == $0.0002 / 1k. A stale rate silently misreports spend,
        # and the archive's cost meter is read straight off these.
        assert _settings().photo_classification_cost_per_1k_input_usd == pytest.approx(0.0002)

    def test_output_rate_matches_nano_list_price(self):
        # $1.25 / 1M == $0.00125 / 1k
        assert _settings().photo_classification_cost_per_1k_output_usd == pytest.approx(0.00125)

    def test_output_costs_more_than_input(self):
        s = _settings()
        assert (
            s.photo_classification_cost_per_1k_output_usd
            > s.photo_classification_cost_per_1k_input_usd
        )


class TestConstructorDefaultsAgree:
    """A default that disagrees with settings is a model nobody chose."""

    def _default(self, fn, param):
        return inspect.signature(fn).parameters[param].default

    def test_photo_classifier_client_default(self):
        from app.api import openai_photo_classifier_client as m
        assert m.DEFAULT_MODEL == NANO

    def test_vibe_service_defaults_preserve_the_tiering(self):
        from app.services.vibe_classifier_service import VibeClassifierService
        a = self._default(VibeClassifierService.__init__, "stage_a_model")
        b = self._default(VibeClassifierService.__init__, "stage_b_model")
        assert (a, b) == (NANO, MINI)

    def test_menu_extraction_service_default(self):
        from app.services.menu_extraction_service import MenuExtractionService
        assert self._default(MenuExtractionService.__init__, "extraction_model") == NANO
