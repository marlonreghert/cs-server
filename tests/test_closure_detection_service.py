"""Unit tests for closure detection.

BDD covers the serving outcome (a flagged venue leaves the projection). These
tests protect the evaluator's edge cases: phrase matching under accents and
casing, negation/temporary-closure rejection, recency ordering, unorderable
evidence, confidence assignment, and per-venue isolation in the run loop.
"""
from __future__ import annotations

import pytest

from app.services.closure_detection_service import (
    DEFAULT_CONTRADICTION_WINDOW_DAYS,
    ClosureDetectionService,
    ClosureSignal,
    evaluate_reviews,
)


def _review(text, publish_time, rating=5):
    return {
        "author_name": "R",
        "rating": rating,
        "text": text,
        "relative_time": "x",
        "publish_time": publish_time,
    }


# ── phrase matching ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "ESSE BAR FECHOU",
        "esse bar fechou",
        "o bar não existe mais",
        "nao existe mais",
        "encerrou as atividades no ano passado",
        "fechado permanentemente",
        "esse lugar não funciona mais",
    ],
)
def test_closure_phrases_match_regardless_of_case_and_accents(text):
    signal = evaluate_reviews("v1", [_review(text, "2026-06-01T00:00:00Z")])
    assert signal.closed is True
    assert signal.confidence == "high"
    assert signal.matched_phrase


@pytest.mark.parametrize(
    "text",
    [
        "fechado hoje, voltamos amanha",
        "fechado para reforma ate dezembro",
        "acho que vai fechar em breve",
        "quase fechou na pandemia mas continua otimo",
        "não fechou, segue funcionando",
        "fechou para ferias",
        "fechou e reabriu reformado",
    ],
)
def test_temporary_or_speculative_phrases_do_not_close(text):
    signal = evaluate_reviews("v1", [_review(text, "2026-06-01T00:00:00Z")])
    assert signal.closed is False, f"{text!r} must not be read as permanent closure"


def test_ordinary_review_is_not_closure():
    signal = evaluate_reviews(
        "v1", [_review("cerveja gelada, otimo atendimento", "2026-06-01T00:00:00Z")]
    )
    assert signal.closed is False
    assert signal.matched_phrase is None


# ── recency ordering ──────────────────────────────────────────────────────────
def test_only_the_newest_review_decides_closure():
    """A closure phrase in history is stale gossip — venues reopen."""
    signal = evaluate_reviews(
        "v1",
        [
            _review("esse bar fechou", "2021-03-02T00:00:00Z"),
            _review("cerveja gelada", "2026-05-30T00:00:00Z"),
        ],
    )
    assert signal.closed is False


def test_newest_closure_review_wins_over_older_ordinary_reviews():
    signal = evaluate_reviews(
        "v1",
        [
            _review("otimo lugar", "2023-05-08T00:00:00Z"),
            _review("ESSE BAR FECHOU", "2026-01-12T00:00:00Z"),
            _review("bar descolado", "2021-08-03T00:00:00Z"),
        ],
    )
    assert signal.closed is True
    assert signal.confidence == "high"
    assert signal.evidence_publish_time == "2026-01-12T00:00:00Z"


def test_review_order_in_the_payload_does_not_matter():
    ordered = evaluate_reviews(
        "v1",
        [
            _review("esse bar fechou", "2026-01-12T00:00:00Z"),
            _review("otimo lugar", "2023-05-08T00:00:00Z"),
        ],
    )
    reversed_ = evaluate_reviews(
        "v1",
        [
            _review("otimo lugar", "2023-05-08T00:00:00Z"),
            _review("esse bar fechou", "2026-01-12T00:00:00Z"),
        ],
    )
    assert ordered.closed == reversed_.closed is True
    assert ordered.evidence_publish_time == reversed_.evidence_publish_time


# ── confidence ────────────────────────────────────────────────────────────────
def test_contradicting_recent_review_downgrades_to_low_confidence():
    signal = evaluate_reviews(
        "v1",
        [
            _review("esse bar fechou", "2026-06-02T00:00:00Z"),
            _review("estivemos ontem, tudo funcionando", "2026-06-01T00:00:00Z"),
        ],
    )
    assert signal.closed is True
    assert signal.confidence == "low"
    assert signal.excludes_from_serving() is False


def test_old_contradicting_review_leaves_confidence_high():
    """Outside the contradiction window the older review says nothing about now."""
    signal = evaluate_reviews(
        "v1",
        [
            _review("esse bar fechou", "2026-06-02T00:00:00Z"),
            _review("tudo funcionando", "2024-01-01T00:00:00Z"),
        ],
    )
    assert signal.confidence == "high"
    assert signal.excludes_from_serving() is True


def test_contradiction_window_is_configurable():
    reviews = [
        _review("esse bar fechou", "2026-06-02T00:00:00Z"),
        _review("tudo funcionando", "2026-01-01T00:00:00Z"),
    ]
    assert evaluate_reviews("v1", reviews).confidence == "high"
    assert (
        evaluate_reviews("v1", reviews, contradiction_window_days=365).confidence
        == "low"
    )


