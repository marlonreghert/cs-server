"""Unit tests for app/services/event_reconciliation.py, driven directly
(neither through EventExtractionService nor PromoterCrawlService).

See plans/260806_venue-post-multi-event.md: this module is the ONE
reconciliation implementation both the venue and promoter extraction paths
call. Its own test suite proves the mechanics shared between them —
identity by content-derived key, confirmed/manual preservation, upsert,
supersession, the divergence flag, and the two attribution strategies —
each asserted once here rather than once per caller. BDD
(instagram-event-extraction.feature, multi-event-posts.feature,
venue-post-multi-event.feature) covers the end-to-end scenarios through
each real service; this file protects the lower-level rules that back them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.dao.venue_repository import VenueRepository
from app.services.event_reconciliation import (
    REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION,
    REVIEW_REASON_DIVERGES_FROM_CONFIRMED,
    STATUS_ACCEPTED,
    STATUS_CONFIRMED,
    STATUS_PENDING_REVIEW,
    STATUS_SUPERSEDED,
    is_clean_extraction,
    reconcile_post_events,
)
from tests.rds_fake import InMemoryRdsVenueStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _dao() -> VenueRepository:
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


def _event(title: str, starts_at, **overrides) -> dict:
    base = {
        "starts_at": starts_at, "ends_at": None, "is_recurring": False,
        "recurrence_text": None, "title": title, "description": None,
        "lineup": [], "ticket_url": None, "price_text": None,
        "location_text": None, "confidence": 0.9, "review_reason": None,
        "raw_extraction": {"title": title},
    }
    base.update(overrides)
    return base


def _venue_attribute(venue_id: str):
    # attribute() returns (fields_to_merge, on_persisted) — see
    # app.services.event_reconciliation.AttributeFn. The venue strategy has
    # no side effect to defer, so on_persisted is always None.
    def attribute(fields: dict, event_id: str) -> tuple[dict, None]:
        return {"venue_id": venue_id}, None
    return attribute


def _rows(dao, handle, shortcode) -> list[dict]:
    return dao.list_events_by_source(handle, shortcode)


class TestFreshInsertAndIdempotentReorder:
    def test_a_fresh_run_inserts_one_row_per_event(self):
        dao = _dao()
        events = [
            _event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc)),
            _event("Event B", datetime(2026, 8, 11, tzinfo=timezone.utc)),
        ]
        persisted = reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink="https://x/s1",
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        assert persisted == 2
        rows = _rows(dao, "h1", "s1")
        assert len(rows) == 2
        assert {r["title"] for r in rows} == {"Event A", "Event B"}
        assert {r["source_event_index"] for r in rows} == {1, 2}
        # Both events are clean per is_clean_extraction (no review reason, a
        # resolved date, a linked venue, confidence 0.9 >= the 0.5 default
        # floor) — plans/260807_auto-accept-and-field-level-protection.md
        # auto-accepts them; a dirtier fixture is what TestAutoAccept below
        # pins staying pending_review.
        assert all(r["status"] == STATUS_ACCEPTED for r in rows)
        assert all(r["source_event_key"] is not None for r in rows)

    def test_reordering_the_same_events_creates_no_duplicates(self):
        dao = _dao()
        events = [
            _event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc)),
            _event("Event B", datetime(2026, 8, 11, tzinfo=timezone.utc)),
        ]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        first_ids = {r["event_id"] for r in _rows(dao, "h1", "s1")}

        reordered = list(reversed(events))
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=reordered, now=NOW, attribute=_venue_attribute("v1"),
        )
        rows = _rows(dao, "h1", "s1")
        assert len(rows) == 2
        assert {r["event_id"] for r in rows} == first_ids


class TestSupersession:
    def test_a_key_this_run_did_not_return_is_superseded_not_deleted(self):
        dao = _dao()
        events = [
            _event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc)),
            _event("Event B", datetime(2026, 8, 11, tzinfo=timezone.utc)),
        ]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        b_id = next(r["event_id"] for r in _rows(dao, "h1", "s1") if r["title"] == "Event B")

        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=[events[0]], now=NOW, attribute=_venue_attribute("v1"),
        )
        row = dao.get_event(b_id)
        assert row is not None
        assert row["status"] == STATUS_SUPERSEDED

    def test_a_confirmed_row_is_never_superseded_when_it_disappears(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})

        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=[], now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(row["event_id"])
        assert after["status"] == STATUS_CONFIRMED

    def test_a_manually_linked_row_is_never_superseded_when_it_disappears(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v_auto"),
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {
            "venue_id": "v_manual", "location_resolution": "manual",
            "linked_by": "operator_x", "linked_at": NOW,
        })

        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=[], now=NOW, attribute=_venue_attribute("v_auto"),
        )
        after = dao.get_event(row["event_id"])
        assert after["location_resolution"] == "manual"
        assert after["venue_id"] == "v_manual"


class TestConfirmedPreservationAndDivergence:
    def test_confirmed_row_survives_unchanged_when_the_model_agrees(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        # Confirmed AS-IS — no operator edit — so a re-extraction with the
        # model's SAME answer must not be read as a divergence.
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})

        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(row["event_id"])
        assert after["status"] == STATUS_CONFIRMED
        assert after["title"] == "Event A"
        assert after["review_reason"] is None

    def test_confirmed_row_flags_divergence_when_the_model_never_agreed_with_the_operators_edit(self):
        """Mirrors the already-passing 'Preserve an operator's confirmed
        event on re-extraction' BDD scenario: an operator's correction is a
        deliberate departure from the model's own answer, so as long as the
        model keeps saying its original (uncorrected) title, that IS a
        genuine, standing divergence — flagged every run, never silently
        dropped just because it was already flagged once."""
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED, "title": "Corrected"})

        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(row["event_id"])
        assert after["status"] == STATUS_CONFIRMED
        assert after["title"] == "Corrected"
        assert after["review_reason"] == REVIEW_REASON_DIVERGES_FROM_CONFIRMED

    def test_confirmed_row_flags_divergence_when_the_models_title_changes(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})

        diverged = [_event("Event A Renamed", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=diverged, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(row["event_id"])
        assert after["status"] == STATUS_CONFIRMED
        assert after["review_reason"] == REVIEW_REASON_DIVERGES_FROM_CONFIRMED
        # The row's own title (the operator's record) is untouched; only
        # raw_extraction/last_seen_at (and the key, kept in step) move.
        assert after["title"] == "Event A"
        assert after["raw_extraction"]["title"] == "Event A Renamed"

    def test_confirmed_row_flags_divergence_when_the_models_date_changes(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})

        diverged = [_event("Event A", datetime(2026, 8, 20, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=diverged, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(row["event_id"])
        assert after["status"] == STATUS_CONFIRMED
        assert after["review_reason"] == REVIEW_REASON_DIVERGES_FROM_CONFIRMED
        assert after["starts_at"].date().isoformat() == "2026-08-10"

    def test_a_confirmed_event_genuinely_replaced_is_inserted_not_absorbed(self):
        """The exact scenario a review caught: pairing on cardinality ALONE
        ("exactly one orphaned protected row, exactly one unmatched fresh
        event") is not enough — a confirmed event genuinely REPLACED by an
        unrelated one is ALSO "one orphaned, one unmatched". Both the title
        AND the resolved date must change together for this to be
        indistinguishable from "the same event moved" vs "a different event
        arrived" — so when BOTH change, they must NOT be paired: the
        confirmed row is left alone (untouched, not superseded — it is
        already exempt) and the new event is inserted as its own row rather
        than silently absorbed and lost."""
        dao = _dao()
        confirmed_event = [_event(
            "Baile da Metropole", datetime(2026, 8, 7, 22, tzinfo=timezone.utc),
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="metropolerecife",
            source_shortcode="ABC", source_permalink="https://ig/p/ABC",
            prepared_events=confirmed_event, now=NOW, attribute=_venue_attribute("ven_x"),
        )
        row = _rows(dao, "metropolerecife", "ABC")[0]
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})

        # An unrelated event: different title AND a different date two
        # weeks later — nothing about it suggests "Baile da Metropole moved".
        unrelated_event = [_event(
            "Noite do Forro", datetime(2026, 8, 21, 22, tzinfo=timezone.utc),
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="metropolerecife",
            source_shortcode="ABC", source_permalink="https://ig/p/ABC",
            prepared_events=unrelated_event, now=NOW, attribute=_venue_attribute("ven_x"),
        )

        rows = _rows(dao, "metropolerecife", "ABC")
        assert len(rows) == 2, rows
        by_title = {r["title"]: r for r in rows}

        confirmed = by_title["Baile da Metropole"]
        assert confirmed["event_id"] == row["event_id"]
        assert confirmed["status"] == STATUS_CONFIRMED
        # NOT flagged as diverging: this run's fresh event was never treated
        # as ITS divergent answer — it is a different event entirely. It IS
        # marked absent-from-this-run so an operator can see the post no
        # longer yields their confirmed event, rather than silence.
        assert confirmed["review_reason"] == REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION

        new_row = by_title["Noite do Forro"]
        # Clean per is_clean_extraction (no review reason, resolved date,
        # linked venue, default confidence 0.9) — auto-accepted, not queued.
        assert new_row["status"] == STATUS_ACCEPTED
        assert new_row["starts_at"].date().isoformat() == "2026-08-21"

    def test_attribute_is_never_invoked_for_a_confirmed_row(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        calls = []

        def tracking_attribute(fields, event_id):
            calls.append(fields["title"])
            return {"venue_id": "v1"}, None

        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=tracking_attribute,
        )
        assert calls == ["Event A"]
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})

        calls.clear()
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=tracking_attribute,
        )
        assert calls == []

    def test_two_simultaneously_diverging_confirmed_rows_are_not_guessed_at(self):
        """The fallback pairing that lets a single diverged confirmed row
        still be matched (its content-derived key necessarily changed) is
        deliberately restricted to the UNAMBIGUOUS case. With two orphaned
        confirmed rows and two unmatched fresh events in the same run, there
        is no principled way to guess which fresh event corresponds to
        which — so neither is touched, and both fresh events are inserted
        as new rows instead of risking a wrong pairing."""
        dao = _dao()
        events = [
            _event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc)),
            _event("Event B", datetime(2026, 8, 11, tzinfo=timezone.utc)),
        ]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        for row in _rows(dao, "h1", "s1"):
            dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})
        confirmed_ids = {r["event_id"] for r in _rows(dao, "h1", "s1")}

        diverged = [
            _event("Event A Renamed", datetime(2026, 8, 10, tzinfo=timezone.utc)),
            _event("Event B Renamed", datetime(2026, 8, 11, tzinfo=timezone.utc)),
        ]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=diverged, now=NOW, attribute=_venue_attribute("v1"),
        )
        rows = _rows(dao, "h1", "s1")
        # Both original confirmed rows survive, untouched, PLUS two new
        # auto-accepted rows for the renamed events — never a guessed pairing.
        assert len(rows) == 4
        still_confirmed = {r["event_id"] for r in rows if r["status"] == STATUS_CONFIRMED}
        assert still_confirmed == confirmed_ids
        for row in rows:
            if row["event_id"] in confirmed_ids:
                assert row["title"] in ("Event A", "Event B")
                # Orphaned (ambiguous cardinality, correctly not guessed at)
                # — marked absent-from-this-run, same as the unambiguous
                # "genuinely replaced" case; not a fabricated divergence.
                assert row["review_reason"] == REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION
            else:
                # Clean per is_clean_extraction — auto-accepted, not queued.
                assert row["status"] == STATUS_ACCEPTED
                assert row["title"] in ("Event A Renamed", "Event B Renamed")


class TestManualLinkPreservation:
    def test_manual_link_columns_are_preserved_but_content_still_refreshes(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]

        def promoter_attribute(fields, event_id):
            return {"venue_id": "v_auto", "location_resolution": "auto"}, None

        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=promoter_attribute,
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {
            "venue_id": "v_manual", "location_resolution": "manual",
            "linked_by": "operator_x", "linked_at": NOW,
        })

        updated = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc),
                           description="Updated description")]
        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=updated, now=NOW, attribute=promoter_attribute,
        )
        after = dao.get_event(row["event_id"])
        assert after["venue_id"] == "v_manual"
        assert after["location_resolution"] == "manual"
        assert after["linked_by"] == "operator_x"
        # Content still refreshes for a manually-linked (non-confirmed) row.
        assert after["description"] == "Updated description"

    def test_attribute_is_never_invoked_for_a_manually_linked_row(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        calls = []

        def tracking_attribute(fields, event_id):
            calls.append(fields["title"])
            return {"venue_id": "v_auto", "location_resolution": "auto"}, None

        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=tracking_attribute,
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {
            "venue_id": "v_manual", "location_resolution": "manual",
        })

        calls.clear()
        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=tracking_attribute,
        )
        assert calls == []

    def test_a_manually_linked_row_genuinely_replaced_is_marked_absent_not_silently_stale(self):
        """The same guarantee TestConfirmedPreservationAndDivergence proves
        for a confirmed row, for a manually-linked one: orphaned (both title
        and date changed) and correctly not paired, it must not go entirely
        untouched. review_reason/last_seen_at refresh; venue_id/
        location_resolution/linked_by (operator-owned) do not move."""
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]

        def promoter_attribute(fields, event_id):
            return {"venue_id": "v_auto", "location_resolution": "auto"}, None

        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=promoter_attribute,
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {
            "venue_id": "v_manual", "location_resolution": "manual",
            "linked_by": "operator_x", "linked_at": NOW,
            "last_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        })

        unrelated = [_event("Something Else Entirely", datetime(2026, 9, 1, tzinfo=timezone.utc))]
        later = datetime(2026, 8, 12, tzinfo=timezone.utc)
        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=unrelated, now=later, attribute=promoter_attribute,
        )

        after = dao.get_event(row["event_id"])
        assert after["venue_id"] == "v_manual"
        assert after["location_resolution"] == "manual"
        assert after["linked_by"] == "operator_x"
        assert after["review_reason"] == REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION
        assert after["last_seen_at"] == later

        rows = _rows(dao, "h1", "s1")
        assert len(rows) == 2, rows
        assert any(r["title"] == "Something Else Entirely" for r in rows)


class TestAttributionStrategies:
    def test_venue_attribution_attaches_the_owning_venue_id_regardless_of_location_text(self):
        dao = _dao()
        events = [_event(
            "Event A", datetime(2026, 8, 10, tzinfo=timezone.utc),
            location_text="Some Other Venue Entirely",
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        # A venue post's own location_text is recorded but never
        # re-attributes the event elsewhere.
        assert row["venue_id"] == "v1"
        assert row["location_text"] == "Some Other Venue Entirely"
        assert row["location_resolution"] is None

    def test_promoter_attribution_runs_the_ladder_on_each_events_own_location_text(self):
        dao = _dao()
        events = [
            _event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc), location_text="Venue One"),
            _event("Event B", datetime(2026, 8, 11, tzinfo=timezone.utc), location_text="Venue Two"),
        ]
        seen_location_texts = []

        def ladder_attribute(fields, event_id):
            seen_location_texts.append(fields["location_text"])
            venue_id = {"Venue One": "v1", "Venue Two": "v2"}[fields["location_text"]]
            return {"venue_id": venue_id, "location_resolution": "auto"}, None

        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=ladder_attribute,
        )
        rows = _rows(dao, "h1", "s1")
        by_title = {r["title"]: r for r in rows}
        assert by_title["Event A"]["venue_id"] == "v1"
        assert by_title["Event B"]["venue_id"] == "v2"
        # Each event's OWN location_text reached the ladder — never a
        # sibling's, never merged.
        assert sorted(seen_location_texts) == ["Venue One", "Venue Two"]


class TestEventsPerPostHistogram:
    def test_observes_the_number_of_prepared_events_every_call(self):
        from prometheus_client import REGISTRY

        def _sum() -> float:
            return REGISTRY.get_sample_value("event_extraction_events_per_post_sum") or 0.0

        before = _sum()
        events = [
            _event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc)),
            _event("Event B", datetime(2026, 8, 11, tzinfo=timezone.utc)),
            _event("Event C", datetime(2026, 8, 12, tzinfo=timezone.utc)),
        ]
        dao = _dao()
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = _sum()
        assert after - before == 3.0


class TestDatelessEventsNeverPairOnDateAlone:
    """An unresolved date is not evidence of a MATCHING date: `None == None`
    must never count as "same date" when deciding whether an orphaned
    confirmed/manual row is plausibly the SAME event as an unmatched fresh
    one. Two genuinely different, both-dateless events differ only by
    title, which the exactly-one-changed rule would otherwise read as
    "date matched, title changed" — a false pair."""

    def test_two_dateless_events_with_different_titles_do_not_pair(self):
        dao = _dao()
        events = [_event("Event A", None)]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})

        unrelated = [_event("Event Z", None)]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=unrelated, now=NOW, attribute=_venue_attribute("v1"),
        )

        rows = _rows(dao, "h1", "s1")
        assert len(rows) == 2, rows
        confirmed = dao.get_event(row["event_id"])
        # NOT absorbed as "Event A"'s divergent answer: title unchanged,
        # marked absent rather than diverging (there is no substantiated
        # divergence — "Event Z" was never treated as Event A's own answer).
        assert confirmed["title"] == "Event A"
        assert confirmed["review_reason"] == REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION
        by_title = {r["title"]: r for r in rows}
        assert by_title["Event Z"]["status"] == STATUS_PENDING_REVIEW


class TestDuplicateKeyWithinOneRun:
    """compute_source_event_key's own docstring already names this: two
    events in one post with the same normalized title and the same
    resolved date collapse onto ONE key. Persisting both would violate the
    real (source_handle, source_shortcode, source_event_key) UNIQUE
    constraint — the second must be skipped, never inserted alongside the
    first."""

    def test_two_identical_title_and_date_events_do_not_both_get_inserted(self):
        dao = _dao()
        same_date = datetime(2026, 8, 10, tzinfo=timezone.utc)
        events = [
            _event("Duplicate Event", same_date, description="First copy"),
            _event("Duplicate Event", same_date, description="Second copy"),
        ]
        persisted = reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        rows = _rows(dao, "h1", "s1")
        assert len(rows) == 1, rows
        assert persisted == 1

    def test_a_duplicate_key_does_not_crash_the_whole_post(self):
        """Before the fix this raised (the fake enforces the real UNIQUE
        constraint on insert_event) instead of degrading gracefully."""
        dao = _dao()
        same_date = datetime(2026, 8, 10, tzinfo=timezone.utc)
        events = [
            _event("Duplicate Event", same_date),
            _event("Duplicate Event", same_date),
            _event("Distinct Event", datetime(2026, 8, 11, tzinfo=timezone.utc)),
        ]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        rows = _rows(dao, "h1", "s1")
        assert {r["title"] for r in rows} == {"Duplicate Event", "Distinct Event"}
        assert len(rows) == 2


class TestExtractionFailurePlaceholderNeverSuperseded:
    """A row with NO source_event_key at all is an extraction_failed
    placeholder (EventExtractionService._record_failure /
    PromoterCrawlService._record_failure) that predates content identity
    entirely — the original PromoterCrawlService explicitly skipped it in
    the supersede loop (`if key is None or key in seen_keys: continue`).
    It must never be flipped to superseded just because this run's events
    don't happen to share its (nonexistent) key."""

    def test_a_null_key_placeholder_is_never_superseded(self):
        dao = _dao()
        placeholder_id = "evt_placeholder"
        dao.insert_event({
            "event_id": placeholder_id, "source_kind": "venue_post",
            "source_handle": "h1", "source_shortcode": "s1",
            "status": "extraction_failed", "source_event_key": None,
            "raw_extraction": {"error": "boom"},
        })

        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )

        placeholder = dao.get_event(placeholder_id)
        assert placeholder["status"] == "extraction_failed"


