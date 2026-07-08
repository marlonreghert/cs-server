"""Unit tests for the vibe_modes admin config validator.

Table-driven coverage of each accept/reject rule in
app/services/vibe_modes_config.py: array/mode/filter shape, field typing (with
the bool-vs-int guard), busyness bounds, sort strategy, quality gates, label
matchers, unique ids, at-least-one-enabled, at-most-one-default, extras
preservation, and the no-mutation invariant. The validator is registered in
app/container.py and the BDD harness; the end-to-end persist/mirror contract is
covered by tests/bdd/api/vibe-modes-config-validator.feature.
"""
from __future__ import annotations

import copy

import pytest

from app.services.vibe_modes_config import validate_vibe_modes_config


def _filter(**overrides) -> dict:
    filt = {
        "allowed_types": ["BAR"],
        "always_pass_types": [],
        "excluded_granular_types": [],
        "quality_gates": [{"types": ["BAR"], "min_rating": 4.0, "min_reviews": 5}],
        "requires_open_late": False,
        "vibe_label_matchers": [
            {"category": "estilo_do_lugar", "labels": ["Lounge"]},
        ],
    }
    filt.update(overrides)
    return filt


def _mode(mode_id="explorar", *, is_default=False, enabled=True, **overrides) -> dict:
    mode = {
        "id": mode_id,
        "label": "Label",
        "emoji": "🔥",
        "description": "desc",
        "is_default": is_default,
        "enabled": enabled,
        "busyness_range": [0, 4],
        "sort_strategy": "combined_score_desc",
        "affinity": {"bar": 1.0},
        "filter": _filter(),
    }
    mode.update(overrides)
    return mode


def _modes(*modes) -> list:
    return list(modes) if modes else [_mode(is_default=True)]


# ── accept ───────────────────────────────────────────────────────────────────
ACCEPT_CASES = {
    "single default mode": _modes(_mode(is_default=True)),
    "several modes one default": _modes(
        _mode("explorar", is_default=True),
        _mode("role_calmo", sort_strategy="rating_desc"),
        _mode("after", sort_strategy="busyness_desc", filter=_filter(requires_open_late=True)),
    ),
    "no default mode is allowed": _modes(_mode("a"), _mode("b")),
    "empty affinity": _modes(_mode(affinity={})),
    "empty filter arrays and no gates": _modes(
        _mode(filter=_filter(allowed_types=[], quality_gates=[], vibe_label_matchers=[]))
    ),
    "equal busyness bounds": _modes(_mode(busyness_range=[2, 2])),
    "min_reviews zero": _modes(
        _mode(filter=_filter(quality_gates=[{"types": ["X"], "min_rating": 4.0, "min_reviews": 0}]))
    ),
    "integer min_rating": _modes(
        _mode(filter=_filter(quality_gates=[{"types": ["X"], "min_rating": 4, "min_reviews": 3}]))
    ),
    "reader-consumed extras when valid": _modes(
        _mode(trajectory_weight=0.5, filter=_filter(requires_family_signal=True))
    ),
}


@pytest.mark.parametrize("modes", ACCEPT_CASES.values(), ids=list(ACCEPT_CASES))
def test_valid_configs_pass_and_return_unchanged(modes):
    # Returns the exact same object so persisted bytes stay reader-compatible.
    assert validate_vibe_modes_config(modes) is modes


def test_extra_keys_are_preserved_verbatim():
    modes = _modes(
        _mode("explorar", is_default=True, trajectory_weight=0.5,
              mystery_top_key="keep-me",
              filter=_filter(requires_family_signal=True, future_flag=[1, 2]))
    )
    result = validate_vibe_modes_config(modes)
    assert result is modes
    # reader-consumed extras (validated) are preserved when valid...
    assert result[0]["trajectory_weight"] == 0.5
    assert result[0]["filter"]["requires_family_signal"] is True
    # ...and genuinely-unknown keys pass through untouched.
    assert result[0]["mystery_top_key"] == "keep-me"
    assert result[0]["filter"]["future_flag"] == [1, 2]


