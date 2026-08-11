"""Unit tests for app/models/post_category.py.

See plans/260811_post-items-and-categories.md §C. Covers category
normalisation (exact, case-differing, accent-differing, whitespace-padded,
empty, absent, and a genuinely new value — the plan's own required cases),
the admin-config load/validate round trip, and the off-vocabulary counter's
cardinality cap.
"""
from __future__ import annotations

import json

import pytest

from app.models.post_category import (
    DEFAULT_CATEGORY_VOCABULARY,
    OFF_VOCABULARY_OVERFLOW_LABEL,
    ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY,
    _MAX_TRACKED_OFF_VOCABULARY_LABELS,
    _seen_off_vocabulary_labels,
    canonicalize_category,
    is_in_vocabulary,
    load_post_category_vocabulary,
    record_off_vocabulary_category,
    validate_post_category_vocabulary_config,
)


# ── canonicalize_category: the plan's own required cases ────────────────────
class TestCanonicalizeCategory:
    def test_exact_match(self):
        assert canonicalize_category("rock", DEFAULT_CATEGORY_VOCABULARY) == "rock"

    def test_case_differing_match_stores_vocabulary_spelling(self):
        assert canonicalize_category("ROCK", DEFAULT_CATEGORY_VOCABULARY) == "rock"
        assert canonicalize_category("Rock", DEFAULT_CATEGORY_VOCABULARY) == "rock"

    def test_accent_differing_text_is_not_forced_to_match(self):
        """`casefold()`, not `.lower()` — pt-BR text. `forró` differs from
        `forro` by an accent, not by case, so this is correctly a MISS (the
        vocabulary entry is `forró`, exactly as written) — the point of this
        case is that casefold does not silently strip accents either; it is
        the correct Unicode fold, not a fuzzy match."""
        assert canonicalize_category("FORRÓ", DEFAULT_CATEGORY_VOCABULARY) == "forró"
        assert canonicalize_category("forro", DEFAULT_CATEGORY_VOCABULARY) == "forro"

    def test_whitespace_padded_and_internally_collapsed(self):
        assert canonicalize_category("  rock  ", DEFAULT_CATEGORY_VOCABULARY) == "rock"
        # Matches case-insensitively (whitespace-collapsed "dj / club night"
        # folds onto the vocabulary's own "DJ / club night") and stores the
        # VOCABULARY's spelling, not the input's.
        assert canonicalize_category("dj  /   club night", DEFAULT_CATEGORY_VOCABULARY) == (
            "DJ / club night"
        )

    def test_empty_string_is_none(self):
        assert canonicalize_category("", DEFAULT_CATEGORY_VOCABULARY) is None
        assert canonicalize_category("   ", DEFAULT_CATEGORY_VOCABULARY) is None

    def test_absent_is_none(self):
        assert canonicalize_category(None, DEFAULT_CATEGORY_VOCABULARY) is None

    def test_genuinely_new_value_is_kept_verbatim_never_rejected(self):
        assert canonicalize_category("bingo", DEFAULT_CATEGORY_VOCABULARY) == "bingo"
        assert canonicalize_category("  Bingo Night  ", DEFAULT_CATEGORY_VOCABULARY) == (
            "Bingo Night"
        )


class TestIsInVocabulary:
    def test_match_case_insensitive(self):
        assert is_in_vocabulary("ROCK", DEFAULT_CATEGORY_VOCABULARY) is True

    def test_miss(self):
        assert is_in_vocabulary("bingo", DEFAULT_CATEGORY_VOCABULARY) is False

    def test_none_and_blank_are_never_in_vocabulary(self):
        assert is_in_vocabulary(None, DEFAULT_CATEGORY_VOCABULARY) is False
        assert is_in_vocabulary("  ", DEFAULT_CATEGORY_VOCABULARY) is False


# ── admin config validate/load ───────────────────────────────────────────────
class TestValidatePostCategoryVocabularyConfig:
    def test_rejects_non_list(self):
        with pytest.raises(TypeError):
            validate_post_category_vocabulary_config({"rock": True})

    def test_rejects_non_string_entries(self):
        with pytest.raises(TypeError):
            validate_post_category_vocabulary_config(["rock", 5])

    def test_rejects_blank_entry(self):
        with pytest.raises(ValueError):
            validate_post_category_vocabulary_config(["rock", "   "])

    def test_rejects_empty_list(self):
        with pytest.raises(ValueError):
            validate_post_category_vocabulary_config([])

    def test_normalizes_whitespace_and_dedupes_case_insensitively(self):
        result = validate_post_category_vocabulary_config([
            "  bingo ", "Bingo", "BINGO", "quiz",
        ])
        assert result == ["bingo", "quiz"]