class TestCleanPredicateTruthTable:
    """plans/260807_auto-accept-and-field-level-protection.md §A: `is_clean_
    extraction` is the SOLE gate between `accepted` and `pending_review`,
    so its truth table is pinned directly, independent of reconcile_post_
    events' plumbing — every one of the four conditions must be
    independently necessary, and all four together must be sufficient."""

    _CLEAN_KWARGS = dict(
        review_reason=None,
        starts_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        venue_id="v1",
        confidence=0.9,
        min_confidence=0.5,
    )

    def test_all_four_conditions_together_are_clean(self):
        assert is_clean_extraction(**self._CLEAN_KWARGS) is True

    def test_a_review_reason_alone_is_not_clean(self):
        kwargs = {**self._CLEAN_KWARGS, "review_reason": "low_confidence"}
        assert is_clean_extraction(**kwargs) is False

    def test_a_missing_start_date_alone_is_not_clean(self):
        kwargs = {**self._CLEAN_KWARGS, "starts_at": None}
        assert is_clean_extraction(**kwargs) is False

    def test_a_missing_venue_alone_is_not_clean(self):
        kwargs = {**self._CLEAN_KWARGS, "venue_id": None}
        assert is_clean_extraction(**kwargs) is False

    def test_confidence_below_the_floor_alone_is_not_clean(self):
        kwargs = {**self._CLEAN_KWARGS, "confidence": 0.49}
        assert is_clean_extraction(**kwargs) is False

    def test_confidence_exactly_on_the_floor_is_clean(self):
        kwargs = {**self._CLEAN_KWARGS, "confidence": 0.5}
        assert is_clean_extraction(**kwargs) is True

    def test_a_missing_confidence_is_not_clean(self):
        """A `None` confidence must never be silently treated as "no floor
        to clear" — the four conditions are ALL required, never three."""
        kwargs = {**self._CLEAN_KWARGS, "confidence": None}
        assert is_clean_extraction(**kwargs) is False

    def test_every_combination_of_the_four_conditions(self):
        """The exhaustive 2^4 truth table: clean iff EVERY condition holds —
        never a majority, never any single condition standing in for the
        others."""
        options = {
            "review_reason": (None, "some_reason"),
            "starts_at": (datetime(2026, 8, 10, tzinfo=timezone.utc), None),
            "venue_id": ("v1", None),
            "confidence": (0.9, 0.1),
        }
        for review_reason in options["review_reason"]:
            for starts_at in options["starts_at"]:
                for venue_id in options["venue_id"]:
                    for confidence in options["confidence"]:
                        expected = (
                            review_reason is None
                            and starts_at is not None
                            and venue_id is not None
                            and confidence is not None
                            and confidence >= 0.5
                        )
                        actual = is_clean_extraction(
                            review_reason=review_reason, starts_at=starts_at,
                            venue_id=venue_id, confidence=confidence, min_confidence=0.5,
                        )
                        assert actual == expected, (
                            review_reason, starts_at, venue_id, confidence, actual, expected,
                        )