# ── reject: ValueError ───────────────────────────────────────────────────────
VALUE_ERROR_CASES = {
    # array-level
    "not a list": {"explorar": _mode()},
    "empty list": [],
    "mode not an object": ["explorar"],
    # missing required top-level fields
    "missing id": _modes({k: v for k, v in _mode().items() if k != "id"}),
    "missing label": _modes({k: v for k, v in _mode().items() if k != "label"}),
    "missing busyness_range": _modes({k: v for k, v in _mode().items() if k != "busyness_range"}),
    "missing filter": _modes({k: v for k, v in _mode().items() if k != "filter"}),
    "missing affinity": _modes({k: v for k, v in _mode().items() if k != "affinity"}),
    # id
    "empty id": _modes(_mode(id="")),
    "non-string id": _modes(_mode(id=7)),
    "duplicate ids": _modes(_mode("explorar", is_default=True), _mode("explorar")),
    # string fields
    "non-string label": _modes(_mode(label=1)),
    "non-string emoji": _modes(_mode(emoji=None)),
    # bool fields
    "non-bool enabled": _modes(_mode(enabled="yes")),
    "non-bool is_default": _modes(_mode(is_default="no")),
    # busyness_range
    "busyness wrong length": _modes(_mode(busyness_range=[0, 1, 2])),
    "busyness non-int": _modes(_mode(busyness_range=[0, "4"])),
    "busyness bool member": _modes(_mode(busyness_range=[True, 2])),
    "busyness inverted": _modes(_mode(busyness_range=[3, 1])),
    "busyness above max": _modes(_mode(busyness_range=[0, 5])),
    "busyness below min": _modes(_mode(busyness_range=[-1, 2])),
    "busyness not a list": _modes(_mode(busyness_range="0-4")),
    # sort_strategy
    "unknown sort_strategy": _modes(_mode(sort_strategy="popularity_desc")),
    # affinity
    "affinity not object": _modes(_mode(affinity=[])),
    "affinity non-number value": _modes(_mode(affinity={"bar": "high"})),
    "affinity bool value": _modes(_mode(affinity={"bar": True})),
    "affinity non-string key": _modes(_mode(affinity={7: 1.0})),
    # reader-consumed optional extras (validated when present)
    "non-numeric trajectory_weight": _modes(_mode(trajectory_weight="high")),
    "null trajectory_weight": _modes(_mode(trajectory_weight=None)),
    "list trajectory_weight": _modes(_mode(trajectory_weight=[1, 2])),
    "bool trajectory_weight": _modes(_mode(trajectory_weight=True)),
    "non-bool requires_family_signal": _modes(
        _mode(filter=_filter(requires_family_signal="false"))
    ),
    # filter shape
    "filter not object": _modes(_mode(filter=[])),
    "filter missing quality_gates": _modes(
        _mode(filter={k: v for k, v in _filter().items() if k != "quality_gates"})
    ),
    "filter missing requires_open_late": _modes(
        _mode(filter={k: v for k, v in _filter().items() if k != "requires_open_late"})
    ),
    "filter array not strings": _modes(_mode(filter=_filter(allowed_types=[1, 2]))),
    "requires_open_late non-bool": _modes(_mode(filter=_filter(requires_open_late="no"))),
    # quality gates
    "quality_gates not a list": _modes(_mode(filter=_filter(quality_gates={}))),
    "quality gate not object": _modes(_mode(filter=_filter(quality_gates=["x"]))),
    "quality gate missing types": _modes(
        _mode(filter=_filter(quality_gates=[{"min_rating": 4.0, "min_reviews": 5}]))
    ),
    "quality gate missing min_rating": _modes(
        _mode(filter=_filter(quality_gates=[{"types": ["X"], "min_reviews": 5}]))
    ),
    "quality gate min_reviews float": _modes(
        _mode(filter=_filter(quality_gates=[{"types": ["X"], "min_rating": 4.0, "min_reviews": 5.5}]))
    ),
    "quality gate min_reviews bool": _modes(
        _mode(filter=_filter(quality_gates=[{"types": ["X"], "min_rating": 4.0, "min_reviews": True}]))
    ),
    # vibe label matchers
    "matchers not a list": _modes(_mode(filter=_filter(vibe_label_matchers={}))),
    "matcher not object": _modes(_mode(filter=_filter(vibe_label_matchers=["x"]))),
    "matcher empty category": _modes(
        _mode(filter=_filter(vibe_label_matchers=[{"category": "", "labels": []}]))
    ),
    "matcher labels not strings": _modes(
        _mode(filter=_filter(vibe_label_matchers=[{"category": "c", "labels": [1]}]))
    ),
    # list-level invariants
    "all disabled": _modes(_mode("a", enabled=False), _mode("b", enabled=False)),
    "two defaults": _modes(_mode("a", is_default=True), _mode("b", is_default=True)),
}


