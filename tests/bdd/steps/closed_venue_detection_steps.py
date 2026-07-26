"""Behave steps for tests/bdd/persistence/closed-venue-detection.feature.

Closure is evidence-derived and reversible: a venue whose newest review reports
it permanently closed is flagged and drops out of the serving view, but its
lifecycle, RDS row and enrichment are untouched, so a newer ordinary review
brings it back on the next cycle.

Drives the RDS layer built in environment.py — context.repository (RDS-backed
DAO), context.rds_store (fake truth), context.redis_only_dao (the Redis serving
projection), context.redis_projection_service (the projector).
"""
from __future__ import annotations

import copy
import re

from behave import given, then, when  # type: ignore[import-untyped]

from app.models import Venue
from app.models.venue_review import VenueReview, VenueReviews

_LAT, _LNG = -8.05, -34.88

# A date comfortably older than any "recent" evidence used in the scenarios.
_OLD = "2019-04-01T12:00:00Z"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "venue"


def _iso(date_str: str) -> str:
    """Accept a bare YYYY-MM-DD from the Gherkin and store a full timestamp."""
    date_str = (date_str or "").strip()
    return date_str if "T" in date_str else f"{date_str}T12:00:00Z"


def _seed_venue(context, name: str) -> str:
    venue_id = _slug(name)
    context.repository.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id=venue_id,
            venue_name=name,
            venue_address=f"{venue_id} address",
            venue_lat=_LAT,
            venue_lng=_LNG,
            venue_type="BAR",
        )
    )
    context.named_ids = getattr(context, "named_ids", {})
    context.named_ids[name] = venue_id
    return venue_id


def _id_of(context, name: str) -> str:
    ids = getattr(context, "named_ids", {})
    return ids.get(name) or _seed_venue(context, name)


def _reviews_of(context, venue_id: str) -> list[VenueReview]:
    return list(getattr(context, "_reviews", {}).get(venue_id, []))


def _write_reviews(context, venue_id: str, reviews: list[VenueReview]) -> None:
    context._reviews = getattr(context, "_reviews", {})
    context._reviews[venue_id] = reviews
    context.repository.set_venue_reviews(
        VenueReviews(venue_id=venue_id, reviews=reviews)
    )


def _review(text: str, publish_time: str | None, rating: int = 5) -> VenueReview:
    return VenueReview(
        author_name="Reviewer",
        rating=rating,
        text=text,
        relative_time="recentemente",
        language="pt",
        publish_time=publish_time,
    )


def _servable(context) -> set[str]:
    return set(context.repository.list_servable_venue_ids())


def _signal(context, venue_id: str):
    """The recorded closure signal for a venue, or None."""
    return (getattr(context, "closure_signals", {}) or {}).get(venue_id)


# ── Background ────────────────────────────────────────────────────────────────
@given("closure detection is enabled")
def step_detection_enabled(context):
    context.closure_enabled = True
    context.closure_signals = {}


@given("closure detection is disabled")
def step_detection_disabled(context):
    context.closure_enabled = False
    context.closure_signals = {}


@given('the venue "{name}" is active and servable')
def step_active_servable(context, name):
    venue_id = _seed_venue(context, name)
    assert venue_id in _servable(context), (
        f"{name} must start servable; view returned {_servable(context)}"
    )


# ── Given: review evidence ────────────────────────────────────────────────────
@given('the venue "{name}" has a review published "{date}" saying "{text}"')
def step_review_published(context, name, date, text):
    venue_id = _id_of(context, name)
    reviews = _reviews_of(context, venue_id)
    reviews.append(_review(text, _iso(date)))
    _write_reviews(context, venue_id, reviews)


@given('its remaining reviews were published before "{date}"')
def step_remaining_reviews_older(context, name=None, date=None):
    venue_id = context.named_ids[list(context.named_ids)[-1]]
    reviews = _reviews_of(context, venue_id)
    reviews.extend(
        [
            _review("cerveja gelada e otimo atendimento", "2023-05-08T12:00:00Z"),
            _review("bar super descolado, boa musica", "2021-08-03T12:00:00Z"),
        ]
    )
    _write_reviews(context, venue_id, reviews)


