"""Unit tests for PATCH /admin/events/{event_id}'s operator_edited_fields
bookkeeping (plans/260807_auto-accept-and-field-level-protection.md §B).

BDD (auto-accept-and-field-protection.feature) covers the end-to-end
behaviour through the real router + reconciliation together; this file
isolates the router's OWN responsibility — recording exactly which fields a
PATCH touched, accumulating across successive patches, and never inventing
an entry for a field the request did not send — against a real
VenueRepository/InMemoryRdsVenueStore pair (the same fakes-must-raise
deterministic stand-in BDD uses), without going through reconcile_post_events
at all.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.venue_repository import VenueRepository
from app.routers.admin_events_router import router, set_container
from tests.rds_fake import InMemoryRdsVenueStore


def _client(dao: VenueRepository) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    set_container(type("C", (), {"pipeline_repository": dao})())
    return TestClient(app)


def _seed_event(dao: VenueRepository, event_id: str = "evt_1") -> str:
    dao.insert_event({
        "event_id": event_id, "source_kind": "venue_post",
        "source_handle": "h1", "source_shortcode": "s1",
        "status": "accepted", "title": "Original Title", "price_text": "R$30",
    })
    return event_id


def _dao() -> VenueRepository:
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


class TestRecordsOnlyPatchedFields:
    def test_a_single_field_patch_records_only_that_field(self):
        dao = _dao()
        event_id = _seed_event(dao)
        client = _client(dao)

        resp = client.patch(f"/admin/events/{event_id}", json={"title": "New Title"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["operator_edited_fields"] == ["title"]
        assert dao.get_event(event_id)["operator_edited_fields"] == ["title"]

    def test_fields_not_sent_are_never_recorded(self):
        """The console sends only genuinely changed fields (vibes_bot #169)
        and this reads `exclude_unset` — so even though EventPatch declares
        many optional fields, only the ones ACTUALLY present in the request
        body are ever recorded, never the full schema."""
        dao = _dao()
        event_id = _seed_event(dao)
        client = _client(dao)

        resp = client.patch(f"/admin/events/{event_id}", json={"price_text": "R$50"})

        assert resp.status_code == 200, resp.text
        recorded = resp.json()["operator_edited_fields"]
        assert recorded == ["price_text"]
        assert "title" not in recorded
        assert "venue_id" not in recorded


class TestAccumulatesAcrossPatches:
    def test_a_second_patch_of_a_different_field_accumulates(self):
        dao = _dao()
        event_id = _seed_event(dao)
        client = _client(dao)

        client.patch(f"/admin/events/{event_id}", json={"title": "First Correction"})
        resp = client.patch(f"/admin/events/{event_id}", json={"price_text": "R$50"})

        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["operator_edited_fields"]) == ["price_text", "title"]

    def test_patching_the_same_field_twice_does_not_duplicate_it(self):
        dao = _dao()
        event_id = _seed_event(dao)
        client = _client(dao)

        client.patch(f"/admin/events/{event_id}", json={"title": "First Correction"})
        resp = client.patch(f"/admin/events/{event_id}", json={"title": "Second Correction"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["operator_edited_fields"] == ["title"]

    def test_a_multi_field_patch_records_every_field_it_touched(self):
        dao = _dao()
        event_id = _seed_event(dao)
        client = _client(dao)

        resp = client.patch(
            f"/admin/events/{event_id}",
            json={"title": "New Title", "price_text": "R$50", "lineup": ["DJ A"]},
        )

        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["operator_edited_fields"]) == ["lineup", "price_text", "title"]


class TestNoOpPatchNeverInventsAnEntry:
    def test_an_empty_patch_body_leaves_operator_edited_fields_untouched(self):
        dao = _dao()
        event_id = _seed_event(dao)
        client = _client(dao)

        resp = client.patch(f"/admin/events/{event_id}", json={})

        assert resp.status_code == 200, resp.text
        assert resp.json()["operator_edited_fields"] is None


class TestEventOutSurfacesNullDistinctFromEmpty:
    def test_a_never_patched_event_reports_null_not_an_empty_list(self):
        """NULL (unknown which fields were edited) and an empty list (known:
        nothing was) are different facts — collapsing them would make a
        freshly-confirmed-but-never-PATCHed row indistinguishable from a
        genuine legacy row in the API, for any future consumer that cares."""
        dao = _dao()
        event_id = _seed_event(dao)
        client = _client(dao)

        resp = client.get(f"/admin/events/{event_id}")

        assert resp.status_code == 200, resp.text
        assert resp.json()["operator_edited_fields"] is None