@pytest.mark.parametrize("modes", VALUE_ERROR_CASES.values(), ids=list(VALUE_ERROR_CASES))
def test_invalid_configs_raise_value_error(modes):
    with pytest.raises(ValueError):
        validate_vibe_modes_config(modes)


# ── error message names the offending mode and field ─────────────────────────
def test_missing_quality_gates_names_mode_and_field():
    modes = _modes(
        _mode("explorar", is_default=True),
        _mode("role_calmo", filter={k: v for k, v in _filter().items() if k != "quality_gates"}),
    )
    with pytest.raises(ValueError) as exc:
        validate_vibe_modes_config(modes)
    message = str(exc.value)
    assert "role_calmo" in message
    assert "filter.quality_gates" in message


def test_missing_top_level_field_names_mode_and_field():
    jantar = _mode("jantar", is_default=True)
    modes = _modes({k: v for k, v in jantar.items() if k != "busyness_range"})
    with pytest.raises(ValueError) as exc:
        validate_vibe_modes_config(modes)
    message = str(exc.value)
    assert "jantar" in message
    assert "busyness_range" in message


def test_duplicate_id_names_the_id():
    modes = _modes(_mode("explorar", is_default=True), _mode("explorar"))
    with pytest.raises(ValueError, match="explorar"):
        validate_vibe_modes_config(modes)


def test_non_numeric_trajectory_weight_names_mode_and_field():
    modes = _modes(_mode("role_agitado", trajectory_weight="high"))
    with pytest.raises(ValueError) as exc:
        validate_vibe_modes_config(modes)
    message = str(exc.value)
    assert "role_agitado" in message and "trajectory_weight" in message


def test_mode_without_valid_id_is_referenced_by_index():
    modes = _modes(_mode(id=7))
    with pytest.raises(ValueError, match="index 0"):
        validate_vibe_modes_config(modes)


# ── no-mutation invariant: a rejected body is never mutated ──────────────────
def test_validator_does_not_mutate_rejected_body():
    modes = _modes(
        _mode("explorar", is_default=True),
        _mode("role_calmo", filter={k: v for k, v in _filter().items() if k != "quality_gates"}),
    )
    snapshot = copy.deepcopy(modes)
    with pytest.raises(ValueError):
        validate_vibe_modes_config(modes)
    assert modes == snapshot


