"""Behave steps for tests/bdd/enrichment/venue-mapping-correction.feature.

See plans/260914_recife-venue-mapping-corrections.md.

Drives `VenueRepository.delete_venue_instagram`/`update_crawl_target` and
`scripts.backfill_event_venue_links.run_backfill` (the new `force-reassign`/
`unresolved-venue` modes) directly against the in-memory RDS fake, exactly
like `backfill_misattributed_links_steps.py` does for the two existing
modes — no Apify/S3/OpenAI client exists anywhere in this harness.

Scenarios 1-5 are pinned against the REAL, independently re-verified
names/handles the plan's Evidence section captured (`real.botequim`,
`marcellos_music_bar`, `editaisculturape`, and their real mapped venue
names) — this harness assigns its own deterministic local venue_ids for
them, like every other BDD fixture in this repo; only the identifying
strings that matter for the scenario's realism (names, handles, the shape
of each defect) are the real ones. Scenario 6 is the one deliberate
exception: it is SYNTHETIC, both in the feature file's own comment and
here — `casabacurau`, the task brief's original real-world target for this
shape, was independently investigated and refuted (see the plan's Evidence:
the casabacurau -> Casa Bacurau mapping is correct) and is never referenced
anywhere in this module.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_identity import compute_source_event_key
from app.services.instagram_handle_sources import group_venue_ids_by_handle
from scripts.backfill_event_venue_links import (
    MODE_FORCE_REASSIGN,
    MODE_UNRESOLVED_VENUE,
    SKIP_CONFIRMED,
    SKIP_MANUAL_LINK,
    SKIP_SUPERSEDED,
    run_backfill,
)
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_STARTS_AT = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)


# ── harness setup ────────────────────────────────────────────────────────────
def _ensure(context) -> None:
    if getattr(context, "vmc_dao", None) is not None:
        return
    context.vmc_store = InMemoryRdsVenueStore()
    context.vmc_dao = VenueRepository(client=None, rds_store=context.vmc_store)
    context.vmc_venues: dict[str, str] = {}
    context.vmc_events: dict[str, str] = {}  # label -> event_id
    context.vmc_item_counter = 0
    context.vmc_report = None
    context.vmc_handle = None
    context.vmc_target_labels: list = []


def _add_venue(context, name: str, handle: "str | None" = None) -> str:
    _ensure(context)
    slug = name.lower().replace(" ", "_").replace("'", "").replace(".", "")
    venue_id = f"v_{slug}"
    context.vmc_dao.upsert_venue(Venue(
        venue_id=venue_id, venue_name=name, venue_lat=-8.05, venue_lng=-34.88, venue_address="",
    ))
    if handle:
        context.vmc_dao.set_venue_instagram(
            VenueInstagram(venue_id=venue_id, instagram_handle=handle, status="found"),
        )
    context.vmc_venues[name] = venue_id
    return venue_id


def _insert(
    context, *, label: str, handle: str, venue_id: "str | None", location_text: "str | None",
    title: "str | None" = None, review_reason: "str | None" = None,
    status: str = "pending_review", operator_edited_fields=None, linked_by=None,
) -> str:
    _ensure(context)
    context.vmc_item_counter += 1
    n = context.vmc_item_counter
    event_id = f"evt_vmc_{n:04d}"
    shortcode = f"vmc_post_{n}"
    title = title or f"Post {n}"
    fields = {
        "event_id": event_id, "venue_id": venue_id, "starts_at": _STARTS_AT,
        "title": title, "status": status, "review_reason": review_reason,
        "location_resolution": None, "location_confidence": None,
        "linked_by": linked_by, "linked_at": None,
        "operator_edited_fields": operator_edited_fields,
        "confidence": 0.9, "location_text": location_text,
        "source_kind": "promoter_post", "source_handle": handle,
        "source_shortcode": shortcode, "source_permalink": f"https://instagram.com/p/{shortcode}",
        "source_event_key": compute_source_event_key(title, _STARTS_AT), "source_event_index": 1,
        "raw_extraction": {"location_text": location_text},
        "first_seen_at": _NOW, "last_seen_at": _NOW,
    }
    context.vmc_dao.insert_event(fields)
    context.vmc_events[label] = event_id
    return event_id


def _row(context, label: str) -> dict:
    row = context.vmc_dao.get_event(context.vmc_events[label])
    assert row is not None, label
    return row


def _run(context, *, mode: str, handles=None, from_venue_id=None, to_venue_id=None) -> None:
    _ensure(context)
    context.vmc_exception = None
    try:
        report = run_backfill(
            context.vmc_dao, apply=True, now=_NOW, mode=mode,
            handles=handles, from_venue_id=from_venue_id, to_venue_id=to_venue_id,
        )
    except Exception as exc:  # noqa: BLE001 - captured for Then steps to inspect
        context.vmc_exception = exc
        report = getattr(exc, "report", None)
    context.vmc_report = report


def _assert_targets_reattributed(context, venue_id: str) -> None:
    for label in context.vmc_target_labels:
        row = _row(context, label)
        assert row["venue_id"] == venue_id, (label, row)


# ── Background ────────────────────────────────────────────────────────────────
@given("the venue-mapping-correction fixture corpus of re-verified real cases")
def step_background(context):
    _ensure(context)


# ── Scenario 1: dropping a spurious duplicate mapping ───────────────────────
@given("a handle currently mapped to two venues, one correct and one spurious and unrelated")
def step_given_double_mapped_handle(context):
    _ensure(context)
    handle = "real.botequim"
    context.vmc_handle = handle
    context.vmc_correct_venue = _add_venue(context, "Bar Real Botequim", handle)
    context.vmc_spurious_venue = _add_venue(context, "Real Bar e Lanches", handle)


@given(
    "every one of the handle's live events is stuck unresolved because neither mapped "
    "venue's name alone clears the auto-link floor"
)
def step_given_events_stuck_unresolved_floor(context):
    handle = context.vmc_handle
    context.vmc_target_labels = []
    for i in range(3):
        label = f"stuck_{i}"
        _insert(
            context, label=label, handle=handle, venue_id=None,
            location_text="Real Botequim\nAv. 17 de agosto, 1761 - Casa Forte",
            status="pending_review", review_reason="unresolved_venue",
        )
        context.vmc_target_labels.append(label)


@when("the spurious venue's mapping is dropped for that handle")
def step_when_spurious_mapping_dropped(context):
    context.vmc_dao.delete_venue_instagram(context.vmc_spurious_venue)


@then("the handle is mapped to exactly the one correct venue")
def step_then_handle_mapped_to_one(context):
    mapping = group_venue_ids_by_handle(context.vmc_dao.list_instagram_handles())
    assert mapping.get(context.vmc_handle) == [context.vmc_correct_venue], mapping


@then(
    "a future crawl of that handle would force-assign its posts directly, "
    "with no scored ladder involved"
)
def step_then_future_crawl_force_assigns(context):
    # `_chain()` (app/services/instagram_crawl_service.py) routes
    # len(venue_ids) == 1 to the single-venue force-assign path (no ladder,
    # no floor/margin) — plan Evidence, "The correction mechanism."
    mapping = group_venue_ids_by_handle(context.vmc_dao.list_instagram_handles())
    venue_ids = mapping.get(context.vmc_handle, [])
    assert len(venue_ids) == 1, venue_ids


# ── Scenario 2: force-reassign after the mapping fix ─────────────────────────
@given("a handle whose spurious mapping has already been dropped, leaving one correct venue")
def step_given_mapping_already_dropped(context):
    _ensure(context)
    handle = "real.botequim"
    context.vmc_handle = handle
    context.vmc_correct_venue = _add_venue(context, "Bar Real Botequim", handle)


@given("every one of the handle's events is still stuck with no venue from before the fix")
def step_given_events_stuck_no_venue(context):
    handle = context.vmc_handle
    _insert(
        context, label="clean", handle=handle, venue_id=None,
        location_text="Real Botequim\nAv. 17 de agosto, 1761 - Casa Forte",
        status="pending_review", review_reason="unresolved_venue",
    )
    _insert(
        context, label="missing_date", handle=handle, venue_id=None,
        location_text="Real Botequim\nAv. 17 de agosto, 1761 - Casa Forte",
        status="pending_review", review_reason="missing_date; unresolved_venue",
    )
    context.vmc_target_labels = ["clean", "missing_date"]


@when("the force-reassign correction runs for that handle from unresolved to its one correct venue")
def step_when_force_reassign_from_unresolved(context):
    _run(
        context, mode=MODE_FORCE_REASSIGN, handles=[context.vmc_handle],
        from_venue_id=None, to_venue_id=context.vmc_correct_venue,
    )


@then("every one of those events is reattributed to the correct venue")
def step_then_scenario2_reattributed(context):
    _assert_targets_reattributed(context, context.vmc_correct_venue)


@then("an event that carries no other review reason is served")
def step_then_clean_event_served(context):
    row = _row(context, "clean")
    assert row["status"] == "accepted", row
    assert not row.get("review_reason"), row


@then(
    "an event that still carries an unrelated review reason stays out of serving "
    "for that reason alone"
)
def step_then_missing_date_event_stays_pending(context):
    row = _row(context, "missing_date")
    assert row["status"] == "pending_review", row
    assert "missing_date" in (row.get("review_reason") or ""), row
    assert "unresolved_venue" not in (row.get("review_reason") or ""), row


# ── Scenario 3: the same shape generalizes to a second handle ────────────────
@given("a second, unrelated handle whose spurious mapping names a real venue in a different city entirely")
def step_given_second_handle(context):
    _ensure(context)
    handle = "marcellos_music_bar"
    context.vmc_handle = handle
    context.vmc_correct_venue = _add_venue(context, "Marcello's Music Bar", handle)
    context.vmc_target_labels = []
    for i in range(2):
        label = f"stuck2_{i}"
        _insert(
            context, label=label, handle=handle, venue_id=None,
            location_text="Rua Ramiz Galvão, 82 – Arruda (Pátio da Feira do Arruda)",
            status="pending_review", review_reason="unresolved_venue",
        )
        context.vmc_target_labels.append(label)


@given("that handle already has one event correctly resolved to the right venue by an unrelated mechanism")
def step_given_sibling_merge_resolved(context):
    handle = context.vmc_handle
    _insert(
        context, label="sibling_merge", handle=handle, venue_id=context.vmc_correct_venue,
        location_text="Marcello's Music Bar", status="accepted", linked_by="sibling_merge",
    )


@then("every one of that handle's still-unresolved events is reattributed to the correct venue")
def step_then_scenario3_reattributed(context):
    _assert_targets_reattributed(context, context.vmc_correct_venue)


@then("the event already resolved by the unrelated mechanism is left exactly as it was")
def step_then_sibling_unchanged(context):
    row = _row(context, "sibling_merge")
    assert row["venue_id"] == context.vmc_correct_venue, row
    assert row["linked_by"] == "sibling_merge", row
    assert row["status"] == "accepted", row


# ── Scenario 4: removing a bulletin account's force-assigned mapping ─────────
@given("a handle force-assigned to a single venue though its posts describe many different real places")
def step_given_bulletin_force_assigned(context):
    _ensure(context)
    handle = "editaisculturape"
    context.vmc_handle = handle
    context.vmc_wrong_venue = _add_venue(context, "Casa da Cultura de Pernambuco", handle)


@given("every one of the handle's live events currently carries that one venue, right or wrong")
def step_given_events_all_carry_wrong_venue(context):
    handle = context.vmc_handle
    venue_id = context.vmc_wrong_venue
    fixtures = [
        ("brejo", "Casa de Câmara e Cadeia de Brejo da Madre de Deus"),
        ("torre_malakoff", "Torre Malakoff"),
        ("mapa_cultural", "Mapa Cultural de Pernambuco"),
        ("pernambuco", "Pernambuco"),
    ]
    context.vmc_target_labels = []
    for label, text in fixtures:
        _insert(
            context, label=label, handle=handle, venue_id=venue_id,
            location_text=text, status="accepted",
        )
        context.vmc_target_labels.append(label)


@when("the venue mapping is removed for that handle")
def step_when_venue_mapping_removed(context):
    context.vmc_dao.delete_venue_instagram(context.vmc_wrong_venue)


@when("the force-reassign correction runs for that handle from its old venue to no venue")
def step_when_force_reassign_detach(context):
    _run(
        context, mode=MODE_FORCE_REASSIGN, handles=[context.vmc_handle],
        from_venue_id=context.vmc_wrong_venue, to_venue_id=None,
    )


@then("every one of the handle's events is detached from that venue")
def step_then_all_events_detached(context):
    for label in context.vmc_target_labels:
        row = _row(context, label)
        assert row["venue_id"] is None, (label, row)


@then("an event with no venue is not served until something else resolves it")
def step_then_no_venue_not_served(context):
    for label in context.vmc_target_labels:
        row = _row(context, label)
        assert row["status"] == "pending_review", (label, row)
        assert "unresolved_venue" in (row.get("review_reason") or ""), (label, row)


# ── Scenario 5: a detached event gets a fair shot at auto-resolution ─────────
@given("a handle's events have just been detached from a wrong force-assigned venue")
def step_given_events_just_detached(context):
    _ensure(context)
    handle = "editaisculturape"
    context.vmc_handle = handle
    # A real, catalogued venue this handle's own text can genuinely name —
    # proves the ladder gets a fair shot post-detach, not a hand-fed result.
    context.vmc_specific_venue = _add_venue(context, "Teatro do Parque", "teatrodoparquerecife")
    _insert(
        context, label="names_specific_venue", handle=handle, venue_id=None,
        location_text="@teatrodoparquerecife", status="pending_review",
        review_reason="unresolved_venue",
    )
    _insert(
        context, label="generic_branding", handle=handle, venue_id=None,
        location_text="Pernambuco", status="pending_review",
        review_reason="unresolved_venue",
    )


@when("the unresolved-venue correction runs for that handle")
def step_when_unresolved_venue_runs(context):
    _run(context, mode=MODE_UNRESOLVED_VENUE, handles=[context.vmc_handle])


@then("an event whose own text clearly and specifically names a real, catalogued venue is reattributed to it")
def step_then_specific_text_reattributed(context):
    row = _row(context, "names_specific_venue")
    assert row["venue_id"] == context.vmc_specific_venue, row
    assert row["status"] == "accepted", row


@then(
    "an event whose own text names no specific place, or only echoes the account's own "
    "generic branding, remains unresolved"
)
def step_then_generic_text_remains_unresolved(context):
    row = _row(context, "generic_branding")
    assert row["venue_id"] is None, row
    assert row["status"] == "pending_review", row


# ── Scenario 6: SYNTHETIC repointing (casabacurau refuted — never used) ──────
@given("a synthetic handle force-assigned to the wrong single venue")
def step_given_synthetic_wrong_venue(context):
    _ensure(context)
    # SYNTHETIC fixture — proves the generic force-reassign(from=X, to=Y)
    # code path only. See this module's docstring for why casabacurau is
    # never used here.
    handle = "synthetic_wrong_mapping_handle"
    context.vmc_handle = handle
    context.vmc_wrong_venue = _add_venue(context, "Synthetic Wrong Venue", handle)
    context.vmc_target_labels = []
    for i in range(2):
        label = f"synthetic_{i}"
        _insert(
            context, label=label, handle=handle, venue_id=context.vmc_wrong_venue,
            location_text="Synthetic Correct Venue", status="accepted",
        )
        context.vmc_target_labels.append(label)


@given("the correct venue is already known and already in the catalog")
def step_given_correct_venue_known(context):
    context.vmc_correct_venue = _add_venue(context, "Synthetic Correct Venue")


@when("the force-reassign correction runs for that handle from the wrong venue to the correct one")
def step_when_force_reassign_repoint(context):
    _run(
        context, mode=MODE_FORCE_REASSIGN, handles=[context.vmc_handle],
        from_venue_id=context.vmc_wrong_venue, to_venue_id=context.vmc_correct_venue,
    )


@then("every one of the handle's events is reattributed to the correct venue")
def step_then_scenario6_reattributed(context):
    _assert_targets_reattributed(context, context.vmc_correct_venue)


@then("none of them is left pointing at the wrong venue")
def step_then_none_point_to_wrong_venue(context):
    for label in context.vmc_target_labels:
        row = _row(context, label)
        assert row["venue_id"] != context.vmc_wrong_venue, (label, row)


# ── Scenarios 7-8: protections shared with the existing repair modes ─────────
@given("an event whose venue_id was corrected by an operator directly")
def step_given_operator_corrected_venue(context):
    _ensure(context)
    handle = "protection_test_handle"
    context.vmc_handle = handle
    context.vmc_operator_venue = _add_venue(context, "Protection Test Venue", handle)
    context.vmc_other_venue = _add_venue(context, "Some Other Venue")
    _insert(
        context, label="operator_edited", handle=handle, venue_id=context.vmc_operator_venue,
        location_text="whatever", status="accepted", operator_edited_fields=["venue_id"],
    )
    context.vmc_force_from = context.vmc_operator_venue
    context.vmc_force_to = context.vmc_other_venue


@given("an event that is confirmed, manually linked, or superseded")
def step_given_confirmed_manual_superseded(context):
    _ensure(context)
    handle = "skip_table_test_handle"
    context.vmc_handle = handle
    context.vmc_skip_venue = _add_venue(context, "Skip Table Test Venue", handle)
    _insert(
        context, label="confirmed", handle=handle, venue_id=context.vmc_skip_venue,
        location_text="x", status="confirmed",
    )
    _insert(
        context, label="manual_link", handle=handle, venue_id=context.vmc_skip_venue,
        location_text="x", status="accepted",
    )
    context.vmc_dao.update_event(context.vmc_events["manual_link"], {"location_resolution": "manual"})
    _insert(
        context, label="superseded", handle=handle, venue_id=context.vmc_skip_venue,
        location_text="x", status="superseded",
    )
    context.vmc_force_from = context.vmc_skip_venue
    context.vmc_force_to = None


@when("either new correction mode is run over the handle that event belongs to")
def step_when_either_mode_runs(context):
    handle = context.vmc_handle
    _run(
        context, mode=MODE_FORCE_REASSIGN, handles=[handle],
        from_venue_id=context.vmc_force_from, to_venue_id=context.vmc_force_to,
    )
    context.vmc_force_reassign_report = context.vmc_report
    _run(context, mode=MODE_UNRESOLVED_VENUE, handles=[handle])
    context.vmc_unresolved_venue_report = context.vmc_report


@then("that event's venue_id is left exactly as the operator set it")
def step_then_operator_venue_unchanged(context):
    row = _row(context, "operator_edited")
    assert row["venue_id"] == context.vmc_operator_venue, row


@then("that event is skipped and reported as skipped, not silently ignored")
def step_then_skipped_and_reported(context):
    fr_report = context.vmc_force_reassign_report
    assert fr_report.skipped_by_reason.get(SKIP_CONFIRMED, 0) >= 1, fr_report.skipped_by_reason
    assert fr_report.skipped_by_reason.get(SKIP_MANUAL_LINK, 0) >= 1, fr_report.skipped_by_reason
    assert fr_report.skipped_by_reason.get(SKIP_SUPERSEDED, 0) >= 1, fr_report.skipped_by_reason
    for label, expected_status in (
        ("confirmed", "confirmed"), ("manual_link", "accepted"), ("superseded", "superseded"),
    ):
        row = _row(context, label)
        assert row["venue_id"] == context.vmc_skip_venue, (label, row)
        assert row["status"] == expected_status, (label, row)


# ── Scenarios 9-10: idempotency ───────────────────────────────────────────────
@given("a handle whose events were already corrected by a first force-reassign run")
def step_given_already_force_reassigned(context):
    _ensure(context)
    handle = "idempotency_fr_handle"
    context.vmc_handle = handle
    context.vmc_correct_venue = _add_venue(context, "Idempotency Test Venue", handle)
    context.vmc_target_labels = []
    for i in range(2):
        label = f"idem_{i}"
        _insert(
            context, label=label, handle=handle, venue_id=None,
            location_text="whatever", status="pending_review", review_reason="unresolved_venue",
        )
        context.vmc_target_labels.append(label)
    _run(
        context, mode=MODE_FORCE_REASSIGN, handles=[handle],
        from_venue_id=None, to_venue_id=context.vmc_correct_venue,
    )
    context.vmc_snapshot = {label: dict(_row(context, label)) for label in context.vmc_target_labels}


@when("the force-reassign correction runs again for the same handle with the same correction")
def step_when_force_reassign_runs_again(context):
    _run(
        context, mode=MODE_FORCE_REASSIGN, handles=[context.vmc_handle],
        from_venue_id=None, to_venue_id=context.vmc_correct_venue,
    )


@then("no event's venue_id, status, or review reason changes")
def step_then_no_field_changes(context):
    for label in context.vmc_target_labels:
        before = context.vmc_snapshot[label]
        after = _row(context, label)
        assert after["venue_id"] == before["venue_id"], (label, before, after)
        assert after["status"] == before["status"], (label, before, after)
        assert after["review_reason"] == before["review_reason"], (label, before, after)


@then("no row is written")
def step_then_no_row_written(context):
    report = context.vmc_report
    assert report.changed_count == 0, report
    assert report.selected == 0, report.rows


@given("a handle whose events were already evaluated by a first unresolved-venue run")
def step_given_already_unresolved_venue_evaluated(context):
    _ensure(context)
    handle = "idempotency_uv_handle"
    context.vmc_handle = handle
    context.vmc_resolves_venue = _add_venue(context, "Resolvable Venue", "resolvablevenuehandle")
    _insert(
        context, label="resolves", handle=handle, venue_id=None,
        location_text="@resolvablevenuehandle", status="pending_review",
        review_reason="unresolved_venue",
    )
    _insert(
        context, label="stays_unresolved", handle=handle, venue_id=None,
        location_text="nothing specific here", status="pending_review",
        review_reason="unresolved_venue",
    )
    _run(context, mode=MODE_UNRESOLVED_VENUE, handles=[handle])
    context.vmc_snapshot = {
        label: dict(_row(context, label)) for label in ("resolves", "stays_unresolved")
    }


@when("the unresolved-venue correction runs again for the same handle")
def step_when_unresolved_venue_runs_again(context):
    _run(context, mode=MODE_UNRESOLVED_VENUE, handles=[context.vmc_handle])


@then("no event that stayed unresolved the first time is written to again")
def step_then_unresolved_stays_same(context):
    before = context.vmc_snapshot["stays_unresolved"]
    after = _row(context, "stays_unresolved")
    assert after == before, (before, after)
    assert context.vmc_report.changed_count == 0, context.vmc_report


@then("no event that resolved the first time is re-evaluated differently")
def step_then_resolved_stays_same(context):
    before = context.vmc_snapshot["resolves"]
    after = _row(context, "resolves")
    assert after["venue_id"] == context.vmc_resolves_venue, after
    assert after == before, (before, after)
