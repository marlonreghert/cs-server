"""Shared admin-config type predicates and the instagram_discovery validator
(app/services/config_validation.py).

The bool-vs-int subtlety these encode is the whole reason they are shared:
``bool`` subclasses ``int``, so every validator that hand-rolls the check
risks accepting ``true`` where a number is required.
"""
import pytest

from app.services.config_validation import (
    is_int,
    is_number,
    is_string_list,
    validate_instagram_discovery_config,
)


def test_is_number_accepts_ints_and_floats_but_not_bools():
    assert is_number(4) and is_number(4.5) and is_number(0) and is_number(-1.2)
    assert not is_number(True) and not is_number(False)
    assert not is_number("4") and not is_number(None) and not is_number([4])


def test_is_int_accepts_ints_but_not_bools_or_floats():
    assert is_int(0) and is_int(-3) and is_int(10)
    assert not is_int(True) and not is_int(False)
    assert not is_int(3.0) and not is_int("3") and not is_int(None)


def test_is_string_list():
    assert is_string_list([]) and is_string_list(["a", "b"])
    assert not is_string_list("ab") and not is_string_list(["a", 1])
    assert not is_string_list(None) and not is_string_list({"a": 1})


# ── validate_instagram_discovery_config ──────────────────────────────────────
# plans/260811_instagram-discovery-admin-flags.md


class TestInstagramDiscoveryConfigAccepts:
    def test_empty_object(self):
        assert validate_instagram_discovery_config({}) == {}

    def test_both_fields_true(self):
        value = {"google_search_enabled": True, "judge_enabled": True}
        assert validate_instagram_discovery_config(value) == value

    def test_both_fields_false(self):
        value = {"google_search_enabled": False, "judge_enabled": False}
        assert validate_instagram_discovery_config(value) == value

    def test_only_one_field_set(self):
        value = {"google_search_enabled": True}
        assert validate_instagram_discovery_config(value) == value
        value2 = {"judge_enabled": False}
        assert validate_instagram_discovery_config(value2) == value2

    def test_returns_the_value_unchanged_not_a_copy_with_defaults_filled_in(self):
        """An absent field must stay absent in the persisted value — the
        resolution helper treats "absent" as "no override", not "false"."""
        value = {"judge_enabled": True}
        stored = validate_instagram_discovery_config(value)
        assert "google_search_enabled" not in stored


class TestInstagramDiscoveryConfigRejects:
    def test_not_an_object(self):
        for bad in ("yes", 1, True, None, ["google_search_enabled"]):
            with pytest.raises(TypeError):
                validate_instagram_discovery_config(bad)

    def test_unknown_field(self):
        with pytest.raises(ValueError):
            validate_instagram_discovery_config({"apify_search_enabled": True})

    def test_unknown_field_alongside_a_known_one(self):
        with pytest.raises(ValueError):
            validate_instagram_discovery_config(
                {"google_search_enabled": True, "apify_search_enabled": True}
            )

    @pytest.mark.parametrize("field", ["google_search_enabled", "judge_enabled"])
    @pytest.mark.parametrize("bad_value", ["yes", 1, 0, None, [], {}])
    def test_non_boolean_value(self, field, bad_value):
        with pytest.raises(TypeError):
            validate_instagram_discovery_config({field: bad_value})