# ── acceptance criteria: the real production payload ─────────────────────────
# Pinned snapshot of the canonical 8-mode array (vibes_bot
# app/services/vibe_modes_service.py DEFAULT_VIBE_MODES as of 2026-07-08). This
# is the true serving shape — multi-codepoint emoji, extra keys
# (trajectory_weight, requires_family_signal), min_reviews: 0, all three sort
# strategies, empty and large affinities. Cannot be imported across submodules,
# so it is pinned here; that also guards the validator against future drift.
# The stored production value differed only in that role_calmo's filter was
# missing quality_gates (the incident); the tests below reproduce that exactly.
PRODUCTION_VIBE_MODES = [
    {
        "id": "explorar",
        "label": "Todas as vibes",
        "emoji": "🔍",
        "description": "Todas as vibes",
        "is_default": True,
        "filter": {
            "allowed_types": [],
            "always_pass_types": [],
            "excluded_granular_types": [],
            "quality_gates": [],
            "requires_open_late": False,
            "vibe_label_matchers": [],
        },
        "busyness_range": [0, 4],
        "sort_strategy": "combined_score_desc",
        "affinity": {},
        "enabled": True,
    },
    {
        "id": "role_agitado",
        "label": "Rolê Agitado",
        "emoji": "🔥",
        "description": "Onde tá pegando fogo",
        "is_default": False,
        "filter": {
            "allowed_types": [
                "BAR", "PUB", "NIGHTCLUB", "KARAOKE", "COCKTAIL_BAR",
                "BREWERY", "EVENT_VENUE", "LIVE_MUSIC", "WINERY",
                "CASINO", "ENTERTAINMENT", "PARK",
            ],
            "always_pass_types": [],
            "excluded_granular_types": [],
            "quality_gates": [],
            "requires_open_late": False,
            "vibe_label_matchers": [
                {"category": "estilo_do_lugar", "labels": ["Balada", "Pista de dança"]},
            ],
        },
        "busyness_range": [2, 4],
        "sort_strategy": "busyness_desc",
        "affinity": {"night_club": 1.5, "karaoke": 1.3, "bar": 1.0, "park": 0.8},
        "trajectory_weight": 0.5,
        "enabled": True,
    },
    {
        "id": "role_calmo",
        "label": "Rolê Calmo",
        "emoji": "🍸",
        "description": "Drink tranquilo",
        "is_default": False,
        "filter": {
            "allowed_types": [
                "BAR", "PUB", "COCKTAIL_BAR", "BREWERY", "WINERY", "COFFEE_SHOP", "PARK",
            ],
            "always_pass_types": ["COCKTAIL_BAR", "WINERY", "COFFEE_SHOP", "PARK"],
            "excluded_granular_types": [],
            "quality_gates": [
                {"types": ["BAR", "PUB", "BREWERY"], "min_rating": 4.0, "min_reviews": 5},
            ],
            "requires_open_late": False,
            "vibe_label_matchers": [
                {"category": "estilo_do_lugar", "labels": ["Lounge"]},
                {"category": "clima_social", "labels": ["Tranquilo", "Intimista"]},
            ],
        },
        "busyness_range": [0, 1],
        "sort_strategy": "rating_desc",
        "affinity": {"wine_bar": 1.5, "coffee_shop": 1.3, "bar": 1.0, "park": 1.2},
        "enabled": True,
    },
    {
        "id": "jantar",
        "label": "Jantar",
        "emoji": "🍽️",
        "description": "Comer bem",
        "is_default": False,
        "filter": {
            "allowed_types": ["RESTAURANT", "BUFFET", "FOOD_DRINK"],
            "always_pass_types": [],
            "excluded_granular_types": [],
            "quality_gates": [],
            "requires_open_late": False,
            "vibe_label_matchers": [
                {"category": "intencao", "labels": ["Comer bem"]},
            ],
        },
        "busyness_range": [0, 2],
        "sort_strategy": "rating_desc",
        "affinity": {"brazilian_restaurant": 1.3, "restaurant": 1.0},
        "enabled": True,
    },
    {
        "id": "familia",
        "label": "Família",
        "emoji": "🧸",
        "description": "Diversão em família",
        "is_default": False,
        "filter": {
            "allowed_types": ["RESTAURANT", "BUFFET", "FOOD_DRINK", "PARK"],
            "always_pass_types": [],
            "excluded_granular_types": [
                "night_club", "cocktail_bar", "wine_bar", "irish_pub",
            ],
            "quality_gates": [
                {"types": ["RESTAURANT"], "min_rating": 3.8, "min_reviews": 10},
            ],
            "requires_open_late": False,
            "requires_family_signal": True,
            "vibe_label_matchers": [],
        },
        "busyness_range": [0, 3],
        "sort_strategy": "rating_desc",
        "affinity": {"family_restaurant": 1.5, "park": 1.3, "restaurant": 1.0},
        "enabled": True,
    },
    {
        "id": "date",
        "label": "Date Romântico",
        "emoji": "🍷",
        "description": "Clima a dois",
        "is_default": False,
        "filter": {
            "allowed_types": [
                "COCKTAIL_BAR", "WINERY", "COFFEE_SHOP", "RESTAURANT", "PUB",
            ],
            "always_pass_types": ["COCKTAIL_BAR", "WINERY"],
            "excluded_granular_types": [
                "snack_bar", "buffet_restaurant", "fast_food_restaurant",
            ],
            "quality_gates": [
                {"types": ["COFFEE_SHOP"], "min_rating": 4.0, "min_reviews": 0},
                {"types": ["RESTAURANT"], "min_rating": 4.0, "min_reviews": 10},
                {"types": ["PUB"], "min_rating": 4.0, "min_reviews": 5},
            ],
            "requires_open_late": False,
            "vibe_label_matchers": [
                {"category": "intencao", "labels": ["Clima de date"]},
                {"category": "clima_social", "labels": ["Intimista", "Tranquilo"]},
            ],
        },
        "busyness_range": [0, 1],
        "sort_strategy": "rating_desc",
        "affinity": {"wine_bar": 1.5, "coffee_shop": 1.4, "restaurant": 1.1},
        "enabled": True,
    },
    {
        "id": "resenha",
        "label": "Resenha com a Galera",
        "emoji": "🍻",
        "description": "Mesa e conversa",
        "is_default": False,
        "filter": {
            "allowed_types": ["BAR", "PUB", "BREWERY", "COCKTAIL_BAR", "FOOD_DRINK", "PARK"],
            "always_pass_types": [],
            "excluded_granular_types": [
                "snack_bar", "buffet_restaurant", "fast_food_restaurant",
            ],
            "quality_gates": [],
            "requires_open_late": False,
            "vibe_label_matchers": [
                {"category": "estilo_do_lugar", "labels": ["Boteco raiz", "Gastrobar"]},
                {"category": "intencao", "labels": ["Sentar com a galera"]},
            ],
        },
        "busyness_range": [1, 3],
        "sort_strategy": "rating_desc",
        "affinity": {"pub": 1.4, "bar": 1.1, "park": 1.1},
        "enabled": True,
    },
    {
        "id": "after",
        "label": "After",
        "emoji": "🌙",
        "description": "Aberto até tarde",
        "is_default": False,
        "filter": {
            "allowed_types": [
                "BAR", "PUB", "NIGHTCLUB", "KARAOKE", "COCKTAIL_BAR",
                "BREWERY", "WINERY", "CASINO", "ENTERTAINMENT",
            ],
            "always_pass_types": [],
            "excluded_granular_types": [],
            "quality_gates": [],
            "requires_open_late": True,
            "vibe_label_matchers": [],
        },
        "busyness_range": [0, 4],
        "sort_strategy": "busyness_desc",
        "affinity": {},
        "enabled": True,
    },
]


