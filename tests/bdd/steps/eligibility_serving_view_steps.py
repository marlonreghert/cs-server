"""Behave steps for tests/bdd/persistence/eligibility-serving-view.feature.

Eligibility is a dynamic serving view (active AND eligible under the live
admin.eligibility_rule block-list), not a destructive soft-delete. These steps
drive the RDS layer built in environment.py (context.repository = RDS-backed DAO,
context.rds_store = fake truth, context.redis_only_dao = Redis serving projection,
context.redis_projection_service = the projector, context.eligibility_rule_service
= the block-list editor). The view's equivalence to evaluate() on real Postgres is
pinned separately by tests/test_eligibility_serving_view_parity.py.
"""
from __future__ import annotations

import re

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import Venue
from app.models.vibe_attributes import VibeAttributes

_LAT, _LNG = -8.05, -34.88


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "venue"


def _seed_venue(
    context,
    name: str,
    *,
    venue_id: str | None = None,
    venue_type: str | None = None,
    google_type: str | None = None,
) -> str:
    venue_id = venue_id or _slug(name)
    context.repository.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id=venue_id,
            venue_name=name,
            venue_address=f"{venue_id} address",
            venue_lat=_LAT,
            venue_lng=_LNG,
            venue_type=venue_type,
        )
    )
    if google_type is not None:
        context.repository.set_vibe_attributes(
            VibeAttributes(
                venue_id=venue_id,
                google_place_id=f"place_{venue_id}",
                google_primary_type=google_type,
            )
        )
    return venue_id


def _servable(context) -> set[str]:
    return set(context.repository.list_servable_venue_ids())


# ── Background ────────────────────────────────────────────────────────────────
@given("the venue eligibility serving view is available")
def step_view_available(context):
    # The RDS layer (repository + fake store + projector + rule service) is built
    # per scenario in environment.py; nothing to wire here. Track named venues.
    context.named_ids = {}


# ── Given: venues ─────────────────────────────────────────────────────────────
@given("an active venue with a blocked Google type")
def step_active_blocked_google(context):
    # "supermarket" is blocked by the default block-list (no rule rows needed).
    context.named_ids["blocked"] = _seed_venue(
        context, "Blocked Market", venue_id="blocked_gtype", google_type="supermarket"
    )


@given("an active venue with an allowed Google type")
def step_active_allowed_google(context):
    context.named_ids["allowed"] = _seed_venue(
        context, "Allowed Bar", venue_id="allowed_gtype", google_type="bar"
    )


@given("an unlabeled active venue with no Google type")
def step_active_unlabeled(context):
    context.named_ids["unlabeled"] = _seed_venue(
        context, "Unlabeled Spot", venue_id="unlabeled"
    )


@given("an active venue whose Google type is currently blocked and absent from the view")
def step_active_blocked_by_rule(context):
    # Block a custom type via an explicit rule, then seed a venue of that type.
    context.eligibility_rule_service.add_rule("blocked_google_type", "arcade", updated_by="test")
    vid = _seed_venue(context, "Arcade Bar", venue_id="rule_blocked", google_type="arcade")
    context.subject_id = vid
    assert vid not in _servable(context), "venue should start absent from the view"
    context.subject_blocked_type = "arcade"


@given("an active venue whose Google type is currently allowed and present in the view")
def step_active_allowed_then_block(context):
    vid = _seed_venue(context, "Arcade Bar", venue_id="rule_allowed", google_type="arcade")
    context.subject_id = vid
    assert vid in _servable(context), "venue should start present in the view"
    context.subject_blocked_type = "arcade"


@given("an active venue whose name matches an ambiguous keyword")
def step_active_ambiguous_keyword(context):
    # "mercado" is a default ambiguous keyword. Good-category (BAR) -> stays in view.
    context.named_ids["good"] = _seed_venue(
        context, "Bar do Mercado", venue_id="ambiguous_good", venue_type="BAR"
    )


@given("the venue resolves to a good category")
def step_resolves_good_category(context):
    # Asserted by seeding venue_type=BAR above; nothing further to set.
    assert context.named_ids.get("good")


@given("a venue that has just left the serving view")
def step_venue_left_view(context):
    # Seed active + eligible, project it to Redis, THEN block its type so it leaves
    # the view while remaining active (reversible, no lifecycle change).
    vid = _seed_venue(context, "Leaving Arcade", venue_id="leaving", google_type="arcade")
    venue = context.repository.get_venue(vid)
    context.redis_only_dao.upsert_venue(venue)  # already projected to Redis
    assert context.redis_only_dao.get_venue(vid) is not None
    context.eligibility_rule_service.add_rule("blocked_google_type", "arcade", updated_by="test")
    context.left_id = vid