class TestAutoAcceptWiring:
    """Confirms `reconcile_post_events` actually WIRES is_clean_extraction
    in — not just that the pure predicate is correct in isolation."""

    def test_a_clean_fresh_event_is_accepted(self):
        dao = _dao()
        events = [_event("Clean Event", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
            min_confidence=0.5,
        )
        row = _rows(dao, "h1", "s1")[0]
        assert row["status"] == STATUS_ACCEPTED

    def test_a_low_confidence_event_stays_pending_review_even_with_no_review_reason(self):
        """Isolates the predicate's STANDALONE confidence check: this event
        carries no review_reason at all (a caller could, in principle, fail
        to fold a low reading into it) — `is_clean_extraction` must still
        catch it via `confidence >= min_confidence` on its own."""
        dao = _dao()
        events = [_event(
            "Iffy Event", datetime(2026, 8, 10, tzinfo=timezone.utc),
            confidence=0.2, review_reason=None,
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
            min_confidence=0.5,
        )
        row = _rows(dao, "h1", "s1")[0]
        assert row["status"] == STATUS_PENDING_REVIEW

    def test_a_promoter_event_with_no_venue_resolved_stays_pending_review(self):
        dao = _dao()
        events = [_event("Unresolved Event", datetime(2026, 8, 10, tzinfo=timezone.utc))]

        def unresolved_attribute(fields, event_id):
            return {"venue_id": None, "location_resolution": "unresolved"}, None

        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=unresolved_attribute,
            min_confidence=0.5,
        )
        row = _rows(dao, "h1", "s1")[0]
        assert row["status"] == STATUS_PENDING_REVIEW

    def test_an_accepted_event_supersedes_normally(self):
        """LOAD-BEARING (plans/260807_auto-accept-and-field-level-
        protection.md §C): if `accepted` inherited the confirmed/manual
        supersession exemption, then once most events are accepted, almost
        nothing could ever be superseded and 0025/0026's supersede
        behaviour would quietly become dead code."""
        dao = _dao()
        events = [_event("Clean Event", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
            min_confidence=0.5,
        )
        row = _rows(dao, "h1", "s1")[0]
        assert row["status"] == STATUS_ACCEPTED

        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=[], now=NOW, attribute=_venue_attribute("v1"),
            min_confidence=0.5,
        )
        after = dao.get_event(row["event_id"])
        assert after["status"] == STATUS_SUPERSEDED

    def test_a_manually_linked_but_unconfirmed_event_that_is_clean_is_accepted(self):
        """Status and supersession-exemption are independent axes: a
        manually-linked (location_resolution == manual) event that is
        otherwise clean is still labelled `accepted` — the manual link, not
        the status, is what protects it from supersession."""
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v_auto"),
            min_confidence=0.5,
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {
            "venue_id": "v_manual", "location_resolution": "manual",
            "linked_by": "operator_x", "linked_at": NOW,
        })

        reconcile_post_events(
            venue_dao=dao, source_kind="promoter_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v_auto"),
            min_confidence=0.5,
        )
        after = dao.get_event(row["event_id"])
        assert after["status"] == STATUS_ACCEPTED
        assert after["location_resolution"] == "manual"