@given('the venue "{name}" has a newest review saying "{text}"')
def step_newest_review(context, name, text):
    venue_id = _id_of(context, name)
    _write_reviews(
        context,
        venue_id,
        [
            _review(text, "2026-06-01T12:00:00Z"),
            _review("lugar bacana", _OLD),
        ],
    )


@given('the venue "{name}" has review evidence that yields confidence "low"')
def step_low_confidence_evidence(context, name):
    """A closure claim contradicted by an equally recent ordinary review."""
    venue_id = _id_of(context, name)
    _write_reviews(
        context,
        venue_id,
        [
            _review("esse bar fechou", "2026-06-02T12:00:00Z"),
            _review("estivemos ontem, tudo funcionando", "2026-06-01T12:00:00Z"),
        ],
    )


@given('the venue "{name}" has no reviews')
def step_no_reviews(context, name):
    _write_reviews(context, _id_of(context, name), [])


@given('the venue "{name}" has only reviews with no publish time')
def step_reviews_without_publish_time(context, name):
    venue_id = _id_of(context, name)
    _write_reviews(
        context,
        venue_id,
        [_review("esse bar fechou", None), _review("fechou mesmo", None)],
    )


@given('the venue "{name}" has a malformed review payload')
def step_malformed_reviews(context, name):
    venue_id = _seed_venue(context, name)
    context.malformed_ids = getattr(context, "malformed_ids", set())
    context.malformed_ids.add(venue_id)
    # Bypass the model and write a structurally broken payload straight to the
    # store, mimicking a poisoned row rather than a validation error at write.
    context.rds_store.upsert_enrichment(
        "google_places.reviews",
        venue_id,
        {"reviews": "not-a-list"},
        history=False,
    )


@given('the venue "{name}" is flagged closed with confidence "{confidence}"')
def step_given_flagged(context, name, confidence):
    venue_id = _id_of(context, name)
    _write_reviews(
        context,
        venue_id,
        [_review("esse bar fechou", "2026-01-12T12:00:00Z"), _review("bom bar", _OLD)],
    )
    _run_detection(context)
    signal = _signal(context, venue_id)
    assert signal is not None and signal.closed, (
        f"{name} must be flagged closed before this scenario continues; got {signal}"
    )
    assert signal.confidence == confidence, (
        f"expected confidence {confidence}, got {signal.confidence}"
    )


@given('the venue "{name}" is absent from the serving projection')
@then('the venue "{name}" is absent from the serving projection')
def step_absent_from_projection(context, name):
    venue_id = _id_of(context, name)
    _rebuild_if_needed(context)
    assert venue_id not in _servable(context), (
        f"{name} must not be in the serving view; view={_servable(context)}"
    )
    assert context.redis_only_dao.get_venue(venue_id) is None, (
        f"{name} must not be projected to Redis"
    )


# ── When ──────────────────────────────────────────────────────────────────────
def _run_detection(context):
    """Run closure detection, tolerating its absence so the failure is a
    meaningful assertion rather than an import error."""
    context.closure_summary = {"errors": [], "flagged": 0}
    try:
        from app.services.closure_detection_service import ClosureDetectionService
    except ImportError:
        context.closure_signals = {}
        context.closure_service_missing = True
        return
    service = ClosureDetectionService(
        rds_store=context.rds_store,
        admin_config_service=getattr(context, "admin_config_service", None),
        enabled=lambda: bool(getattr(context, "closure_enabled", True)),
    )
    context.closure_summary = service.run()
    context.closure_signals = service.list_signals()


@when("closure detection runs")
def step_run_detection(context):
    _run_detection(context)


@when('a review published "{date}" saying "{text}" is added')
def step_add_review(context, date, text):
    venue_id = context.named_ids[list(context.named_ids)[-1]]
    reviews = _reviews_of(context, venue_id)
    reviews.append(_review(text, _iso(date)))
    _write_reviews(context, venue_id, reviews)


def _rebuild_if_needed(context):
    # NOTE: the `When the serving projection is rebuilt` step itself is already
    # defined in discovery_hardening_geofence_steps.py (behave's step registry is
    # global, so redefining it raises AmbiguousStep). This helper is the shared
    # implementation used by the Then-steps that must reconcile before asserting.
    context.projection_summary = context.redis_projection_service.rebuild_redis_from_rds()