@given("a venue that has just entered the serving view")
def step_venue_entered_view(context):
    # Active + eligible but not yet projected to Redis.
    vid = _seed_venue(context, "Entering Bar", venue_id="entering", google_type="bar")
    assert context.redis_only_dao.get_venue(vid) is None
    context.entered_id = vid


@given("an active venue excluded from the serving view")
def step_active_excluded(context):
    context.excluded_id = _seed_venue(
        context, "Excluded Pharmacy", venue_id="excluded", google_type="pharmacy"
    )


@given("an unlabeled active venue in the serving view")
def step_active_unlabeled_in_view(context):
    context.unlabeled_id = _seed_venue(context, "Unlabeled Cantina", venue_id="unlabeled_in_view")


@given("a venue deprecated as permanently closed")
def step_deprecated_closed(context):
    vid = _seed_venue(context, "Closed Forever", venue_id="closed", google_type="bar")
    context.repository.soft_delete_venue(
        vid, "google_places_closed_permanently", "google_places",
        google_business_status="CLOSED_PERMANENTLY",
    )
    context.closed_id = vid


# ── When ──────────────────────────────────────────────────────────────────────
@when("the serving view is evaluated")
@when("the serving view is re-evaluated")
def step_evaluate_view(context):
    context.servable = _servable(context)


@when("the operator removes that Google type from the block-list")
def step_remove_from_blocklist(context):
    context.eligibility_rule_service.remove_rule(
        "blocked_google_type", context.subject_blocked_type, updated_by="test"
    )


@when("the operator adds that Google type to the block-list")
def step_add_to_blocklist(context):
    context.eligibility_rule_service.add_rule(
        "blocked_google_type", context.subject_blocked_type, updated_by="test"
    )


@when("the projector runs")
def step_projector_runs(context):
    context.projection_summary = context.redis_projection_service.rebuild_redis_from_rds()


@when("an enrichment job selects venues to process")
def step_enrichment_selects(context):
    # The enrichment gate IS the serving view: jobs enumerate servable ids.
    context.enrichment_selection = _servable(context)


# ── Then: view membership ─────────────────────────────────────────────────────
@then("the allowed venue is in the view")
def step_allowed_in_view(context):
    assert context.named_ids["allowed"] in context.servable, context.servable


@then("the unlabeled venue is in the view")
def step_unlabeled_in_view(context):
    assert context.named_ids["unlabeled"] in context.servable, context.servable


@then("the blocked-type venue is absent from the view")
def step_blocked_absent(context):
    assert context.named_ids["blocked"] not in context.servable, context.servable


@then("the venue is present in the view")
def step_subject_present(context):
    assert context.subject_id in _servable(context)


@then("the venue is absent from the view")
def step_subject_absent(context):
    target = getattr(context, "subject_id", None) or getattr(context, "closed_id", None)
    assert target is not None
    assert target not in _servable(context)


@then("the venue is in the view")
def step_good_in_view(context):
    assert context.named_ids["good"] in context.servable, context.servable


@then("a non-good-category venue matching the same keyword is absent from the view")
def step_nongood_absent(context):
    # Labeled (Google type present) + non-good category + ambiguous keyword
    # -> high-confidence ineligible -> excluded.
    vid = _seed_venue(
        context, "Mercado Central", venue_id="ambiguous_nongood",
        google_type="amusement_park",
    )
    assert vid not in _servable(context)


# ── Then: lifecycle invariance ────────────────────────────────────────────────
@then("the venue lifecycle remained active throughout")
@then("the venue lifecycle remained active")
def step_lifecycle_active(context):
    venue = context.repository.get_venue(context.subject_id)
    assert venue is not None and venue.is_active(), venue


# ── Then: projector reconcile ─────────────────────────────────────────────────
@then("the venue that left the view is removed from the Redis geo index and venue key")
def step_left_removed(context):
    assert context.redis_only_dao.get_venue(context.left_id) is None
    nearby = {v.venue_id for v in context.redis_only_dao.get_nearby_venues(_LAT, _LNG, 1.0)}
    assert context.left_id not in nearby, nearby


@then("the venue that entered the view is projected to Redis")
def step_entered_projected(context):
    assert context.redis_only_dao.get_venue(context.entered_id) is not None
    nearby = {v.venue_id for v in context.redis_only_dao.get_nearby_venues(_LAT, _LNG, 1.0)}
    assert context.entered_id in nearby, nearby


# ── Then: enrichment gate ─────────────────────────────────────────────────────
@then("the view-excluded venue is skipped")
def step_excluded_skipped(context):
    assert context.excluded_id not in context.enrichment_selection


@then("the unlabeled venue is enriched")
def step_unlabeled_enriched(context):
    assert context.unlabeled_id in context.enrichment_selection
