"""Unit tests for the handle-identity merge
(plans/260811_merge-unresolved-into-resolved-sibling.md).

`tests/test_event_merge.py` covers the pure decision functions
(`compute_event_identity`, `choose_canonical`, `merge_event_fields`) in
isolation; this file adds `compute_handle_identity` at that same pure level,
then exercises `merge_touched_events` end-to-end against
`tests.rds_fake.InMemoryRdsVenueStore` — the SAME fake the BDD suite's `ee_`
harness uses — for everything a pure-function test alone cannot prove:
direction survives BOTH processing orders, an ambiguous handle refuses the
WHOLE group (not just the unresolved member), the pre-existing resolved-to-
resolved path is BYTE-FOR-BYTE unchanged, and the real nine-pair production
case collapses cleanly.

tests/bdd/enrichment/merge-unresolved-into-resolved-sibling.feature covers
the same behavior from the outside; this file protects the same decisions at
the DAO-row level, so a regression here fails fast without booting behave.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.venue import Venue
from app.services.event_merge import compute_handle_identity, merge_touched_events
from app.services.event_reconciliation import new_event_id
from app.services.event_venue_resolution import METHOD_SIBLING_MERGE, RESOLUTION_AUTO
from tests.rds_fake import InMemoryRdsVenueStore

_DATE = datetime(2026, 7, 8, 16, 0, tzinfo=timezone.utc)
_NOON = _DATE.replace(hour=12)
_HANDLE = "entreamigosobode"
_TITLE = "Oficina de Sorvete"


def _store_with_venue(venue_id="v1", venue_name="Entre Amigos O Bode") -> InMemoryRdsVenueStore:
    store = InMemoryRdsVenueStore()
    store.upsert_venue(
        Venue(venue_id=venue_id, venue_name=venue_name, venue_lat=-8.05, venue_lng=-34.88)
    )
    return store


def _insert(store: InMemoryRdsVenueStore, shortcode: str, **fields) -> str:
    event_id = fields.pop("event_id", None) or new_event_id()
    base = {
        "event_id": event_id, "source_shortcode": shortcode,
        "source_event_key": f"{shortcode}_key",
        "source_permalink": f"https://instagram.com/p/{shortcode}",
        "raw_extraction": {"time_known": True},
    }
    base.update(fields)
    store.insert_event(base)
    return event_id


class TestComputeHandleIdentity:
    def test_same_handle_title_and_date_are_the_same_identity(self):
        a = compute_handle_identity({"source_handle": "h", "starts_at": _DATE, "title": _TITLE})
        b = compute_handle_identity({"source_handle": "h", "starts_at": _DATE, "title": _TITLE})
        assert a == b

    def test_case_and_accent_differences_do_not_change_identity(self):
        a = compute_handle_identity({"source_handle": "h", "starts_at": _DATE, "title": "OFICINA DE SORVETE"})
        b = compute_handle_identity({"source_handle": "h", "starts_at": _DATE, "title": "Oficina de Sorvete"})
        assert a == b

    def test_differing_clock_time_on_the_same_date_is_the_same_identity(self):
        a = compute_handle_identity({"source_handle": "h", "starts_at": _DATE, "title": _TITLE})
        b = compute_handle_identity({"source_handle": "h", "starts_at": _NOON, "title": _TITLE})
        assert a == b

    def test_different_handle_is_a_different_identity(self):
        a = compute_handle_identity({"source_handle": "h1", "starts_at": _DATE, "title": _TITLE})
        b = compute_handle_identity({"source_handle": "h2", "starts_at": _DATE, "title": _TITLE})
        assert a != b

    def test_different_date_is_a_different_identity(self):
        a = compute_handle_identity({"source_handle": "h", "starts_at": _DATE, "title": _TITLE})
        b = compute_handle_identity({
            "source_handle": "h", "starts_at": _DATE.replace(day=9), "title": _TITLE,
        })
        assert a != b

    def test_null_date_never_computes_an_identity(self):
        assert compute_handle_identity({"source_handle": "h", "starts_at": None, "title": _TITLE}) is None

    def test_null_handle_never_computes_an_identity(self):
        assert compute_handle_identity({"source_handle": None, "starts_at": _DATE, "title": _TITLE}) is None

    def test_missing_handle_key_never_computes_an_identity(self):
        assert compute_handle_identity({"starts_at": _DATE, "title": _TITLE}) is None


def _seed_pair(
    store: InMemoryRdsVenueStore, *, handle=_HANDLE, venue_id="v1", unresolved_id_smaller=True,
) -> tuple[str, str]:
    """Seeds one resolved + one unresolved event sharing a handle identity.
    `unresolved_id_smaller` controls INSERTION order (and therefore which
    event_id — a time-ordered ULID — sorts first): the DEFAULT (True) is the
    harder case, matching how the real production pairs actually arise (the
    unresolved post can easily be archived/extracted before its resolved
    sibling)."""
    if unresolved_id_smaller:
        unresolved_id = _insert(
            store, "unresolved_post", venue_id=None, source_handle=handle,
            source_kind="promoter_post", title=_TITLE, starts_at=_DATE,
            status="pending_review", review_reason="unresolved_venue",
        )
        resolved_id = _insert(
            store, "resolved_post", venue_id=venue_id, source_handle=handle,
            source_kind="venue_post", title=_TITLE, starts_at=_DATE, status="accepted",
        )
    else:
        resolved_id = _insert(
            store, "resolved_post", venue_id=venue_id, source_handle=handle,
            source_kind="venue_post", title=_TITLE, starts_at=_DATE, status="accepted",
        )
        unresolved_id = _insert(
            store, "unresolved_post", venue_id=None, source_handle=handle,
            source_kind="promoter_post", title=_TITLE, starts_at=_DATE,
            status="pending_review", review_reason="unresolved_venue",
        )
    assert (unresolved_id < resolved_id) == unresolved_id_smaller  # fixture sanity
    return resolved_id, unresolved_id


class TestDirectionIsStructural:
    """The resolved member always survives and adopts nothing; the
    unresolved member always adopts the resolved member's venue. Two
    INDEPENDENT axes can each hide an ordering bug, so both are
    parametrized together rather than assumed to co-vary:
      - which event_id (ULID) sorts first — a canonical-selection bug that
        picks "the oldest id in the WHOLE group" instead of "the oldest id
        among the RESOLVED members" only shows up when the unresolved
        item's id sorts first, which every OTHER fixture in this module
        happens NOT to exercise (a prior version of this test suite created
        the resolved member first everywhere, and stayed green when this
        exact bug was deliberately reintroduced — see the PR);
      - which event is fed to `merge_touched_events` first — the real
        `_merge_one` entry point a crawl actually calls per post.

    This is the exact shape of two ordering bugs this project has already
    shipped (see the plan's own evidence): an event attributed to whichever
    venue was processed last, and a cursor advanced before the work it
    guarded.
    """

    @pytest.mark.parametrize("unresolved_id_smaller", [True, False])
    @pytest.mark.parametrize("resolved_processed_first", [True, False])
    def test_the_resolved_member_always_survives(self, unresolved_id_smaller, resolved_processed_first):
        store = _store_with_venue()
        resolved_id, unresolved_id = _seed_pair(store, unresolved_id_smaller=unresolved_id_smaller)
        ids = (
            [resolved_id, unresolved_id] if resolved_processed_first
            else [unresolved_id, resolved_id]
        )
        merge_touched_events(store, ids, datetime.now(timezone.utc))
        assert store.get_event(unresolved_id) is None
        survivor = store.get_event(resolved_id)
        assert survivor is not None
        assert survivor["venue_id"] == "v1"


class TestAmbiguityRefusesToGuess:
    def test_two_resolved_siblings_at_different_venues_leave_the_whole_group_untouched(self):
        store = _store_with_venue("v1", "Entre Amigos O Bode")
        store.upsert_venue(Venue(
            venue_id="v2", venue_name="Entre Amigos O Bode Espinheiro", venue_lat=-8.06, venue_lng=-34.90,
        ))
        a_id = _insert(
            store, "venue_a", venue_id="v1", source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="accepted",
        )
        b_id = _insert(
            store, "venue_b", venue_id="v2", source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="accepted",
        )
        u_id = _insert(
            store, "unresolved", venue_id=None, source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="pending_review", review_reason="unresolved_venue",
        )

        merge_touched_events(store, [a_id, b_id, u_id], datetime.now(timezone.utc))

        assert store.get_event(a_id) is not None
        assert store.get_event(b_id) is not None
        row = store.get_event(u_id)
        assert row is not None
        assert row["venue_id"] is None
        assert row["review_reason"] == "unresolved_venue"


class TestGroupProtectionsExtendedNotBypassed:
    """The SAME group protections a resolved-to-resolved merge already
    honours (`_is_protected`), extended to the handle path rather than
    bypassed — and an `operator_edited_fields` entry for `venue_id`, which
    only THIS path needs to check."""

    def test_a_confirmed_unresolved_item_is_left_alone(self):
        store = _store_with_venue()
        resolved_id = _insert(
            store, "r_confirmed", venue_id="v1", source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="accepted",
        )
        confirmed_id = _insert(
            store, "u_confirmed", venue_id=None, source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="confirmed", review_reason=None,
        )
        merge_touched_events(store, [resolved_id, confirmed_id], datetime.now(timezone.utc))
        row = store.get_event(confirmed_id)
        assert row is not None
        assert row["status"] == "confirmed"
        assert row["venue_id"] is None
        assert store.get_event(resolved_id) is not None

    def test_an_operator_edited_venue_field_is_left_alone(self):
        store = _store_with_venue()
        resolved_id = _insert(
            store, "r_edited", venue_id="v1", source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="accepted",
        )
        edited_id = _insert(
            store, "u_edited", venue_id=None, source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="pending_review", operator_edited_fields=["venue_id"],
        )
        merge_touched_events(store, [resolved_id, edited_id], datetime.now(timezone.utc))
        row = store.get_event(edited_id)
        assert row is not None
        assert row["venue_id"] is None
        assert row["operator_edited_fields"] == ["venue_id"]
        assert store.get_event(resolved_id) is not None


class TestResolvedToResolvedPathIsByteForByteUnchanged:
    """The common case, and (per the plan) the likeliest casualty of this
    change — even though `_merge_one` now ALSO calls `_merge_handle_group`
    after every venue-identity merge, a solo resolved survivor with no
    unresolved sibling must come out of that call with NOTHING touched by
    the new path: same canonical choice, same field fold, no §D
    bookkeeping."""

    def test_two_resolved_posts_at_one_venue_merge_exactly_as_before(self):
        store = _store_with_venue()
        older_id = _insert(
            store, "older", venue_id="v1", source_handle=_HANDLE, title="Noite da Patroa",
            starts_at=_DATE, status="pending_review", lineup=["DJ A"],
        )
        newer_id = _insert(
            store, "newer", venue_id="v1", source_handle=_HANDLE, title="NOITE DA PATROA",
            starts_at=_DATE, status="pending_review", lineup=["DJ B"],
        )
        merge_touched_events(store, [older_id, newer_id], datetime.now(timezone.utc))

        assert store.get_event(newer_id) is None
        survivor = store.get_event(older_id)
        assert survivor is not None
        assert survivor["event_id"] == older_id  # oldest ULID wins, exactly as before
        assert survivor["title"] == "Noite da Patroa"  # canonical's own title stands
        assert survivor["lineup"] == ["DJ A", "DJ B"]  # unioned, exactly as before
        # No handle-identity side effect leaked into the ordinary path: this
        # solo (post-merge) survivor has no unresolved sibling to adopt, so
        # §D's adopted-venue bookkeeping must never fire here.
        assert survivor.get("linked_by") is None
        assert survivor.get("location_resolution") is None


class TestRealProductionCase:
    def test_nine_entreamigosobode_pairs_collapse_to_nine_items_each_at_the_venue(self):
        store = _store_with_venue("v1", "Entre Amigos O Bode")
        touched_ids: list[str] = []
        expected_survivors: list[str] = []
        for i in range(9):
            title = f"Oficina de Sorvete - semana {i + 1}"
            starts_at = _DATE.replace(day=8 + i)
            resolved_id = _insert(
                store, f"resolved_{i}", venue_id="v1", source_handle=_HANDLE,
                title=title, starts_at=starts_at, status="accepted",
            )
            unresolved_id = _insert(
                store, f"unresolved_{i}", venue_id=None, source_handle=_HANDLE,
                title=title, starts_at=starts_at, status="pending_review",
                review_reason="date_range; unresolved_venue",
            )
            touched_ids += [resolved_id, unresolved_id]
            expected_survivors.append(resolved_id)

        merge_touched_events(store, touched_ids, datetime.now(timezone.utc))

        remaining = store.list_events(venue_id="v1")
        assert {r["event_id"] for r in remaining} == set(expected_survivors)
        assert len(remaining) == 9
        for row in remaining:
            assert row["venue_id"] == "v1"
            assert "unresolved_venue" not in (row.get("review_reason") or "")
            assert "date_range" in (row.get("review_reason") or "")


class TestAdoptedVenueProvenance:
    """plans/260811_merge-unresolved-into-resolved-sibling.md §D: the
    adopted venue is weaker evidence than one the post itself named,
    recorded via `linked_by`/`location_resolution` — UNLESS the survivor is
    already manually linked, in which case an operator's attribution must
    never be overwritten by an automatic path."""

    def test_the_survivor_records_that_its_venue_was_adopted_from_a_sibling(self):
        store = _store_with_venue()
        resolved_id, unresolved_id = _seed_pair(store)
        merge_touched_events(store, [resolved_id, unresolved_id], datetime.now(timezone.utc))
        row = store.get_event(resolved_id)
        assert row["linked_by"] == METHOD_SIBLING_MERGE
        assert row["location_resolution"] == RESOLUTION_AUTO

    def test_a_manually_linked_survivor_keeps_its_operators_attribution(self):
        store = _store_with_venue()
        resolved_id = _insert(
            store, "r_manual", venue_id="v1", source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="accepted", location_resolution="manual",
            linked_by="operator@example.com",
        )
        unresolved_id = _insert(
            store, "u_manual", venue_id=None, source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="pending_review",
        )
        merge_touched_events(store, [resolved_id, unresolved_id], datetime.now(timezone.utc))
        row = store.get_event(resolved_id)
        assert row["linked_by"] == "operator@example.com"
        assert row["location_resolution"] == "manual"
        assert store.get_event(unresolved_id) is None  # still absorbed


class TestReviewReasonFold:
    def test_unresolved_venue_is_dropped_and_other_reasons_survive(self):
        store = _store_with_venue()
        resolved_id = _insert(
            store, "r_reason", venue_id="v1", source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="accepted", review_reason=None,
        )
        unresolved_id = _insert(
            store, "u_reason", venue_id=None, source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="pending_review", review_reason="date_range; unresolved_venue",
        )
        merge_touched_events(store, [resolved_id, unresolved_id], datetime.now(timezone.utc))
        row = store.get_event(resolved_id)
        assert row["review_reason"] == "date_range"

    def test_a_confirmed_canonicals_review_reason_is_untouched_by_the_fold(self):
        store = _store_with_venue()
        resolved_id = _insert(
            store, "r_confirmed_reason", venue_id="v1", source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="confirmed", review_reason=None,
        )
        unresolved_id = _insert(
            store, "u_confirmed_reason", venue_id=None, source_handle=_HANDLE, title=_TITLE,
            starts_at=_DATE, status="pending_review", review_reason="date_range; unresolved_venue",
        )
        merge_touched_events(store, [resolved_id, unresolved_id], datetime.now(timezone.utc))
        row = store.get_event(resolved_id)
        assert row["review_reason"] is None
        assert store.get_event(unresolved_id) is None