@when("an operator requests the closed-venue report")
def step_operator_report(context):
    context.closed_report = context.client.get("/admin/venues/closed")


# ── Then ──────────────────────────────────────────────────────────────────────
@then('the venue "{name}" is flagged closed with confidence "{confidence}"')
def step_then_flagged(context, name, confidence):
    venue_id = _id_of(context, name)
    signal = _signal(context, venue_id)
    assert signal is not None, (
        f"{name} was not flagged closed (no signal recorded). "
        f"service_missing={getattr(context, 'closure_service_missing', False)}"
    )
    assert signal.closed, f"{name} signal exists but closed is False: {signal}"
    assert signal.confidence == confidence, (
        f"{name}: expected confidence {confidence}, got {signal.confidence}"
    )


@then('the venue "{name}" is not flagged closed')
def step_then_not_flagged(context, name):
    venue_id = _id_of(context, name)
    signal = _signal(context, venue_id)
    assert signal is None or not signal.closed, (
        f"{name} must not be flagged closed, got {signal}"
    )


@then('the recorded evidence names the review published "{date}"')
def step_evidence_date(context, date):
    venue_id = context.named_ids[list(context.named_ids)[-1]]
    signal = _signal(context, venue_id)
    assert signal is not None, "no closure signal recorded"
    assert signal.evidence_publish_time is not None, "no evidence date recorded"
    assert str(signal.evidence_publish_time).startswith(date.strip()), (
        f"expected evidence dated {date}, got {signal.evidence_publish_time}"
    )


@then('the venue "{name}" is present in the serving projection')
def step_present_in_projection(context, name):
    venue_id = _id_of(context, name)
    _rebuild_if_needed(context)
    assert venue_id in _servable(context), (
        f"{name} must be in the serving view; view={_servable(context)}"
    )
    assert context.redis_only_dao.get_venue(venue_id) is not None, (
        f"{name} must be projected to Redis"
    )


@then('the venue "{name}" still has lifecycle status "{status}"')
def step_lifecycle_unchanged(context, name, status):
    venue = context.repository.get_venue(_id_of(context, name))
    assert venue is not None, f"{name} row disappeared"
    actual = context.rds_store.venues[_id_of(context, name)].get(
        "lifecycle_status", "active"
    )
    assert actual == status, f"expected lifecycle {status}, got {actual}"


@then('the stored venue row and enrichment records for "{name}" are unchanged')
def step_row_unchanged(context, name):
    venue_id = _id_of(context, name)
    row = context.rds_store.venues.get(venue_id)
    assert row is not None, f"{name} row disappeared"
    assert row.get("deleted_at") is None, f"{name} must not be soft-deleted"
    reviews = context.repository.get_venue_reviews(venue_id)
    assert reviews is not None, f"{name} enrichment (reviews) must survive flagging"


@then('the run summary reports at least one error naming "{name}"')
def step_summary_error(context, name):
    venue_id = _id_of(context, name)
    summary = getattr(context, "closure_summary", {}) or {}
    errors = summary.get("error_venues") or summary.get("errors") or []
    assert any(venue_id in str(entry) for entry in errors), (
        f"expected an error naming {venue_id}; summary={summary}"
    )


@then('the report lists "{name}" with its reason, confidence, evidence date and matched phrase')
def step_report_lists(context, name):
    response = getattr(context, "closed_report", None)
    assert response is not None, "operator report was never requested"
    assert response.status_code == 200, (
        f"closed-venue report returned {response.status_code}: {response.text[:200]}"
    )
    venue_id = _id_of(context, name)
    entries = response.json()
    entries = entries.get("venues", entries) if isinstance(entries, dict) else entries
    match = next((e for e in entries if e.get("venue_id") == venue_id), None)
    assert match is not None, f"{name} missing from the report: {entries}"
    for field in ("reason", "confidence", "evidence_publish_time", "matched_phrase"):
        assert match.get(field) is not None, f"report entry missing {field}: {match}"