class _FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})

    def get(self, key):
        return self.store.get(key)


class TestLoadPostCategoryVocabulary:
    def test_none_redis_falls_back_to_defaults(self):
        vocab, reason = load_post_category_vocabulary(None)
        assert vocab == list(DEFAULT_CATEGORY_VOCABULARY)
        assert reason is None

    def test_missing_key_falls_back_to_defaults_without_a_fallback_reason(self):
        vocab, reason = load_post_category_vocabulary(_FakeRedis())
        assert vocab == list(DEFAULT_CATEGORY_VOCABULARY)
        assert reason is None  # missing key is the expected pre-first-write state

    def test_reads_the_admin_override(self):
        redis = _FakeRedis({
            ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY: json.dumps(["bingo", "quiz"]),
        })
        vocab, reason = load_post_category_vocabulary(redis)
        assert vocab == ["bingo", "quiz"]
        assert reason is None

    def test_invalid_json_falls_back_to_defaults_with_a_reason(self):
        redis = _FakeRedis({ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY: "{not json"})
        vocab, reason = load_post_category_vocabulary(redis)
        assert vocab == list(DEFAULT_CATEGORY_VOCABULARY)
        assert reason == "invalid_json"

    def test_invalid_shape_falls_back_to_defaults_with_a_reason(self):
        redis = _FakeRedis({
            ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY: json.dumps({"not": "a list"}),
        })
        vocab, reason = load_post_category_vocabulary(redis)
        assert vocab == list(DEFAULT_CATEGORY_VOCABULARY)
        assert reason == "invalid_shape"


# ── off-vocabulary counter cardinality cap ───────────────────────────────────
class TestOffVocabularyCardinalityCap:
    def setup_method(self):
        # Isolate each test from the module-level bounded set — a real
        # process never resets it, but a test must not leak state into its
        # neighbours (or into the plan's own "cap stays bounded" property).
        _seen_off_vocabulary_labels.clear()

    def teardown_method(self):
        _seen_off_vocabulary_labels.clear()

    def test_distinct_values_within_the_cap_each_get_their_own_label(self):
        from app.metrics import POST_CATEGORY_OFF_VOCABULARY_TOTAL

        record_off_vocabulary_category("bingo")
        record_off_vocabulary_category("trivia")
        assert POST_CATEGORY_OFF_VOCABULARY_TOTAL.labels(category="bingo")._value.get() >= 1.0
        assert POST_CATEGORY_OFF_VOCABULARY_TOTAL.labels(category="trivia")._value.get() >= 1.0

    def test_cardinality_never_exceeds_the_cap(self):
        for i in range(_MAX_TRACKED_OFF_VOCABULARY_LABELS + 25):
            record_off_vocabulary_category(f"category-{i}")
        assert len(_seen_off_vocabulary_labels) == _MAX_TRACKED_OFF_VOCABULARY_LABELS

    def test_values_over_the_cap_bucket_into_the_overflow_label(self):
        from app.metrics import POST_CATEGORY_OFF_VOCABULARY_TOTAL

        for i in range(_MAX_TRACKED_OFF_VOCABULARY_LABELS):
            record_off_vocabulary_category(f"warmup-{i}")
        before = POST_CATEGORY_OFF_VOCABULARY_TOTAL.labels(
            category=OFF_VOCABULARY_OVERFLOW_LABEL,
        )._value.get()
        record_off_vocabulary_category("one-value-too-many")
        record_off_vocabulary_category("another-value-too-many")
        after = POST_CATEGORY_OFF_VOCABULARY_TOTAL.labels(
            category=OFF_VOCABULARY_OVERFLOW_LABEL,
        )._value.get()
        assert after - before == 2.0

    def test_a_value_already_tracked_keeps_its_own_label_even_after_the_cap_fills(self):
        from app.metrics import POST_CATEGORY_OFF_VOCABULARY_TOTAL

        record_off_vocabulary_category("bingo")
        for i in range(_MAX_TRACKED_OFF_VOCABULARY_LABELS):
            record_off_vocabulary_category(f"fill-{i}")
        before = POST_CATEGORY_OFF_VOCABULARY_TOTAL.labels(category="bingo")._value.get()
        record_off_vocabulary_category("bingo")
        after = POST_CATEGORY_OFF_VOCABULARY_TOTAL.labels(category="bingo")._value.get()
        assert after - before == 1.0