def test_only_high_confidence_excludes_from_serving():
    assert ClosureSignal("v", closed=True, confidence="high").excludes_from_serving()
    assert not ClosureSignal("v", closed=True, confidence="low").excludes_from_serving()
    assert not ClosureSignal("v", closed=False, confidence="high").excludes_from_serving()


# ── unorderable / absent evidence ─────────────────────────────────────────────
def test_no_reviews_never_closes():
    assert evaluate_reviews("v1", []).closed is False


def test_reviews_without_publish_time_never_close():
    """Closure depends on recency; an undatable review cannot establish it."""
    signal = evaluate_reviews("v1", [_review("esse bar fechou", None)])
    assert signal.closed is False


def test_unparseable_publish_time_is_ignored():
    signal = evaluate_reviews("v1", [_review("esse bar fechou", "not-a-date")])
    assert signal.closed is False


def test_datable_closure_beats_undatable_noise():
    signal = evaluate_reviews(
        "v1",
        [
            _review("tudo otimo", None),
            _review("esse bar fechou", "2026-06-01T00:00:00Z"),
        ],
    )
    assert signal.closed is True


def test_over_precise_fractional_seconds_parse():
    """Google emits 9-digit fractional seconds; they must not break ordering."""
    signal = evaluate_reviews(
        "v1",
        [
            _review("esse bar fechou", "2026-01-02T17:37:26.579401739Z"),
            _review("otimo", "2023-01-01T00:00:00Z"),
        ],
    )
    assert signal.closed is True


def test_malformed_payload_raises_for_the_caller_to_isolate():
    with pytest.raises(TypeError):
        evaluate_reviews("v1", "not-a-list")


# ── run loop ──────────────────────────────────────────────────────────────────
class _Store:
    def __init__(self, reviews_by_venue, fail_ids=()):
        self._reviews = reviews_by_venue
        self._fail = set(fail_ids)
        self.signals = {}

    def list_active_venue_ids(self):
        return list(self._reviews)

    def get_enrichment_bulk(self, table_key, venue_ids):
        return {
            vid: ({"reviews": "boom"} if vid in self._fail
                  else {"reviews": self._reviews[vid]})
            for vid in venue_ids
        }

    def get_closure_signal(self, venue_id):
        return self.signals.get(venue_id)

    def set_closure_signal(self, venue_id, signal):
        self.signals[venue_id] = signal

    def clear_closure_signal(self, venue_id):
        self.signals.pop(venue_id, None)

    def list_closure_signals(self):
        return list(self.signals.values())


def _service(store, enabled=True):
    return ClosureDetectionService(rds_store=store, enabled=lambda: enabled)


def test_run_flags_closed_and_leaves_others_alone():
    store = _Store(
        {
            "closed_bar": [_review("esse bar fechou", "2026-06-01T00:00:00Z")],
            "open_bar": [_review("cerveja gelada", "2026-06-01T00:00:00Z")],
        }
    )
    summary = _service(store).run()
    assert summary["flagged"] == 1
    assert store.signals["closed_bar"]["closed"] is True
    assert "open_bar" not in store.signals


def test_run_is_idempotent():
    store = _Store({"v": [_review("esse bar fechou", "2026-06-01T00:00:00Z")]})
    service = _service(store)
    first = service.run()
    second = service.run()
    assert first["flagged"] == 1
    # Already flagged — a re-run must not double-count it.
    assert second["flagged"] == 0
    assert store.signals["v"]["closed"] is True


def test_run_clears_a_stale_signal_when_newer_evidence_appears():
    store = _Store({"v": [_review("esse bar fechou", "2026-01-01T00:00:00Z")]})
    service = _service(store)
    service.run()
    assert "v" in store.signals

    store._reviews["v"].append(_review("reabriu, casa cheia", "2026-07-20T00:00:00Z"))
    summary = service.run()
    assert summary["cleared"] == 1
    assert "v" not in store.signals


def test_disabled_service_evaluates_nothing():
    store = _Store({"v": [_review("esse bar fechou", "2026-06-01T00:00:00Z")]})
    summary = _service(store, enabled=False).run()
    assert summary["skipped"] == "disabled"
    assert store.signals == {}


def test_a_poisoned_venue_does_not_abort_the_run():
    store = _Store(
        {
            "broken": [],
            "closed_bar": [_review("esse bar fechou", "2026-06-01T00:00:00Z")],
        },
        fail_ids=["broken"],
    )
    summary = _service(store).run()
    assert summary["errors"] == 1
    assert "broken" in summary["error_venues"]
    # The healthy venue was still evaluated and flagged.
    assert store.signals["closed_bar"]["closed"] is True


def test_run_never_raises_when_the_store_read_fails():
    class _Broken(_Store):
        def list_active_venue_ids(self):
            raise RuntimeError("db down")

    summary = _service(_Broken({})).run()
    assert summary["errors"] == 1
    assert summary["flagged"] == 0
