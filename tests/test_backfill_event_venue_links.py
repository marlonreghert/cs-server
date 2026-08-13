"""Unit tests for scripts/backfill_event_venue_links.py.

See plans/260812_backfill-misattributed-links.md's Test Plan.

`tests/bdd/enrichment/backfill-misattributed-links.feature` covers the same
behavior end-to-end through the real `run_backfill` against the in-memory RDS
fake; this file adds two more granular layers:
  - `decide_one` exercised as a PURE function (no DAO at all) for the
    per-status table, operator-edited-fields protection, and review-reason
    folding — fast, and immune to any fixture-wiring mistake in the DAO
    layer;
  - `run_backfill`/`check_balance` against `tests.rds_fake.InMemoryRdsVenueStore`
    for everything that genuinely needs persistence: the dependency guard,
    idempotency, collision detection, and the write-failure/arithmetic
    hard-stops.

Every assertion below names the specific venue/handle/reason involved, never
just a row count — plan requirement: "a count-based assertion already stayed
green in this repo against a deliberately reintroduced wrong-handle bug,
because both passes computed the same wrong number."
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_identity import compute_source_event_key
from app.services.event_reconciliation import (
    REVIEW_REASON_NEEDS_REVIEW,
    REVIEW_REASON_UNRESOLVED_VENUE,
    REVIEW_REASON_VENUE_NOT_IN_CATALOG,
    new_event_id,
)
from app.services.event_venue_resolution import (
    METHOD_CAPTION_HANDLE_MENTION,
    METHOD_HANDLE_MENTION,
    METHOD_NAME_MATCH,
    METHOD_VENUE_NOT_IN_CATALOG,
    RESOLUTION_AUTO,
    RESOLUTION_MANUAL,
    RESOLUTION_UNRESOLVED,
    VenueLite,
)
from scripts.backfill_event_venue_links import (
    ArithmeticImbalance,
    DependencyNotLanded,
    Report,
    SKIP_CONFIRMED,
    SKIP_EXTRACTION_FAILED,
    SKIP_MANUAL_LINK,
    SKIP_OPERATOR_EDITED_VENUE,
    SKIP_REJECTED,
    SKIP_SUPERSEDED,
    WriteAffectedNoRows,
    check_balance,
    decide_one,
    run_backfill,
)
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
_STARTS_AT = datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc)
_FLOOR = 0.55
_MARGIN = 0.08
_MIN_CONFIDENCE = 0.5

_ROCK_BAR = VenueLite(venue_id="v_rock", venue_name="Sempre Rock Bar")
_TEATRO = VenueLite(venue_id="v_teatro", venue_name="Teatro Riachuelo")
_VENUES = [_ROCK_BAR, _TEATRO]
_HANDLE_INDEX = {"semprerockbar": "v_rock", "teatroriachuelonatal": "v_teatro"}
_VENUE_NAMES_BY_ID = {v.venue_id: v.venue_name for v in _VENUES}


def _base_event(**overrides) -> dict:
    event = {
        "event_id": "evt_1", "venue_id": "v_teatro", "venue_name": "Teatro Riachuelo",
        "starts_at": _STARTS_AT, "title": "Festa", "status": "accepted",
        "review_reason": None, "location_resolution": RESOLUTION_AUTO,
        "location_confidence": 1.0, "linked_by": METHOD_HANDLE_MENTION,
        "linked_at": _NOW, "operator_edited_fields": None, "confidence": 0.9,
        "location_text": None, "source_handle": "oquetemhojeemnatal",
        "raw_extraction": {"location_text": "@semprerockbar"},
    }
    event.update(overrides)
    return event


def _decide(event: dict) -> "object":
    return decide_one(
        event, venues=_VENUES, handle_index=_HANDLE_INDEX, venue_names_by_id=_VENUE_NAMES_BY_ID,
        confidence_floor=_FLOOR, margin=_MARGIN, min_confidence=_MIN_CONFIDENCE, now=_NOW,
    )


# ── decide_one: per-status action table (plan §B) ────────────────────────────
class TestPerStatusActionTable:
    def test_accepted_row_repoints_to_the_venue_its_own_text_names(self):
        decision = _decide(_base_event())
        assert decision.action == "repoint"
        assert decision.new_venue_id == "v_rock"
        assert decision.new_linked_by == METHOD_HANDLE_MENTION

    def test_confirmed_row_is_skipped(self):
        decision = _decide(_base_event(status="confirmed"))
        assert decision.action == "skip"
        assert decision.skip_reason == SKIP_CONFIRMED

    def test_manually_linked_row_is_skipped_regardless_of_status(self):
        decision = _decide(_base_event(location_resolution=RESOLUTION_MANUAL))
        assert decision.action == "skip"
        assert decision.skip_reason == SKIP_MANUAL_LINK

    def test_superseded_row_is_skipped(self):
        decision = _decide(_base_event(status="superseded"))
        assert decision.action == "skip"
        assert decision.skip_reason == SKIP_SUPERSEDED

    def test_extraction_failed_row_is_skipped(self):
        decision = _decide(_base_event(status="extraction_failed"))
        assert decision.action == "skip"
        assert decision.skip_reason == SKIP_EXTRACTION_FAILED

    def test_rejected_row_is_skipped(self):
        decision = _decide(_base_event(status="rejected"))
        assert decision.action == "skip"
        assert decision.skip_reason == SKIP_REJECTED

    def test_pending_review_row_is_re_resolved(self):
        decision = _decide(_base_event(status="pending_review", review_reason=REVIEW_REASON_UNRESOLVED_VENUE))
        assert decision.action == "repoint"
        assert decision.new_venue_id == "v_rock"


# ── operator_edited_fields protection (plan §B) ──────────────────────────────
class TestOperatorEditedFields:
    def test_venue_id_edit_refuses(self):
        decision = _decide(_base_event(operator_edited_fields=["venue_id"]))
        assert decision.action == "skip"
        assert decision.skip_reason == SKIP_OPERATOR_EDITED_VENUE

    def test_location_text_edit_does_not_refuse_and_its_value_is_used(self):
        # The operator's correction lives on the event-level location_text
        # column, not raw_extraction — raw_extraction still names the WRONG
        # venue, proving the corrected value (not the frozen model answer)
        # drove this resolution.
        decision = _decide(_base_event(
            operator_edited_fields=["location_text"],
            location_text="@teatroriachuelonatal",
            raw_extraction={"location_text": "@semprerockbar"},
        ))
        assert decision.action == "unchanged"  # already Teatro Riachuelo, correctly re-confirmed
        assert decision.new_venue_id == "v_teatro"
        assert decision.new_location_resolution == RESOLUTION_AUTO


# ── §C: review-reason folding ────────────────────────────────────────────────
class TestReviewReasonFolding:
    def test_unresolved_venue_dropped_when_a_venue_is_found(self):
        decision = _decide(_base_event(status="pending_review", review_reason=REVIEW_REASON_UNRESOLVED_VENUE))
        assert decision.new_review_reason is None

    def test_missing_date_preserved_when_a_venue_is_found(self):
        decision = _decide(_base_event(status="pending_review", review_reason="missing_date", starts_at=None))
        assert decision.new_review_reason == "missing_date"

    def test_both_preserved_when_no_venue_is_found(self):
        decision = _decide(_base_event(
            status="pending_review", review_reason="missing_date", starts_at=None,
            raw_extraction={"location_text": None},
        ))
        assert decision.new_review_reason == "missing_date; " + REVIEW_REASON_UNRESOLVED_VENUE

    def test_venue_not_in_catalog_reason_never_duplicated_across_runs(self):
        decision = _decide(_base_event(
            status="pending_review", review_reason=REVIEW_REASON_VENUE_NOT_IN_CATALOG,
            raw_extraction={"location_text": "@donanapubnatal"},
        ))
        assert decision.new_review_reason == REVIEW_REASON_VENUE_NOT_IN_CATALOG
        assert decision.new_review_reason.count(REVIEW_REASON_VENUE_NOT_IN_CATALOG) == 1


# ── §C: status restoration through is_clean_extraction only ─────────────────
class TestStatusRestoration:
    def test_pending_row_returns_to_accepted_when_clean(self):
        decision = _decide(_base_event(
            status="pending_review", review_reason=REVIEW_REASON_UNRESOLVED_VENUE, confidence=0.9,
        ))
        assert decision.new_status == "accepted"
        assert decision.new_review_reason is None

    def test_pending_row_stays_pending_when_confidence_below_floor(self):
        decision = _decide(_base_event(
            status="pending_review", review_reason=REVIEW_REASON_UNRESOLVED_VENUE, confidence=0.1,
        ))
        assert decision.new_status == "pending_review"
        assert decision.new_review_reason == REVIEW_REASON_NEEDS_REVIEW

    def test_accepted_row_becomes_pending_when_repair_detaches_it(self):
        decision = _decide(_base_event(raw_extraction={"location_text": "@donanapubnatal"}))
        assert decision.action == "detach"
        assert decision.new_status == "pending_review"
        assert decision.new_review_reason == REVIEW_REASON_VENUE_NOT_IN_CATALOG


# ── venue_not_in_catalog vs generic unresolved (plan §C) ─────────────────────
class TestDetachReasons:
    def test_unrecognized_handle_yields_venue_not_in_catalog(self):
        decision = _decide(_base_event(raw_extraction={"location_text": "@donanapubnatal"}))
        assert decision.new_venue_id is None
        assert decision.resolution_method == METHOD_VENUE_NOT_IN_CATALOG
        assert decision.new_review_reason == REVIEW_REASON_VENUE_NOT_IN_CATALOG
        assert decision.unrecognized_handle == "donanapubnatal"

    def test_no_location_text_yields_generic_unresolved(self):
        decision = _decide(_base_event(raw_extraction={"location_text": None}))
        assert decision.new_venue_id is None
        assert decision.new_review_reason == REVIEW_REASON_UNRESOLVED_VENUE

    def test_an_ambiguous_rung4_match_detaches_as_generic_unresolved_never_auto_linked(self):
        # Two DIFFERENT venues sharing the exact same name -> rung 4 finds
        # two candidates tied at score 1.0, clearing the floor but failing
        # the margin gate -> RESOLUTION_QUEUED. This script must never
        # silently accept an ambiguous match (it writes no link candidates
        # for a human to pick from) -- it detaches honestly, generic
        # unresolved_venue, never venue_not_in_catalog (that reason is
        # reserved for a SPECIFIC unrecognized handle, not an ambiguous name).
        tied_a = VenueLite(venue_id="v_tied_a", venue_name="Bar Duplicado")
        tied_b = VenueLite(venue_id="v_tied_b", venue_name="Bar Duplicado")
        decision = decide_one(
            _base_event(raw_extraction={"location_text": "Bar Duplicado"}),
            venues=[tied_a, tied_b], handle_index={}, venue_names_by_id={},
            confidence_floor=_FLOOR, margin=_MARGIN, min_confidence=_MIN_CONFIDENCE, now=_NOW,
        )
        assert decision.action == "detach"
        assert decision.new_venue_id is None
        assert decision.new_review_reason == REVIEW_REASON_UNRESOLVED_VENUE

    def test_this_ladder_never_produces_name_match_for_an_at_mention_only_text(self):
        # The exact regression the task brief names: post-fix, a handle-only
        # location_text must never reach rung 4's fuzzy scorer.
        decision = _decide(_base_event(raw_extraction={"location_text": "@donanapubnatal"}))
        assert decision.resolution_method != METHOD_NAME_MATCH


# ── idempotency at the decide_one level ──────────────────────────────────────
def test_re_deciding_an_already_repaired_row_is_a_true_no_op():
    repaired = _base_event(venue_id="v_rock", venue_name="Sempre Rock Bar", linked_by=METHOD_HANDLE_MENTION)
    decision = _decide(repaired)
    assert decision.action == "unchanged"


# ══════════════════════════════════════════════════════════════════════════
# DAO-level tests: run_backfill / check_balance against the in-memory fake
# ══════════════════════════════════════════════════════════════════════════
def _dao() -> VenueRepository:
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


def _add_venue(dao, name, handle=None, venue_id=None):
    vid = venue_id or f"v_{(handle or name).lower().replace(' ', '_')}"
    dao.upsert_venue(Venue(venue_id=vid, venue_name=name, venue_lat=-8.05, venue_lng=-34.88, venue_address=""))
    if handle:
        dao.set_venue_instagram(VenueInstagram(venue_id=vid, instagram_handle=handle, status="found"))
    return vid


def _insert(dao, *, venue_id, location_text, title="Festa", shortcode=None, **overrides):
    event_id = overrides.pop("event_id", None) or new_event_id()
    shortcode = shortcode or f"post_{event_id}"
    starts_at = overrides.pop("starts_at", _STARTS_AT)
    fields = {
        "event_id": event_id, "venue_id": venue_id, "starts_at": starts_at, "title": title,
        "status": "accepted", "review_reason": None, "location_resolution": RESOLUTION_AUTO,
        "location_confidence": 1.0, "linked_by": METHOD_HANDLE_MENTION, "linked_at": _NOW,
        "operator_edited_fields": None, "confidence": 0.9, "location_text": None,
        "source_kind": "promoter_post", "source_handle": "oquetemhojeemnatal",
        "source_shortcode": shortcode, "source_permalink": f"https://instagram.com/p/{shortcode}",
        "source_event_key": compute_source_event_key(title, starts_at), "source_event_index": 1,
        "raw_extraction": {"location_text": location_text},
        "first_seen_at": _NOW, "last_seen_at": _NOW,
    }
    fields.update(overrides)
    dao.insert_event(fields)
    return event_id


class TestDependencyGuard:
    def test_refuses_before_reading_any_row_when_the_forward_fix_is_missing(self, monkeypatch):
        import app.services.event_venue_resolution as evr

        monkeypatch.delattr(evr, "METHOD_CAPTION_HANDLE_MENTION")
        dao = _dao()

        def _boom(*a, **kw):
            raise AssertionError("run_backfill read events before the dependency guard fired")

        monkeypatch.setattr(dao.rds_store, "list_events", _boom)

        with pytest.raises(DependencyNotLanded):
            run_backfill(dao, apply=False)


class TestSelection:
    def test_selects_exactly_the_handle_mention_population(self):
        dao = _dao()
        wrong_venue = _add_venue(dao, "Teatro Riachuelo", "teatroriachuelonatal")
        _add_venue(dao, "Sempre Rock Bar", "semprerockbar")

        target = _insert(dao, venue_id=wrong_venue, location_text="@semprerockbar", title="A")
        # NOT selected: a genuine per-event handle mention from the FIXED
        # ladder is also linked_by=handle_mention — this one is already
        # correct and must be counted "unchanged", never miscounted as
        # something else's population.
        already_correct = _insert(
            dao, venue_id=wrong_venue, location_text="@teatroriachuelonatal", title="B",
        )
        caption_mention = _insert(
            dao, venue_id=wrong_venue, location_text=None, title="C",
            linked_by=METHOD_CAPTION_HANDLE_MENTION,
        )
        name_match = _insert(
            dao, venue_id=wrong_venue, location_text=None, title="D", linked_by=METHOD_NAME_MATCH,
        )

        report = run_backfill(dao, apply=False, now=_NOW)
        assert report.selected == 2  # target + already_correct
        touched_ids = {d.event_id for d in report.rows}
        assert target in touched_ids
        assert already_correct in touched_ids
        assert caption_mention not in touched_ids
        assert name_match not in touched_ids


class TestArithmeticBalance:
    def test_a_realistic_mixed_run_balances_and_does_not_raise(self):
        dao = _dao()
        rock = _add_venue(dao, "Sempre Rock Bar", "semprerockbar")
        teatro = _add_venue(dao, "Teatro Riachuelo", "teatroriachuelonatal")
        _insert(dao, venue_id=teatro, location_text="@semprerockbar", title="repoint")
        _insert(dao, venue_id=rock, location_text="@donanapubnatal", title="detach")
        _insert(dao, venue_id=teatro, location_text="@teatroriachuelonatal", title="unchanged")
        _insert(dao, venue_id=teatro, location_text="@semprerockbar", title="confirmed", status="confirmed")

        report = run_backfill(dao, apply=True, now=_NOW)  # must not raise
        assert report.balanced is True
        assert report.repointed == 1
        assert report.detached == 1
        assert report.unchanged == 1
        assert report.skipped_by_reason[SKIP_CONFIRMED] == 1

    def test_fabricated_imbalance_raises_and_names_it(self):
        report = Report(selected=5, repointed=1, detached=1, unchanged=1)
        with pytest.raises(ArithmeticImbalance, match="did not balance"):
            check_balance(report)


class TestWriteFailure:
    def test_zero_row_update_is_a_hard_stop_naming_the_event(self, monkeypatch):
        dao = _dao()
        rock = _add_venue(dao, "Sempre Rock Bar", "semprerockbar")
        teatro = _add_venue(dao, "Teatro Riachuelo", "teatroriachuelonatal")
        event_id = _insert(dao, venue_id=teatro, location_text="@semprerockbar")

        monkeypatch.setattr(dao.rds_store, "update_event", lambda *a, **kw: None)

        with pytest.raises(WriteAffectedNoRows) as excinfo:
            run_backfill(dao, apply=True, now=_NOW)
        assert excinfo.value.event_id == event_id


class TestIdempotency:
    def test_second_apply_pass_changes_nothing(self):
        dao = _dao()
        rock = _add_venue(dao, "Sempre Rock Bar", "semprerockbar")
        teatro = _add_venue(dao, "Teatro Riachuelo", "teatroriachuelonatal")
        event_id = _insert(dao, venue_id=teatro, location_text="@semprerockbar")

        first = run_backfill(dao, apply=True, now=_NOW)
        assert first.repointed == 1
        row = dao.get_event(event_id)
        assert row["venue_id"] == rock

        second = run_backfill(dao, apply=True, now=_NOW)
        assert second.changed_count == 0
        assert second.unchanged == 1
        row_again = dao.get_event(event_id)
        assert row_again["venue_id"] == rock
        assert row_again["linked_at"] == row["linked_at"]  # never rewritten on a no-op


class TestCollisionDetection:
    def test_two_repaired_rows_landing_on_the_same_identity_are_both_written_and_reported(self):
        dao = _dao()
        rock = _add_venue(dao, "Sempre Rock Bar", "semprerockbar")
        teatro = _add_venue(dao, "Teatro Riachuelo", "teatroriachuelonatal")
        shared_title = "Noite Compartilhada"
        a = _insert(dao, venue_id=teatro, location_text="@semprerockbar", title=shared_title, shortcode="p1")
        b = _insert(dao, venue_id=teatro, location_text="@semprerockbar", title=shared_title, shortcode="p2")

        report = run_backfill(dao, apply=True, now=_NOW)
        assert dao.get_event(a)["venue_id"] == rock
        assert dao.get_event(b)["venue_id"] == rock
        assert dao.get_event(a) is not None and dao.get_event(b) is not None  # never merged/deleted

        collided_ids = {eid for _identity, ids in report.venue_identity_collisions for eid in ids}
        assert {a, b} <= collided_ids
        matching = [ids for identity, ids in report.venue_identity_collisions if identity[0] == rock]
        assert matching, report.venue_identity_collisions

    def test_a_detached_row_reports_a_handle_identity_collision_with_a_resolved_sibling(self):
        dao = _dao()
        teatro = _add_venue(dao, "Teatro Riachuelo", "teatroriachuelonatal")
        sibling_venue = _add_venue(dao, "Sibling Venue", "siblingvenuehandle")
        shared_title = "Programacao da Semana"
        detached = _insert(
            dao, venue_id=teatro, location_text="@donanapubnatal", title=shared_title, shortcode="p1",
        )
        sibling = _insert(
            dao, venue_id=sibling_venue, location_text="Sibling Venue", title=shared_title, shortcode="p2",
            linked_by=METHOD_NAME_MATCH,
        )

        report = run_backfill(dao, apply=True, now=_NOW)
        assert dao.get_event(detached)["venue_id"] is None
        pairs = {(d, s) for d, s, _identity in report.handle_identity_collisions}
        assert (detached, sibling) in pairs


class TestVenueAcquisitionBacklog:
    def test_backlog_is_ranked_by_named_handle(self):
        dao = _dao()
        wrong_venue = _add_venue(dao, "Wrong Venue", "wrongvenue")
        for i in range(3):
            _insert(dao, venue_id=wrong_venue, location_text="@popularvenue", title=f"pop{i}")
        _insert(dao, venue_id=wrong_venue, location_text="@rarevenue", title="rare")

        report = run_backfill(dao, apply=False, now=_NOW)
        backlog = dict(report.venue_acquisition_backlog)
        assert backlog["popularvenue"] == 3
        assert backlog["rarevenue"] == 1
        counts = [c for _h, c in report.venue_acquisition_backlog]
        assert counts == sorted(counts, reverse=True)
        assert report.venue_acquisition_backlog[0][0] == "popularvenue"


class TestExpectedPostFixShape:
    """The task brief's own regression pin: a live post-fix promoter crawl
    produced "0 name_match links, 16 venue_not_in_catalog, 4 exact
    handle_mention" — not the old shape (name_match links from fuzzy-scoring
    an @handle's characters). This is the SAME shape this backfill's dry-run
    report must show for a corpus built the same way."""

    def test_no_row_in_a_realistic_mix_resolves_via_name_match(self):
        dao = _dao()
        wrong_venue = _add_venue(dao, "Wrong Venue", "wrongvenue")
        _add_venue(dao, "Sempre Rock Bar", "semprerockbar")
        _add_venue(dao, "Casa do Matuto", "casadomatutonatal")

        _insert(dao, venue_id=wrong_venue, location_text="@semprerockbar", title="a")
        _insert(dao, venue_id=wrong_venue, location_text="@casadomatutonatal", title="b")
        for i in range(3):
            _insert(dao, venue_id=wrong_venue, location_text=f"@unknownvenue{i}", title=f"c{i}")

        report = run_backfill(dao, apply=False, now=_NOW)
        assert report.repointed == 2
        assert report.detached_venue_not_in_catalog == 3
        assert METHOD_NAME_MATCH not in report.after_linked_by
