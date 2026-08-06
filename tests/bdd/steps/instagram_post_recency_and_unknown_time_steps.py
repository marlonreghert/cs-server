"""Behave steps for
tests/bdd/enrichment/instagram-post-recency-and-unknown-time.feature.

Two unrelated defects share one feature file (see the plan,
plans/260806_instagram-post-recency-and-unknown-time.md), so this file drives
two different real components, reusing each domain's existing fixtures rather
than duplicating them:

* Post-ordering scenarios drive the REAL VenuePhotoArchiveService /
  `instagram_posts` archive source, reusing `instagram_media_archive_steps.py`'s
  build helper and fakes (`_build_instagram`, `_FakeApifyInstagram`) — the
  Background step `a venue with a confirmed Instagram handle` is THAT file's
  own step, not redefined here (redefining identical step text would be an
  AmbiguousStep collision).
* Unread-time scenarios drive the REAL EventExtractionService, reusing
  `instagram_event_extraction_steps.py`'s context helpers (`_add_post`,
  `_extraction_json`, `_stored_event`) and its venue-seeding step, called
  directly as a plain function. Its `event extraction runs` step is reused
  unmodified for the same reason.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then  # type: ignore[import-untyped]

from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _extraction_json,
    _stored_event,
    step_given_an_event_candidate_venue_with_an_instagram_handle as _seed_ee_venue,
)
from tests.bdd.steps.instagram_media_archive_steps import (
    _build_instagram,
    _manifest_for,
    _single_post,
)

# ── shared setup helpers ──────────────────────────────────────────────────────
def _ensure_instagram(context) -> None:
    """Build the real archive service + fakes once per scenario. The feature's
    Background only seeds the venue (instagram_media_archive_steps.py's own
    step); it does not build the service, because this feature's second half
    (event extraction) needs no archive service at all."""
    if getattr(context, "service", None) is None:
        _build_instagram(context)


def _ensure_ee_ready(context) -> None:
    """Build the real EventExtractionService + a venue/handle once per
    scenario, calling the sibling file's Given step as a plain function — a
    behave step decorator does not wrap its function, so this runs exactly
    what `Given an event-candidate venue with an Instagram handle` runs."""
    if getattr(context, "ee_venue_id", None) is None:
        _seed_ee_venue(context)


def _dated_single_post(shortcode: str, date_str: str) -> dict:
    return _single_post(shortcode, timestamp=f"{date_str}T20:00:00.000Z")


def _post_no_timestamp(shortcode: str) -> dict:
    post = _single_post(shortcode)
    post["timestamp"] = None
    return post


def _dated_carousel_post(shortcode: str, n_children: int, date_str: str) -> dict:
    urls = [f"https://instagram.cdn.example/{shortcode}_{i}.jpg" for i in range(n_children)]
    return {
        "caption": "", "likes_count": 0, "comments_count": 0,
        "timestamp": f"{date_str}T20:00:00.000Z", "post_type": "carousel",
        "shortcode": shortcode, "permalink": f"https://instagram.com/p/{shortcode}/",
        "image_urls": urls,
    }


def _archived_dates(context) -> set:
    manifest = _manifest_for(context, context.venue_id)
    photos = (manifest or {}).get("photos") or []
    return {p["uploaded_at"][:10] for p in photos if p.get("uploaded_at")}


# ── Given: post ordering (Defect 1) ──────────────────────────────────────────
@given('the scraper returns posts dated "{dates}" in that order')
def step_scraper_returns_posts_dated_in_order(context, dates):
    _ensure_instagram(context)
    date_list = [d.strip() for d in dates.replace(" and ", ", ").split(",") if d.strip()]
    posts = [_dated_single_post(f"sc_{d}", d) for d in date_list]
    context.apify_instagram.posts_by_handle[context.handle] = posts


@given("each post carries one image")
def step_each_post_carries_one_image(context):
    pass  # already true: every post built above carries exactly one image url


@given("the scraper returns 3 carousel posts of 5 images each, out of date order")
def step_scraper_returns_three_carousels_out_of_order(context):
    _ensure_instagram(context)
    # Deliberately OLDEST first: a cap that trusted array order (the pre-fix
    # bug) would spend the whole cap on car_old + car_mid and never reach the
    # newest post at all.
    posts = [
        _dated_carousel_post("car_old", 5, "2026-08-01"),
        _dated_carousel_post("car_mid", 5, "2026-08-03"),
        _dated_carousel_post("car_new", 5, "2026-08-05"),
    ]
    context.apify_instagram.posts_by_handle[context.handle] = posts
    context.newest_two_shortcodes = {"car_new", "car_mid"}


@given("the scraper returns a post with no timestamp and a post dated 2026-08-06")
def step_scraper_returns_undated_and_dated_post(context):
    _ensure_instagram(context)
    # The undated post is listed FIRST: if an unparseable timestamp were not
    # sorted last, it would consume the cap ahead of the dated post.
    undated = _post_no_timestamp("sc_undated")
    dated = _dated_single_post("sc_20260806", "2026-08-06")
    context.apify_instagram.posts_by_handle[context.handle] = [undated, dated]


# ── Then: post ordering (Defect 1) ───────────────────────────────────────────
@then("the images archived come from the {d1} and {d2} posts")
def step_images_archived_come_from_two_dates(context, d1, d2):
    dates = _archived_dates(context)
    assert d1 in dates, f"expected {d1} among archived dates {dates}"
    assert d2 in dates, f"expected {d2} among archived dates {dates}"


@then("no image from the {date} post is archived")
def step_no_image_from_date_archived(context, date):
    dates = _archived_dates(context)
    assert date not in dates, f"{date} must not be archived; got {dates}"


@then("{n:d} images are archived")
def step_n_images_archived(context, n):
    manifest = _manifest_for(context, context.venue_id)
    photos = (manifest or {}).get("photos") or []
    assert len(photos) == n, f"expected {n} archived images, got {len(photos)}: {photos}"


@then("every archived image comes from the two newest posts")
def step_every_archived_image_from_two_newest(context):
    manifest = _manifest_for(context, context.venue_id)
    photos = (manifest or {}).get("photos") or []
    shortcodes = {p.get("shortcode") for p in photos}
    assert shortcodes, "no images were archived"
    assert shortcodes <= context.newest_two_shortcodes, (
        f"images came from {shortcodes}, expected only {context.newest_two_shortcodes}"
    )


@then("the image archived comes from the {date} post")
def step_the_image_archived_comes_from_date(context, date):
    dates = _archived_dates(context)
    assert dates == {date}, f"expected only {date}, got {dates}"


@then("both images are archived")
def step_both_images_archived(context):
    manifest = _manifest_for(context, context.venue_id)
    photos = (manifest or {}).get("photos") or []
    assert len(photos) == 2, f"expected 2 archived images, got {len(photos)}: {photos}"


# ── Given: unread event time (Defect 2) ──────────────────────────────────────
@given("a qualifying post whose flyer reports that it names a time")
def step_qualifying_post_flyer_names_time(context):
    _ensure_ee_ready(context)
    _add_post(context, "flyer_names_time_yes", flyer_names_time="yes")


@given("a qualifying post whose flyer reports that it names no time")
def step_qualifying_post_flyer_names_no_time(context):
    _ensure_ee_ready(context)
    _add_post(context, "flyer_names_time_no", flyer_names_time="no")


@given("a qualifying post that has no flyer attributes")
def step_qualifying_post_no_flyer_attributes(context):
    _ensure_ee_ready(context)
    # No flyer photo classified at all — qualifies via the caption marker
    # instead, same technique the sibling steps file uses for its
    # "qualifying_i" fixtures.
    _add_post(context, "no_flyer_attrs", flyer_photo_key=None, flyer_confidence=None)


@given("the extractor returns no time")
def step_extractor_returns_no_time(context):
    context.ee_openai.program(_extraction_json(date_text="hoje", time_text=None))


@given('a qualifying post whose flyer states the event begins at "00h"')
def step_qualifying_post_states_00h(context):
    _ensure_ee_ready(context)
    _add_post(context, "flyer_states_00h", flyer_names_time="yes")
    context.ee_openai.program(_extraction_json(date_text="hoje", time_text="00h"))


@given("a qualifying post whose flyer states a date and a time")
def step_qualifying_post_states_date_and_time(context):
    _ensure_ee_ready(context)
    ts = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    _add_post(context, "flyer_full_date_time", timestamp=ts, flyer_names_time="yes")
    context.ee_openai.program(_extraction_json(date_text="20/08", time_text="22h"))


# ── Then: unread event time (Defect 2) ───────────────────────────────────────
@then('the event is queued for review with the reason "{reason}"')
def step_event_queued_with_reason(context, reason):
    row = _stored_event(context)
    review_reason = row.get("review_reason") or ""
    assert reason in review_reason, f"expected {reason!r} in {review_reason!r}"


@then('the outcome "{outcome}" is counted')
def step_outcome_is_counted(context, outcome):
    assert context.ee_result is not None
    assert context.ee_result["outcomes"].get(outcome, 0) >= 1, context.ee_result["outcomes"]


@then("the event starts at midnight on its resolved date")
def step_event_starts_at_midnight(context):
    row = _stored_event(context)
    starts_at = row.get("starts_at")
    assert starts_at is not None, row
    assert starts_at.hour == 0 and starts_at.minute == 0, starts_at


@then("the event time is marked unknown")
def step_event_time_marked_unknown(context):
    row = _stored_event(context)
    raw = row.get("raw_extraction") or {}
    assert raw.get("time_known") is False, raw


@then("the event time is marked known")
def step_event_time_marked_known(context):
    row = _stored_event(context)
    raw = row.get("raw_extraction") or {}
    assert raw.get("time_known") is True, raw


@then("the event is not queued for an unread time")
def step_event_not_queued_for_unread_time(context):
    row = _stored_event(context)
    review_reason = row.get("review_reason") or ""
    assert "unread_time" not in review_reason, review_reason


@then("the event carries that date and time")
def step_event_carries_that_date_and_time(context):
    row = _stored_event(context)
    starts_at = row.get("starts_at")
    assert starts_at is not None
    assert starts_at.date().isoformat() == "2026-08-20", starts_at
    assert starts_at.hour == 22, starts_at


@then("the event is not queued for review")
def step_event_not_queued_for_review(context):
    row = _stored_event(context)
    assert not row.get("review_reason"), row.get("review_reason")
