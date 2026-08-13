"""Behave steps for tests/bdd/enrichment/handle-attribution-hardening.feature.

See plans/260813_handle-attribution-hardening.md.

Drives the SAME real `PromoterCrawlService._process_post` pipeline over the
SAME in-memory RDS fake `event_attribution_and_dates_steps.py` already built
for plans/260812_event-attribution-and-dates.md — this plan fixes what
happens AFTER that plan's §A ladder ordering, not a parallel pipeline, so
this file reuses that harness's private helpers (`_ensure_eadb`, `_add_venue`,
`_process_one_event`, `_eadb_event`, `_run`) rather than building a second
one. Step texts already registered there (background venue setup, "an
extracted event whose location text is ...", "the event is attributed", "the
event links to ...") are reused verbatim — never redefined here, which would
raise behave's AmbiguousStep.
"""
from __future__ import annotations

import re

from behave import given, then  # type: ignore[import-untyped]

from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_venue_resolution import METHOD_NAME_MATCH, build_handle_index, build_venue_catalog
from tests.bdd.steps.event_attribution_and_dates_steps import (
    _add_venue,
    _ensure_eadb,
    _eadb_event,
    _event_payload,
    _run,
)


def _add_venue_no_handle(context, name: str) -> str:
    """The SAME `VenueLite`-backing shape `_add_venue` builds, minus the
    Instagram handle — for "Maria Café"/"Espaço Tucano"-style venues the
    catalog carries by NAME only. `venue_id` is derived from the name
    (never a handle, since there is none) so it stays stable across the
    repeated Background setup every scenario in this feature re-runs."""
    _ensure_eadb(context)
    slug = re.sub(r"[^a-z0-9]+", "", name.lower()) or "venue"
    venue_id = f"v_nohandle_{slug}"
    context.eadb_dao.upsert_venue(Venue(
        venue_id=venue_id, venue_name=name, venue_lat=-8.05, venue_lng=-34.88,
        venue_address="",
    ))
    context.eadb_venues_by_name[name] = venue_id
    return venue_id


@given('the venue "{name}" with no Instagram handle')
def step_given_venue_with_no_instagram_handle(context, name):
    _add_venue_no_handle(context, name)


@given("its post caption names no known venue")
def step_given_caption_names_no_known_venue(context):
    _ensure_eadb(context)
    context.eadb_caption_text = "Confira a programação de hoje! Ingressos abertos."


@then('the stored event is queued for review with reason "{reason}"')
def step_then_stored_event_queued_with_reason(context, reason):
    """End-to-end on the STORED row, deliberately — the defect this plan
    fixes is precisely that `resolve_event_venue`'s answer was already
    right while the persisted `review_reason` was not (see
    plans/260813_handle-attribution-hardening.md §B); asserting on the
    resolver's return value alone would have stayed green while that bug
    was live."""
    row = _eadb_event(context)
    assert reason in (row.get("review_reason") or ""), row


@then('the stored event is not queued for review with reason "{reason}"')
def step_then_stored_event_not_queued_with_reason(context, reason):
    row = _eadb_event(context)
    assert reason not in (row.get("review_reason") or ""), row


@then("no name match is attempted for that event")
def step_then_no_name_match_attempted(context):
    """rung 4 producing NO candidate at all for this event — checked on the
    PERSISTED `event_venue_link_candidate` rows, not on an internal call
    count, so this proves what actually reached the database rather than an
    implementation detail of how the ladder got there."""
    row = _eadb_event(context)
    candidates = context.eadb_dao.list_event_venue_link_candidates(row["event_id"])
    methods = [c["method"] for c in candidates]
    assert METHOD_NAME_MATCH not in methods, candidates


# ── the 2026-08-13 production crawl — the six links that were already
# correct on the live run, replayed here so a fix for the three wrong ones
# cannot silently break them ─────────────────────────────────────────────
_PRODUCTION_CRAWL_LINKS = [
    ("Seu Chico Botequim", "seuchicobotequim"),
    ("Sempre Rock Bar", "semprerockbar"),
    ("Taverna Pub Medieval Bar & Avalon Events", "tavernapubnatal"),
    ("Ô Bar Restaurante - Ponta Negra", "obarpontanegra"),
    ("Bar 54", "bar54_"),
]


def _ensure_production_crawl_venue(context, name: str, handle: str) -> str:
    """Reuses an EXISTING same-named venue from an earlier scenario in this
    feature (the Background/`_ensure_eadb` catalog persists across
    scenarios by design — see event_attribution_and_dates_steps.py's own
    docstring) rather than creating a second venue_id under the same
    display name, attaching just the handle if the venue is already there."""
    _ensure_eadb(context)
    existing_id = context.eadb_venues_by_name.get(name)
    if existing_id is not None:
        context.eadb_dao.set_venue_instagram(
            VenueInstagram(venue_id=existing_id, instagram_handle=handle, status="found")
        )
        return existing_id
    return _add_venue(context, name, handle)


@given("the production events of the 2026-08-13 promoter crawl")
def step_given_production_crawl_events(context):
    _ensure_eadb(context)
    for name, handle in _PRODUCTION_CRAWL_LINKS:
        _ensure_production_crawl_venue(context, name, handle)

    context.eadb_shortcode_counter += 1
    shortcode = f"eadb_prodcrawl_{context.eadb_shortcode_counter}"
    caption = "Roteiro de hoje em Natal! Confira a programação completa, ingressos abertos."
    events = [
        _event_payload(title=f"Evento {handle}", location_text=f"@{handle}")
        for _name, handle in _PRODUCTION_CRAWL_LINKS
    ]
    context.eadb_openai.program_events(events)
    post = {
        "shortcode": shortcode, "caption": caption,
        "permalink": f"https://instagram.com/p/{shortcode}",
        "timestamp": context.eadb_now.isoformat(),
        "image_urls": [], "location_tag": None,
    }
    venues = build_venue_catalog(context.eadb_dao)
    handle_index = build_handle_index(context.eadb_dao)
    _run(context.eadb_service._process_post(
        handle=context.eadb_promoter_handle, post=post, venues=venues,
        handle_index=handle_index, now=context.eadb_now,
        location_text_fallback_to_caption=False,
    ))
    context.eadb_production_crawl_shortcode = shortcode


@then('"{handle}" links to "{venue_name}"')
def step_then_production_handle_links_to(context, handle, venue_name):
    # The quoted Gherkin capture already includes the leading "@"
    # ("@seuchicobotequim" links to ...) — `handle` here is that full
    # string, matched directly against the stored `location_text`.
    rows = context.eadb_dao.list_events_by_source(
        context.eadb_promoter_handle, context.eadb_production_crawl_shortcode,
    )
    matching = [r for r in rows if r.get("location_text") == handle]
    assert len(matching) == 1, (handle, rows)
    row = matching[0]
    expected_id = context.eadb_venues_by_name[venue_name]
    assert row["venue_id"] == expected_id, (handle, venue_name, row)
