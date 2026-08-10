"""Behave steps for
tests/bdd/enrichment/stream-dedupe-and-venue-attribution.feature.

Shares the SAME context/fakes `scheduled_incremental_instagram_crawl_steps.py`
already built (imported, never duplicated) — the Background step ("Given the
Instagram crawl scheduler is configured") and several `Given`/`When` steps
this feature's scenarios reuse verbatim (e.g. "a crawl target with reels
enabled", "its scheduled crawl runs") are defined THERE and picked up by
behave's shared step registry; this module only adds the step texts that are
genuinely new to this feature.

Two extra fakes this feature's scenarios need that the shared module does not
wire by default: a photo classifier (to prove a duplicated post is not
classified twice) and a real `PromoterCrawlService` instance (reused,
unmodified, as `InstagramCrawlChainer._chain_shared_handle` calls its
`_archive_post_images`/`_process_post` for EVERY post of a shared handle —
see app/services/instagram_crawl_service.py. As of
plans/260810_post-kind-and-post-extraction-attribution.md §C, attribution
itself now runs AFTER extraction inside `_process_post`, using the model's
`location_text` with a caption fallback, rather than the raw-caption guess
this feature originally built — this file's fixtures still exercise that
outcome unchanged, since the fake OpenAI client's canned `location_text` is
null here, which is exactly what the fallback covers).
"""
from __future__ import annotations

from behave import given, then  # type: ignore[import-untyped]

from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS
from app.services.promoter_crawl_service import PromoterCrawlService
from tests.bdd.steps.scheduled_incremental_instagram_crawl_steps import (
    RECIFE_LAT,
    RECIFE_LNG,
    _create_target,
    _ensure_context,
    _extraction_json,
    _post,
    _program_posts,
)

_HANDLE = "entreamigosobode"
_VENUE_A_NAME = "Entre Amigos O Bode"
_VENUE_B_NAME = "Entre Amigos O Bode Espinheiro"
_SHORTCODE = "sharedpost1"
# Empirically verified against the REAL resolver (app.services.
# event_venue_resolution.resolve_event_venue) with these exact venue names —
# see the PR for the finding this uncovered: `name_similarity` was
# calibrated for comparing two short, name-like strings (a promoter's
# MODEL-EXTRACTED location_text against a venue name), not a whole raw
# caption. A caption that only mentions a branch word in passing often
# scores BELOW the confidence floor even when a human would resolve it
# instantly — this branch caption clears floor+margin because it closely
# echoes the venue's own name, which is what real captions do least
# reliably. Conservative-by-construction: exactly the plan's own posture
# ("a wrong venue is invisible ... an unresolved one is visibly waiting for
# a human").
_BRANCH_CAPTION = (
    "Vem pro Entre Amigos O Bode Espinheiro hoje! Forro e ingressos abertos, sexta 22h"
)
_NEITHER_CAPTION = "Happy hour hoje com musica ao vivo, venha conferir! Ingressos abertos."
_WEAK_CAPTION = "Hoje tem forro na nossa unidade principal! Ingressos abertos, sexta 22h"


class _FakeClassifier:
    """Mirrors tests/test_instagram_crawl_service.py's `_FakePhotoClassifier`,
    plus `photos_classified` — the count that actually distinguishes "one
    post, classified once" from "one post whose duplicate copy was
    classified in the same batched call": `annotate` is called ONCE per
    `_archive_venue_posts` invocation regardless of how many photos are in
    its batch, so a bare call count cannot tell a deduplicated batch of 1
    from an undeduplicated batch of 2 apart — the photo count can."""

    def __init__(self, category="flyer", confidence=0.9):
        self.category = category
        self.confidence = confidence
        self.calls = 0
        self.photos_classified = 0

    async def annotate(self, photos, *, derive_attributes=True, venue_id="", require_bytes=False):
        self.calls += 1
        self.photos_classified += len(photos)
        for photo in photos:
            photo["category"] = self.category
            photo["classification_confidence"] = self.confidence
        return {
            "classified": len(photos), "attributed": 0, "cost_usd": 0.0,
            "input_tokens": 0, "output_tokens": 0,
        }


def _create_named_venue(context, handle: str, venue_name: str, *, lat=RECIFE_LAT, lng=RECIFE_LNG) -> str:
    context.ic_venue_counter += 1
    venue_id = f"sdva_venue_{context.ic_venue_counter}"
    context.ic_dao.upsert_venue(
        Venue(venue_id=venue_id, venue_name=venue_name, venue_lat=lat, venue_lng=lng)
    )
    context.ic_dao.set_venue_instagram(
        VenueInstagram(venue_id=venue_id, instagram_handle=handle, status="found")
    )
    context.ic_venue_ids.append(venue_id)
    return venue_id


