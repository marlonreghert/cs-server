"""Behave steps for tests/bdd/enrichment/venue-name-matching.feature.

"A match" is not an abstract score: it is whether the venue-website tier's
confidence clears the bar production actually uses. Every pair here was measured
against a real venue.
"""
from __future__ import annotations

import difflib

from behave import given, then  # type: ignore[import-untyped]

# Production: accept 0.8, minus the existence bonus that can never be collected.
BAR = 0.65


def _weights():
    from app.services.instagram_cascade_service import NAME_WEIGHT, PROVENANCE_WEIGHT
    from app.services.instagram_handle_sources import SOURCE_VENUE_WEBSITE

    return PROVENANCE_WEIGHT[SOURCE_VENUE_WEBSITE], NAME_WEIGHT


def _confidence(venue, handle):
    from app.services.instagram_cascade_service import name_similarity

    prov, weight = _weights()
    return min(prov + weight * name_similarity(venue, None, handle), 1.0)


@given('a venue called "{name}"')
def step_venue_called(context, name):
    context.match_venue = name


@given('a candidate handle "{handle}"')
def step_candidate_handle(context, handle):
    context.match_handle = handle


@then("the names are considered a match")
def step_is_match(context):
    from app.services.instagram_cascade_service import name_similarity

    conf = _confidence(context.match_venue, context.match_handle)
    sim = name_similarity(context.match_venue, None, context.match_handle)
    assert conf >= BAR, (
        f"similarity {sim:.3f} -> confidence {conf:.3f} < {BAR}: "
        f"{context.match_venue!r} vs @{context.match_handle} was rejected on a "
        "cosmetic difference"
    )


@then("the names are not considered a match")
def step_is_not_match(context):
    conf = _confidence(context.match_venue, context.match_handle)
    assert conf < BAR, (
        f"confidence {conf:.3f} >= {BAR}: {context.match_venue!r} accepted "
        f"@{context.match_handle}, which belongs to somebody else"
    )


@then("the score is at least the plain comparison score")
def step_monotonic(context):
    from app.services.instagram_cascade_service import name_similarity

    plain = difflib.SequenceMatcher(
        None,
        context.match_venue.strip().lower(),
        context.match_handle.replace(".", " ").replace("_", " ").lower(),
    ).ratio()
    got = name_similarity(context.match_venue, None, context.match_handle)
    assert got >= plain - 1e-9, (
        f"{got:.3f} < {plain:.3f} — the new comparison must only ever ADD "
        "evidence, never remove it"
    )