class TestFieldLevelProtectionPerFieldTable:
    """plans/260807_auto-accept-and-field-level-protection.md §B: per field,
    on a confirmed row's re-extraction — null/absent never degrades, an
    equal value is a no-op, an unedited field updates, an edited field
    holds and flags. Drives the real reconcile_post_events end to end
    (never the private per-field helper directly) so these tests break the
    moment the wiring — not just the table — regresses."""

    def _confirmed_row(self, dao, *, edited_fields, **overrides):
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc), **overrides)]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        dao.update_event(row["event_id"], {
            "status": STATUS_CONFIRMED, "operator_edited_fields": edited_fields,
        })
        return row["event_id"]

    def test_a_null_new_value_never_overwrites_a_known_one(self):
        dao = _dao()
        event_id = self._confirmed_row(dao, edited_fields=["title"], price_text="R$30")

        later = [_event(
            "Event A", datetime(2026, 8, 10, tzinfo=timezone.utc), price_text=None,
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=later, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(event_id)
        assert after["price_text"] == "R$30"

    def test_an_unedited_field_updates_from_the_fresh_answer(self):
        dao = _dao()
        event_id = self._confirmed_row(dao, edited_fields=["title"], price_text="R$30")

        later = [_event(
            "Event A", datetime(2026, 8, 10, tzinfo=timezone.utc), price_text="R$99",
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=later, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(event_id)
        assert after["price_text"] == "R$99"
        assert after["review_reason"] is None

    def test_an_edited_field_holds_and_flags_when_the_fresh_answer_differs(self):
        dao = _dao()
        event_id = self._confirmed_row(dao, edited_fields=["price_text"], price_text="R$30")

        later = [_event(
            "Event A", datetime(2026, 8, 10, tzinfo=timezone.utc), price_text="R$99",
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=later, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(event_id)
        assert after["price_text"] == "R$30"
        assert after["review_reason"] == REVIEW_REASON_DIVERGES_FROM_CONFIRMED

    def test_an_edited_field_equal_to_the_fresh_answer_is_not_flagged(self):
        dao = _dao()
        event_id = self._confirmed_row(dao, edited_fields=["price_text"], price_text="R$30")

        later = [_event(
            "Event A", datetime(2026, 8, 10, tzinfo=timezone.utc), price_text="R$30",
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=later, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(event_id)
        assert after["price_text"] == "R$30"
        assert after["review_reason"] is None

    def test_lineup_unions_even_on_an_operator_edited_event(self):
        dao = _dao()
        event_id = self._confirmed_row(
            dao, edited_fields=["title"], lineup=["DJ A", "DJ B"],
        )

        later = [_event(
            "Event A", datetime(2026, 8, 10, tzinfo=timezone.utc), lineup=["DJ B", "DJ C"],
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=later, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(event_id)
        assert after["lineup"] == ["DJ A", "DJ B", "DJ C"]

    def test_a_legacy_null_operator_edited_fields_keeps_whole_row_protection(self):
        """plans/260807_auto-accept-and-field-level-protection.md §3: a
        confirmed row with NO record of which fields were edited (NULL —
        every row before this migration, or one confirmed without ever
        being PATCHed) must keep TODAY's whole-row freeze: not even an
        unedited-seeming field updates."""
        dao = _dao()
        events = [_event(
            "Event A", datetime(2026, 8, 10, tzinfo=timezone.utc), price_text="R$30",
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=events, now=NOW, attribute=_venue_attribute("v1"),
        )
        row = _rows(dao, "h1", "s1")[0]
        # Confirmed with NO operator_edited_fields key ever set — stays NULL.
        dao.update_event(row["event_id"], {"status": STATUS_CONFIRMED})
        assert dao.get_event(row["event_id"]).get("operator_edited_fields") is None

        later = [_event(
            "Event A", datetime(2026, 8, 10, tzinfo=timezone.utc),
            price_text="R$999", lineup=["DJ Z"],
        )]
        reconcile_post_events(
            venue_dao=dao, source_kind="venue_post", source_handle="h1",
            source_shortcode="s1", source_permalink=None,
            prepared_events=later, now=NOW, attribute=_venue_attribute("v1"),
        )
        after = dao.get_event(row["event_id"])
        assert after["price_text"] == "R$30"
        assert after["lineup"] == []
        assert after["review_reason"] is None  # title/date agree; no divergence