def _replace_post(context, *, caption: str) -> None:
    """(Re)programs the ONE post this scenario's target crawls, and arms
    exactly one extraction response — a post is archived under exactly one
    destination (resolved venue OR the handle), so exactly one of
    `EventExtractionService`/`PromoterCrawlService._process_post` ever calls
    the model for it."""
    _program_posts(context, context.ic_handle, "posts", [
        _post(context.sdva_shortcode, "2026-08-05T20:00:00.000Z", caption=caption),
    ])
    context.ic_openai._responses = []
    context.ic_openai.program(_extraction_json())


def _setup_two_venue_handle(context) -> None:
    _ensure_context(context)
    context.ic_handle = _create_target(context, _HANDLE, crawl_reels=False, link_venue=False)
    context.sdva_venue_a_id = _create_named_venue(context, _HANDLE, _VENUE_A_NAME, lat=-8.05, lng=-34.88)
    context.sdva_venue_b_id = _create_named_venue(context, _HANDLE, _VENUE_B_NAME, lat=-8.06, lng=-34.90)
    # §C's "queue it, do not guess" path reuses PromoterCrawlService's own
    # per-post archiving/persistence (`_archive_post_images`/`_process_post`)
    # for a post that does not resolve — the SAME instance `chain_promoter`
    # already calls this way for a promoter-kind target; here it is wired
    # directly onto the chainer, since these BDD targets are `kind="venue"`.
    context.ic_chainer.promoter_crawl_service = PromoterCrawlService(
        venue_dao=context.ic_dao, posts_client=None, media_store=context.ic_media_store,
        downloader=context.ic_downloader, openai_client=context.ic_openai,
        archive_source=SOURCE_INSTAGRAM_POSTS, min_confidence=0.0,
        now_provider=lambda: context.ic_now,
    )
    context.sdva_shortcode = _SHORTCODE
    # A default post that names neither venue — scenarios that reuse this
    # Given step without their own "And a post whose caption names ..." step
    # (9, 10) still exercise a REAL post through the ambiguous path, rather
    # than a vacuous run with nothing to attribute.
    _replace_post(context, caption=_NEITHER_CAPTION)


def _sole_event(context) -> dict:
    rows = context.ic_dao.list_events_by_source(context.ic_handle, context.sdva_shortcode)
    assert len(rows) == 1, rows
    return rows[0]


# ── Overlapping streams ──────────────────────────────────────────────────
@given("one post is returned by both the posts and the reels stream")
def step_given_post_in_both_streams(context):
    context.sdva_classifier = _FakeClassifier()
    context.ic_chainer.photo_classifier = context.sdva_classifier
    context.sdva_shortcode = "dualpost1"
    post = _post(
        context.sdva_shortcode, "2026-08-05T20:00:00.000Z",
        caption="Festa hoje! Ingressos abertos, vem cedo.",
    )
    # Two SEPARATE dict objects, same content — mirrors what two independent
    # Apify results for the SAME underlying post actually look like
    # (§Evidence: identical S3 key, not the same Python object).
    _program_posts(context, context.ic_handle, "posts", [dict(post)])
    _program_posts(context, context.ic_handle, "reels", [dict(post)])
    context.ic_openai.program(_extraction_json())


@then("that post is archived once")
def step_then_archived_once(context):
    assert len(context.ic_media_store.images) == 1, context.ic_media_store.images


@then("that post is classified once")
def step_then_classified_once(context):
    assert context.sdva_classifier.photos_classified == 1, context.sdva_classifier.photos_classified


@then("that post is extracted once")
def step_then_extracted_once(context):
    assert context.ic_openai.calls == 1, context.ic_openai.calls


@given("every returned reel is also returned as a post")
def step_given_reels_fully_overlap_posts(context):
    posts = [
        _post("ovlp1", "2026-08-01T10:00:00.000Z"),
        _post("ovlp2", "2026-08-03T10:00:00.000Z"),
    ]
    _program_posts(context, context.ic_handle, "posts", posts)
    _program_posts(context, context.ic_handle, "reels", [dict(p) for p in posts])


@then("the posts cursor advances to the newest post")
def step_then_posts_cursor_advances_to_newest(context):
    from datetime import datetime, timezone

    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["cursor_posts_at"] == datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc), row


@then("the reels cursor advances to the newest reel")
def step_then_reels_cursor_advances_to_newest(context):
    from datetime import datetime, timezone

    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["cursor_reels_at"] == datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc), row