def _production_with_role_calmo_bug() -> list:
    """The value stored in prod on 2026-07-07: identical to the canonical array
    except role_calmo's filter is missing quality_gates."""
    payload = copy.deepcopy(PRODUCTION_VIBE_MODES)
    role_calmo = next(m for m in payload if m["id"] == "role_calmo")
    role_calmo["filter"].pop("quality_gates")
    return payload


def test_canonical_production_payload_is_accepted_unchanged():
    payload = copy.deepcopy(PRODUCTION_VIBE_MODES)
    assert validate_vibe_modes_config(payload) is payload


def test_production_payload_rejected_solely_for_role_calmo_quality_gates():
    # Acceptance criterion #1: today's stored value is rejected, naming both the
    # offending mode and field.
    with pytest.raises(ValueError) as exc:
        validate_vibe_modes_config(_production_with_role_calmo_bug())
    message = str(exc.value)
    assert "role_calmo" in message and "filter.quality_gates" in message


def test_corrected_production_payload_is_accepted_unchanged():
    # Acceptance criterion #2: adding quality_gates: [] (the operator's
    # remediation PUT) makes the exact same array valid and byte-unchanged.
    corrected = _production_with_role_calmo_bug()
    next(m for m in corrected if m["id"] == "role_calmo")["filter"]["quality_gates"] = []
    assert validate_vibe_modes_config(corrected) is corrected


def test_disabled_default_mode_is_rejected():
    # A disabled default is unservable: clients filter to enabled modes before
    # resolving the default, so the flagged default would silently fall back to
    # whatever mode is first in the array.
    with pytest.raises(ValueError) as exc:
        validate_vibe_modes_config(_modes(
            _mode("explorar", is_default=True, enabled=False),
            _mode("jantar", enabled=True),
        ))
    message = str(exc.value)
    assert "explorar" in message and "default" in message and "enabled" in message


def test_enabled_default_mode_is_accepted():
    payload = _modes(
        _mode("explorar", is_default=True, enabled=True),
        _mode("jantar", enabled=False),
    )
    assert validate_vibe_modes_config(payload) is payload
