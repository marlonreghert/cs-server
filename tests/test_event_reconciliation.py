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
    REVIEW_REASON_DIVERGES_FROM_CONFIRMED,
    STATUS_CONFIRMED,
    STATUS_PENDING_REVIEW,
    STATUS_SUPERSEDED,
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
    def attribute(fields: dict, event_id: str) -> dict:
        return {"venue_id": venue_id}
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
        assert all(r["status"] == STATUS_PENDING_REVIEW for r in rows)
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
        # NOT flagged: this run's fresh event was never treated as ITS
        # divergent answer — it is a different event entirely.
        assert confirmed["review_reason"] is None

        new_row = by_title["Noite do Forro"]
        assert new_row["status"] == STATUS_PENDING_REVIEW
        assert new_row["starts_at"].date().isoformat() == "2026-08-21"

    def test_attribute_is_never_invoked_for_a_confirmed_row(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]
        calls = []

        def tracking_attribute(fields, event_id):
            calls.append(fields["title"])
            return {"venue_id": "v1"}

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
        # pending_review rows for the renamed events — never a guessed pairing.
        assert len(rows) == 4
        still_confirmed = {r["event_id"] for r in rows if r["status"] == STATUS_CONFIRMED}
        assert still_confirmed == confirmed_ids
        for row in rows:
            if row["event_id"] in confirmed_ids:
                assert row["title"] in ("Event A", "Event B")
                assert row["review_reason"] is None
            else:
                assert row["status"] == STATUS_PENDING_REVIEW
                assert row["title"] in ("Event A Renamed", "Event B Renamed")


class TestManualLinkPreservation:
    def test_manual_link_columns_are_preserved_but_content_still_refreshes(self):
        dao = _dao()
        events = [_event("Event A", datetime(2026, 8, 10, tzinfo=timezone.utc))]

        def promoter_attribute(fields, event_id):
            return {"venue_id": "v_auto", "location_resolution": "auto"}

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
            return {"venue_id": "v_auto", "location_resolution": "auto"}

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
            return {"venue_id": venue_id, "location_resolution": "auto"}

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