@given("some reels were already returned by the posts stream")
def step_given_some_reels_already_seen(context):
    posts = [
        _post("ov1", "2026-08-01T09:00:00.000Z"),
        _post("ov2", "2026-08-02T09:00:00.000Z"),
        _post("ov3", "2026-08-03T09:00:00.000Z"),
    ]
    reels = [
        _post("ov1", "2026-08-01T09:00:00.000Z"),  # overlaps posts
        _post("ov2", "2026-08-02T09:00:00.000Z"),  # overlaps posts
        _post("ov4", "2026-08-04T09:00:00.000Z"),  # new to reels
    ]
    _program_posts(context, context.ic_handle, "posts", posts)
    _program_posts(context, context.ic_handle, "reels", reels)


@then("the run records how many reels results were new")
def step_then_records_reels_new(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["last_run_reels_new"] == 1, row


@then("the run records how many were already seen as posts")
def step_then_records_reels_already_seen(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    already_seen = row["last_run_reels_fetched"] - row["last_run_reels_new"]
    assert already_seen == 2, row


# ── The reels default ────────────────────────────────────────────────────
@given("a crawl target created without choosing whether to crawl reels")
def step_given_target_no_reels_choice(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "defaultreelstarget")


@then("only the posts stream is scraped")
def step_then_only_posts_stream_scraped(context):
    types = [c["results_type"] for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert types == ["posts"], types


@given("an existing crawl target with reels enabled")
def step_given_existing_target_reels_enabled(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "existingreelstarget", crawl_reels=True)


@then("both streams are scraped")
def step_then_both_streams_scraped(context):
    types = [c["results_type"] for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert types == ["posts", "reels"], types


# ── Attributing a shared handle ──────────────────────────────────────────
@given("a crawl target whose handle belongs to one venue")
def step_given_single_venue_handle(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "singlevenuehandle", crawl_reels=False)
    context.sdva_shortcode = "singlepost1"
    _program_posts(context, context.ic_handle, "posts", [
        _post(
            context.sdva_shortcode, "2026-08-05T20:00:00.000Z",
            caption="Forro hoje na casa! Ingressos abertos, sexta 22h",
        ),
    ])
    context.ic_openai.program(_extraction_json())


@then("every post is attributed to that venue")
def step_then_attributed_to_single_venue(context):
    row = _sole_event(context)
    assert row["venue_id"] == context.ic_venue_ids[-1], (row, context.ic_venue_ids)


@given("a crawl target whose handle belongs to two venues in different areas")
def step_given_two_venue_handle_areas(context):
    _setup_two_venue_handle(context)


@given("a crawl target whose handle belongs to two venues")
def step_given_two_venue_handle(context):
    _setup_two_venue_handle(context)


@given("a post whose caption names one of those areas")
def step_given_post_names_area(context):
    _replace_post(context, caption=_BRANCH_CAPTION)


@then("the event is attributed to the venue in that area")
def step_then_attributed_to_area(context):
    row = _sole_event(context)
    assert row["venue_id"] == context.sdva_venue_b_id, row


@then("it is not attributed to the other venue")
def step_then_not_attributed_to_other_venue(context):
    row = _sole_event(context)
    assert row["venue_id"] != context.sdva_venue_a_id, row


@given("a post whose caption names neither of them")
def step_given_post_names_neither(context):
    _replace_post(context, caption=_NEITHER_CAPTION)


@then("the event is attributed to no venue")
def step_then_attributed_to_no_venue(context):
    row = _sole_event(context)
    assert row["venue_id"] is None, row


@then("the event awaits a human decision")
def step_then_awaits_human_decision(context):
    row = _sole_event(context)
    awaiting_ids = {r["event_id"] for r in (context.ic_dao.list_events_awaiting_decision() or [])}
    assert row["event_id"] in awaiting_ids, (row, awaiting_ids)


@then("no post produces an event for more than one venue")
def step_then_no_post_multi_venue(context):
    rows = context.ic_dao.list_events_by_source(context.ic_handle, context.sdva_shortcode)
    venue_ids = {r["venue_id"] for r in rows if r.get("venue_id")}
    assert len(venue_ids) <= 1, rows
    assert len(rows) <= 1, rows


@then("each post's images are archived once, not once per venue")
def step_then_archived_once_not_per_venue(context):
    assert len(context.ic_media_store.images) == 1, context.ic_media_store.images


@given("a post whose caption weakly suggests one of them")
def step_given_post_weakly_suggests(context):
    _replace_post(context, caption=_WEAK_CAPTION)
