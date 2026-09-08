"""The shape of `tests/fixtures/kind_eval_captions.json`, checked OFFLINE.

Split out of `tests/test_kind_eval_live.py` deliberately. That module carries
a module-level `pytest.mark.live_openai` and is enumerated by neither Make
target, so the guard below — the one thing standing between the eval fixture
and a silent loss of half its rows — never actually ran anywhere (F1). This
file has no marker and IS enumerated in `make test-unit`, so it runs in CI on
every push.

It makes no network call and reads nothing but the JSON file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "kind_eval_captions.json"
KINDS = ("event", "promotion", "menu", "food", "other")


# The eval's two halves. A fixture that keeps only one of them still gates
# something, which is exactly why losing the other must be a test failure
# rather than a quiet reduction in coverage (F5).
#
# PROTECTED — must survive as `event`. These are §0's four non-conceded
# shapes plus the seams where a price sits inside an event caption.
_PROTECTED = ("S4", "S5", "S6", "S7", "S14", "G4", "G5", "G6", "G7", "G8")
# SUPPRESSED — must be answered `menu`/`promotion`. Both live offenders, the
# "Especial do dia" regression named in the 1.4.1 brief, and the
# generalisation controls built on food nouns the prompt never enumerates.
_SUPPRESSED = ("S9", "S10", "S11", "S12", "S13", "G1", "G2", "G3")


def _rows():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_every_row_is_well_formed():
    rows = _rows()
    assert rows, "the eval fixture is empty"
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), "duplicate row ids in the fixture"
    for row in rows:
        assert row.get("caption"), row["id"]
        assert "expected_kind" in row, row["id"]
        assert "blocking" in row, row["id"]
        if row["expected_kind"] is not None:
            assert row["expected_kind"] in KINDS, row["id"]
        for kind in row.get("accepted_kinds") or []:
            assert kind in KINDS, (row["id"], kind)


def test_no_blocking_row_is_unlabelled():
    """A blocking row with no expected answer gates nothing while looking
    like it does. Live captions are checked in UNLABELLED on purpose, and
    they are correspondingly non-blocking until an operator confirms each
    label from the caption text."""
    bad = [
        row["id"] for row in _rows()
        if row["blocking"] and not (row["expected_kind"] or row.get("accepted_kinds"))
    ]
    assert not bad, (
        f"blocking rows carry no expected answer: {bad}. Assign each "
        "expected_kind FROM THE CAPTION TEXT, or set blocking to false."
    )


@pytest.mark.parametrize("row_id", _PROTECTED)
def test_the_protected_half_is_present_and_gating(row_id):
    """§1a: one clause in the prompt keeps all of these, so if that clause is
    ever trimmed they fail together — but only while they are still in the
    fixture AND still blocking."""
    row = next((r for r in _rows() if r["id"] == row_id), None)
    assert row is not None, f"protected control {row_id} is missing from the fixture"
    assert row["blocking"], f"{row_id} is present but no longer gates"
    accepted = row.get("accepted_kinds") or [row["expected_kind"]]
    assert accepted == ["event"], (row_id, accepted)


@pytest.mark.parametrize("row_id", _SUPPRESSED)
def test_the_suppression_half_is_present_and_gating(row_id):
    """Deleting S9-S13 used to leave the fixture guard green, because it
    only listed the protected set — so the two live offenders and the
    'Especial do dia' regression could vanish silently (F5)."""
    row = next((r for r in _rows() if r["id"] == row_id), None)
    assert row is not None, f"suppression control {row_id} is missing from the fixture"
    assert row["blocking"], f"{row_id} is present but no longer gates"
    accepted = set(row.get("accepted_kinds") or [row["expected_kind"]])
    assert accepted and accepted <= {"menu", "promotion"}, (row_id, accepted)


def test_the_generalisation_controls_do_not_all_lean_on_enumerated_nouns():
    """F2: a control whose subject is a noun the prompt itself lists
    (`buffet`, `happy hour`, `especial do dia`, `rodizio`) evidences
    instruction-following, not that the rule GENERALISES. At least three
    blocking controls must turn on wording the prompt never names."""
    unnamed = [
        row["id"] for row in _rows()
        if row["blocking"] and row.get("names_a_prompt_noun") is False
    ]
    assert len(unnamed) >= 3, (
        f"only {len(unnamed)} blocking control(s) avoid the prompt's own "
        f"enumerated nouns: {unnamed}"
    )


def test_the_live_half_is_carried_unlabelled_and_ungated():
    """The live captions emitted from production ride along so the operator
    can confirm each label in one pass. Until they do, none of them may
    gate — a label nobody has confirmed is not evidence."""
    live = [row for row in _rows() if not row.get("synthetic")]
    assert live, "the live captions are missing from the fixture"
    for row in live:
        assert row["expected_kind"] is None, (
            f"{row['id']} has an expected_kind that no operator confirmed"
        )
        assert not row["blocking"], row["id"]
        assert row.get("proposed_kind"), (
            f"{row['id']} carries no proposed label, so confirming it would be "
            "a fresh labelling exercise rather than a review"
        )
