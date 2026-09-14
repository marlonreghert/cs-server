"""Unit tests for `scripts/review_venue_link_audit.py` —
plans/260914_agentic-venue-resolution-fallback.md §7.

The runner is the operator's only entry point into this pass, so the pieces
pinned here are the ones an operator's hands actually touch: the dry-run /
apply split, `--handle` scoping, `--consensus-k`, the report JSON's shape,
and the distinct exit code that says "the flag is off, nothing ran".

`build_service` is monkeypatched in every test that calls `main`, so nothing
here opens a database, a Redis connection or an OpenAI client — the real
`build_service` constructs all three.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

import scripts.review_venue_link_audit as runner
from app.services.venue_link_audit_reviewer import (
    DEFAULT_REVIEW_CONSENSUS_K,
    OUTCOME_CONFIRMED,
    OUTCOME_NO_CONSENSUS,
    PairOutcome,
)
from app.services.venue_link_audit_reviewer_validator import (
    FIELD_STREET,
    VERDICT_CONFIRM,
)

# Real, re-verified production values (plan §A/§E) — the same two pairs the
# sibling unit tests use.
_CONCHITTAS_TEXT = "Rua da Imperatriz Tereza Cristina, 218"


def _outcome(**overrides) -> dict:
    """A real `PairOutcome.to_dict()`, so this file cannot drift from the
    shape the service actually returns."""
    pair = PairOutcome(
        handle="conchittasbar", venue_id="conchittas_bar", outcome=OUTCOME_CONFIRMED,
        verdict=VERDICT_CONFIRM, evidence_quote=_CONCHITTAS_TEXT,
        matched_field=FIELD_STREET, reason="same street and number",
        tally={VERDICT_CONFIRM: 10}, checkable_count=28, non_corroborating_count=20,
        suppressed=True,
    )
    row = pair.to_dict()
    row.update(overrides)
    return row


def _result(**overrides) -> dict:
    result = {
        "outcomes": [_outcome()],
        "counts": {OUTCOME_CONFIRMED: 1},
        "enabled": True,
        "auto_apply": True,
        "dry_run": False,
        "consensus_k": DEFAULT_REVIEW_CONSENSUS_K,
    }
    result.update(overrides)
    return result


class _StubService:
    """Stands in for `VenueLinkAuditReviewerService`: the runner calls
    exactly one method on it."""

    def __init__(self, result):
        self._result = result
        self.runs: list = []

    async def run(self, *, handles=None, dry_run=False):
        self.runs.append({"handles": handles, "dry_run": dry_run})
        return self._result


def _patch_build_service(monkeypatch, result):
    """Replace `build_service` and record the kwargs it was called with."""
    service = _StubService(result)
    built: list = []

    def _fake_build_service(*, consensus_k=DEFAULT_REVIEW_CONSENSUS_K):
        built.append({"consensus_k": consensus_k})
        return service

    monkeypatch.setattr(runner, "build_service", _fake_build_service)
    return service, built


# ── report_to_dict ──────────────────────────────────────────────────────────

def test_report_to_dict_marks_an_apply_run():
    report = runner.report_to_dict(_result(), applied=True)
    assert report["mode"] == "apply"


def test_report_to_dict_marks_a_dry_run():
    """Dry-run is the DEFAULT and the mode an operator reads first, so the
    report must say so in its own right rather than leaving it to be
    inferred from an empty write count."""
    report = runner.report_to_dict(_result(dry_run=True), applied=False)
    assert report["mode"] == "dry_run"


def test_report_to_dict_carries_every_key_an_operator_reads():
    report = runner.report_to_dict(_result(), applied=True)
    assert set(report) == {
        "mode", "generated_at", "enabled", "auto_apply", "consensus_k",
        "counts", "outcomes",
    }
    assert report["enabled"] is True
    assert report["auto_apply"] is True
    assert report["consensus_k"] == DEFAULT_REVIEW_CONSENSUS_K
    assert report["counts"] == {OUTCOME_CONFIRMED: 1}
    assert report["outcomes"][0]["handle"] == "conchittasbar"
    assert report["outcomes"][0]["tally"] == {VERDICT_CONFIRM: 10}


def test_report_to_dict_of_a_run_that_never_started_defaults_safely():
    """The flag-off shape the service actually returns:
    `{"outcomes": [], "counts": {}, "enabled": False}` — no `auto_apply`,
    no `consensus_k`. The report must not raise on the missing keys and must
    not invent an `enabled: True`."""
    report = runner.report_to_dict(
        {"outcomes": [], "counts": {}, "enabled": False}, applied=False,
    )
    assert report["enabled"] is False
    assert report["auto_apply"] is False
    assert report["consensus_k"] is None
    assert report["counts"] == {}
    assert report["outcomes"] == []


def test_report_to_dict_stamps_a_parseable_utc_timestamp():
    report = runner.report_to_dict(_result(), applied=False)
    stamped = datetime.fromisoformat(report["generated_at"])
    assert stamped.tzinfo is not None, "the run report's timestamp must be tz-aware"


# ── write_report_json ───────────────────────────────────────────────────────

def test_write_report_json_round_trips(tmp_path):
    path = tmp_path / "review-report.json"
    runner.write_report_json(str(path), _result(), applied=True)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == runner.report_to_dict(_result(), applied=True) | {
        "generated_at": loaded["generated_at"],
    }
    assert loaded["mode"] == "apply"
    assert loaded["outcomes"][0]["evidence_quote"] == _CONCHITTAS_TEXT


def test_write_report_json_keeps_accented_evidence_readable(tmp_path):
    """`ensure_ascii=False`: the corpus is Brazilian street names, and a
    report full of `\\u00f3` escapes is one an operator cannot skim."""
    path = tmp_path / "report.json"
    runner.write_report_json(
        str(path),
        _result(outcomes=[_outcome(evidence_quote="R. Gregório Júnior, 264")]),
        applied=False,
    )
    assert "R. Gregório Júnior, 264" in path.read_text(encoding="utf-8")


# ── main ────────────────────────────────────────────────────────────────────

def test_main_returns_2_when_the_reviewer_flag_is_off(monkeypatch, capsys):
    """Exit code 2 is "nothing ran" — deliberately distinguishable from a
    run that found nothing to do (0), because the two call for opposite
    operator actions."""
    _patch_build_service(monkeypatch, {"outcomes": [], "counts": {}, "enabled": False})
    assert runner.main([]) == 2
    assert "venue_link_audit_reviewer_enabled is False" in capsys.readouterr().out


def test_main_returns_0_when_the_pass_ran(monkeypatch, capsys):
    _patch_build_service(monkeypatch, _result())
    assert runner.main([]) == 0
    out = capsys.readouterr().out
    assert "conchittasbar" in out
    assert OUTCOME_CONFIRMED in out


def test_main_returns_0_for_an_enabled_run_that_flagged_nothing(monkeypatch):
    _patch_build_service(monkeypatch, _result(outcomes=[], counts={}))
    assert runner.main([]) == 0


def test_main_is_dry_run_by_default(monkeypatch):
    """"Dry-run by default, and that is the point" — `--apply` is the
    deliberate second step."""
    service, _ = _patch_build_service(monkeypatch, _result(dry_run=True))
    runner.main([])
    assert service.runs == [{"handles": None, "dry_run": True}]


def test_main_apply_turns_the_write_on(monkeypatch, capsys):
    service, _ = _patch_build_service(monkeypatch, _result())
    runner.main(["--apply"])
    assert service.runs == [{"handles": None, "dry_run": False}]
    assert "mode: apply" in capsys.readouterr().out


def test_main_accepts_a_repeated_handle_flag(monkeypatch):
    service, _ = _patch_build_service(monkeypatch, _result())
    runner.main(["--handle", "conchittasbar", "--handle", "lacasarecife"])
    assert service.runs == [
        {"handles": ["conchittasbar", "lacasarecife"], "dry_run": True},
    ]


def test_main_accepts_a_single_handle(monkeypatch):
    service, _ = _patch_build_service(monkeypatch, _result())
    runner.main(["--handle", "lacasarecife", "--apply"])
    assert service.runs == [{"handles": ["lacasarecife"], "dry_run": False}]


def test_main_passes_consensus_k_through_to_the_service(monkeypatch):
    _, built = _patch_build_service(monkeypatch, _result(consensus_k=3))
    runner.main(["--consensus-k", "3"])
    assert built == [{"consensus_k": 3}]


def test_main_defaults_consensus_k_to_the_shipped_constant(monkeypatch):
    _, built = _patch_build_service(monkeypatch, _result())
    runner.main([])
    assert built == [{"consensus_k": DEFAULT_REVIEW_CONSENSUS_K}]


def test_main_rejects_a_non_integer_consensus_k(monkeypatch):
    _patch_build_service(monkeypatch, _result())
    with pytest.raises(SystemExit):
        runner.main(["--consensus-k", "ten"])


def test_main_writes_the_report_json(monkeypatch, tmp_path):
    _patch_build_service(monkeypatch, _result(outcomes=[
        _outcome(outcome=OUTCOME_NO_CONSENSUS, verdict=None, suppressed=False),
    ]))
    path = tmp_path / "out.json"
    assert runner.main(["--report-json", str(path)]) == 0
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["mode"] == "dry_run"
    assert report["outcomes"][0]["outcome"] == OUTCOME_NO_CONSENSUS


def test_main_writes_a_report_even_when_nothing_ran(monkeypatch, tmp_path):
    """A flag-off run is still a measurement an operator may want on disk —
    and the report must not be skipped just because the exit code is 2."""
    _patch_build_service(monkeypatch, {"outcomes": [], "counts": {}, "enabled": False})
    path = tmp_path / "off.json"
    assert runner.main(["--report-json", str(path)]) == 2
    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False


def test_main_prints_the_reason_and_the_quote_for_each_pair(monkeypatch, capsys):
    _patch_build_service(monkeypatch, _result())
    runner.main([])
    out = capsys.readouterr().out
    assert "same street and number" in out
    assert _CONCHITTAS_TEXT in out
