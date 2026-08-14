"""Behave steps for tests/bdd/enrichment/deep-review-corpus.feature.

Drives the REAL `DeepReviewCrawlService` over the REAL `VenueRepository` (RDS
system of record via `tests.rds_fake.InMemoryRdsVenueStore`, exactly the
harness environment.py already wires as `context.repository`/`context.
rds_store`) and the REAL `ReviewCrawlBudgetDao` over `context.fake_redis` — a
Redis counter is exactly the kind of thing worth exercising for real rather
than faking. The only fakes are at the true external boundary: the Apify
reviews client and the Apify ACCOUNT usage lookup, mirroring this suite's own
established pattern (see scheduled_incremental_instagram_crawl_steps.py's
module docstring).

Reads the projector (`context.redis_projection_service`) and the Redis-only
DAO (`context.redis_only_dao`) directly for the two projection scenarios,
same as projection_persistence_integrity_steps.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from behave import given, when, then  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.config import settings
from app.dao.review_crawl_budget_dao import ReviewCrawlBudgetDao
from app.models.venue import Venue
from app.models.vibe_attributes import VibeAttributes
from app.models.venue_review import VenueReview, VenueReviews, VenueReviewsDeep
from app.services.deep_review_crawl_service import DeepReviewCrawlService

_LAT, _LNG = -8.05, -34.88

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _override_setting(context, name, value):
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)


def _metric(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


def _place_id(vid: str) -> str:
    return f"place_{vid}"


def _venue(vid: str) -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=vid,
        venue_address=f"{vid} address", venue_lat=_LAT, venue_lng=_LNG,
        venue_type="BAR",
    )


def _seed_venue(context, vid: str, with_place_id: bool = True) -> None:
    context.repository.upsert_venue(_venue(vid))
    if with_place_id:
        context.repository.set_vibe_attributes(
            VibeAttributes(venue_id=vid, google_place_id=_place_id(vid))
        )


def _raw_item(review_id: str, author: str, text: str, published: datetime, rating: int = 5) -> dict:
    return {
        "reviewId": review_id,
        "name": author,
        "text": text,
        "publishedAtDate": published.isoformat(),
        "stars": rating,
        "reviewerLanguage": "pt",
    }


class _FakeReviewsApifyClient:
    """Programmed per place_id: a FIFO queue of (raw_items | Exception).
    Records every call's exact arguments — place_ids/max_reviews/since —
    matching this suite's "test the platform" discipline (assert on what was
    actually requested, not only on what came back)."""

    def __init__(self):
        self._queues: dict[str, list] = {}
        self.calls: list[dict] = []

    def program(self, place_id: str, items_or_exception) -> None:
        self._queues.setdefault(place_id, []).append(items_or_exception)

    async def fetch_reviews(self, place_ids, max_reviews, language="pt-BR", since=None):
        place_id = place_ids[0]
        self.calls.append({"place_id": place_id, "max_reviews": max_reviews, "since": since})
        queue = self._queues.get(place_id)
        if not queue:
            return []
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeAccountUsageClient:
    """Defaults to generous headroom so scenarios not about the account gate
    are unaffected; a scenario that needs to exercise the gate overrides
    `headroom_usd` (a number) or `fail` (simulate a lookup failure -> None,
    which the service MUST treat as zero headroom)."""

    def __init__(self):
        self.headroom_usd = 1000.0
        self.fail = False
        self.calls = 0

    async def get_headroom_usd(self):
        self.calls += 1
        if self.fail:
            return None
        return self.headroom_usd


def _service(context) -> DeepReviewCrawlService:
    if getattr(context, "deep_review_service", None) is None:
        context.fake_apify_reviews = _FakeReviewsApifyClient()
        context.fake_account_usage = _FakeAccountUsageClient()
        context.review_budget_dao = ReviewCrawlBudgetDao(context.fake_redis)
        context.now = datetime.now(timezone.utc)
        context.deep_review_service = DeepReviewCrawlService(
            repo=context.repository,
            apify_client=context.fake_apify_reviews,
            budget_dao=context.review_budget_dao,
            account_usage_client=context.fake_account_usage,
            now_fn=lambda: context.now,
        )
    return context.deep_review_service


# ── Background ────────────────────────────────────────────────────────────────
@given("the review window is {days:d} days")
def step_window_days(context, days):
    _override_setting(context, "reviews_deep_window_days", days)


@given("the per-venue review cap is {cap:d}")
def step_cap(context, cap):
    _override_setting(context, "reviews_deep_max_per_venue", cap)


@given("a monthly review budget is configured")
def step_budget_configured(context):
    _override_setting(context, "reviews_deep_monthly_review_budget", 30000)
    _override_setting(context, "reviews_deep_batch_size", 20)
    _override_setting(context, "reviews_deep_reserved_headroom_usd", 8.0)
    _service(context)


# ── Scenario: captures every review inside the window ───────────────────────
@given("a venue with {n:d} reviews published inside the window")
def step_venue_with_n_in_window(context, n):
    vid = "venue-a"
    context.venue_id = vid
    _seed_venue(context, vid)
    window = settings.reviews_deep_window_days
    span = max(window - 1, 1)
    items = [
        _raw_item(f"{vid}-r{i}", f"Author{i}", f"Great place #{i}", context.now - timedelta(days=(i % span)))
        for i in range(n)
    ]
    context.fake_apify_reviews.program(_place_id(vid), items)


@when("the operator runs a deep review crawl for that venue")
def step_run_for_that_venue(context):
    context.run_result = _run(_service(context).run(venue_ids=[context.venue_id]))


@then("all {n:d} reviews are stored for that venue")
def step_all_n_stored(context, n):
    deep = context.repository.get_venue_reviews_deep(context.venue_id)
    assert deep is not None, "expected a stored deep-review record"
    assert len(deep.reviews) == n, f"expected {n} reviews, got {len(deep.reviews)}"


@then("the stored record is not marked truncated")
def step_not_truncated(context):
    deep = context.repository.get_venue_reviews_deep(context.venue_id)
    assert deep.truncated is False


# ── Scenario: reviews older than the window are not stored ─────────────────
@given("a venue with {inside:d} reviews inside the window and {outside:d} reviews older than it")
def step_venue_mixed_window(context, inside, outside):
    vid = "venue-b"
    context.venue_id = vid
    _seed_venue(context, vid)
    window = settings.reviews_deep_window_days
    items = [
        _raw_item(f"{vid}-in{i}", f"In{i}", f"text {i}", context.now - timedelta(days=(i % max(window - 1, 1))))
        for i in range(inside)
    ] + [
        _raw_item(f"{vid}-out{i}", f"Out{i}", f"old text {i}", context.now - timedelta(days=window + 10 + i))
        for i in range(outside)
    ]
    context.fake_apify_reviews.program(_place_id(vid), items)


@then("only the {n:d} in-window reviews are stored")
def step_only_n_in_window_stored(context, n):
    deep = context.repository.get_venue_reviews_deep(context.venue_id)
    assert deep is not None
    assert len(deep.reviews) == n, f"expected {n}, got {len(deep.reviews)}"


# ── Scenario: hitting the per-venue cap is reported (reuses the Given above:
# "a venue with {n:d} reviews published inside the window" — same pattern,
# just a bigger n, e.g. 500) ─────────────────────────────────────────────────
@then("{n:d} reviews are stored for that venue")
def step_n_stored(context, n):
    deep = context.repository.get_venue_reviews_deep(context.venue_id)
    assert deep is not None
    assert len(deep.reviews) == n, f"expected {n}, got {len(deep.reviews)}"


@then("the stored record is marked truncated")
def step_marked_truncated(context):
    deep = context.repository.get_venue_reviews_deep(context.venue_id)
    assert deep.truncated is True


@then("the run summary names that venue as truncated")
def step_summary_names_truncated(context):
    assert context.venue_id in context.run_result["truncated_venues"], context.run_result


# ── Scenario: re-run fetches only reviews newer than the newest stored ─────
@given("a venue whose deep reviews were captured yesterday")
def step_venue_captured_yesterday(context):
    vid = "venue-c"
    context.venue_id = vid
    _seed_venue(context, vid)
    existing = [
        VenueReview(
            author_name=f"Existing{d}", rating=5, text=f"existing text {d}",
            relative_time="", publish_time=(context.now - timedelta(days=d)).isoformat(),
            review_id=f"{vid}-existing-{d}", source="apify_gmaps",
        )
        for d in (10, 9, 8, 7, 6)
    ]
    context.existing_reviews = existing
    publish_times = [r.publish_time for r in existing]
    deep = VenueReviewsDeep(
        venue_id=vid, reviews=existing, window_days=settings.reviews_deep_window_days,
        fetched_at=(context.now - timedelta(days=1)).isoformat(),
        oldest_publish_time=min(publish_times), newest_publish_time=max(publish_times),
        truncated=False,
    )
    context.repository.set_venue_reviews_deep(deep)


@given("{n:d} new reviews have been published since")
def step_n_new_since(context, n):
    vid = context.venue_id
    items = [
        _raw_item(f"{vid}-new{i}", f"New{i}", f"new text {i}", context.now - timedelta(days=1) + timedelta(hours=i))
        for i in range(n)
    ]
    context.fake_apify_reviews.program(_place_id(vid), items)
    context.new_reviews_count = n


@when("the operator runs a deep review crawl for that venue again")
def step_run_again(context):
    context.run_result = _run(_service(context).run(venue_ids=[context.venue_id]))


@then("only {n:d} reviews are billed")
def step_only_n_billed(context, n):
    assert context.run_result["reviews_billed_total"] == n, context.run_result
    # Test the platform, not our own callback: assert the incremental cursor
    # was actually PASSED to the client, not merely that the count matched by
    # coincidence.
    last_call = context.fake_apify_reviews.calls[-1]
    existing_newest = max(r.publish_time for r in context.existing_reviews)
    assert last_call["since"] == existing_newest, last_call


@then("the stored review count increases by {n:d}")
def step_count_increases_by_n(context, n):
    deep = context.repository.get_venue_reviews_deep(context.venue_id)
    assert len(deep.reviews) == len(context.existing_reviews) + n, len(deep.reviews)


# ── Scenario: a re-run never duplicates a review already stored ────────────
@given("the actor returns reviews that were already captured")
def step_actor_returns_duplicates(context):
    vid = context.venue_id
    dup_reviews = context.existing_reviews[:2]
    items = [
        _raw_item(
            r.review_id, r.author_name, r.text,
            datetime.fromisoformat(r.publish_time),
        )
        for r in dup_reviews
    ]
    context.fake_apify_reviews.program(_place_id(vid), items)
    context.duplicate_metric_before = _metric("deep_reviews_fetched_total", {"outcome": "duplicate"})


@then("the stored review count is unchanged")
def step_count_unchanged(context):
    deep = context.repository.get_venue_reviews_deep(context.venue_id)
    assert len(deep.reviews) == len(context.existing_reviews), len(deep.reviews)


@then("the duplicates are counted as duplicate outcomes")
def step_duplicates_counted(context):
    after = _metric("deep_reviews_fetched_total", {"outcome": "duplicate"})
    assert after > context.duplicate_metric_before, (after, context.duplicate_metric_before)


# ── Scenario: reviews that age out of the window are retained ──────────────
@given("a venue whose stored reviews include one now older than the window")
def step_venue_with_aged_review(context):
    vid = "venue-d"
    context.venue_id = vid
    _seed_venue(context, vid)
    window = settings.reviews_deep_window_days
    aged = VenueReview(
        author_name="Old Author", rating=4, text="an old review",
        relative_time="", publish_time=(context.now - timedelta(days=window + 30)).isoformat(),
        review_id=f"{vid}-aged", source="apify_gmaps",
    )
    fresh = VenueReview(
        author_name="Fresh Author", rating=5, text="a fresh review",
        relative_time="", publish_time=(context.now - timedelta(days=5)).isoformat(),
        review_id=f"{vid}-fresh", source="apify_gmaps",
    )
    context.aged_review_id = aged.review_id
    deep = VenueReviewsDeep(
        venue_id=vid, reviews=[aged, fresh], window_days=window,
        fetched_at=(context.now - timedelta(days=1)).isoformat(),
        oldest_publish_time=aged.publish_time, newest_publish_time=fresh.publish_time,
        truncated=False,
    )
    context.repository.set_venue_reviews_deep(deep)
    # No new reviews this run — isolates the retention behavior.
    context.fake_apify_reviews.program(_place_id(vid), [])


@then("the aged review is still stored")
def step_aged_review_still_stored(context):
    deep = context.repository.get_venue_reviews_deep(context.venue_id)
    ids = [r.review_id for r in deep.reviews]
    assert context.aged_review_id in ids, ids


# ── Scenario: a venue without a Google place id is skipped ─────────────────
@given("a selected venue with no google_place_id")
def step_venue_without_place_id(context):
    vid = "venue-e"
    context.venue_id = vid
    _seed_venue(context, vid, with_place_id=False)


@when("the operator runs a deep review crawl")
def step_run_crawl_generic(context):
    venue_ids = getattr(context, "venue_ids", None) or [context.venue_id]
    filter_spec = getattr(context, "filter_spec", None)
    context.run_result = _run(_service(context).run(venue_ids=venue_ids, filter_spec=filter_spec))


@then("that venue is reported as skipped for a missing place id")
def step_reported_skipped(context):
    assert context.venue_id in context.run_result["skipped_no_place_id"], context.run_result


@then("no paid call is made for that venue")
def step_no_paid_call(context):
    assert len(context.fake_apify_reviews.calls) == 0, context.fake_apify_reviews.calls


# ── Scenario: the estimate spends nothing ───────────────────────────────────
@given("the operator selects {n:d} venues")
def step_operator_selects_n_venues(context, n):
    ids = [f"venue-est-{i}" for i in range(n)]
    for vid in ids:
        _seed_venue(context, vid)
    context.venue_ids = ids


@when("the operator requests an estimate for that selection")
def step_request_estimate(context):
    context.estimate_result = _service(context).estimate(venue_ids=context.venue_ids)


@then("the estimate reports the worst-case review count and cost")
def step_estimate_reports_bound(context):
    n = len(context.venue_ids)
    cap = settings.reviews_deep_max_per_venue
    expected_reviews = n * cap
    expected_cost = round(expected_reviews * settings.apify_review_cost_usd, 4)
    assert context.estimate_result["selected_venue_count"] == n
    assert context.estimate_result["worst_case_review_count"] == expected_reviews
    assert context.estimate_result["worst_case_cost_usd"] == expected_cost


@then("the estimate states that the count is an upper bound")
def step_estimate_upper_bound(context):
    assert context.estimate_result["is_upper_bound"] is True


@then("no actor run is started")
def step_no_actor_run_started(context):
    assert len(context.fake_apify_reviews.calls) == 0


# ── Scenario: the local budget is checked before the paid call ─────────────
@given("the monthly review budget is already exhausted")
def step_budget_already_exhausted(context):
    vid = "venue-f"
    context.venue_id = vid
    _seed_venue(context, vid)
    year_month = ReviewCrawlBudgetDao.current_year_month_utc(context.now)
    context.review_budget_dao.increment_month(year_month, settings.reviews_deep_monthly_review_budget)


@then("the run reports that the budget stopped it")
def step_run_reports_budget_stopped(context):
    assert context.run_result["budget_stopped"] is True, context.run_result


# ── Scenario: a run that would cross the budget stops at the boundary ──────
@given("the remaining monthly budget covers only the first two of five venues")
def step_budget_covers_first_two_of_five(context):
    vids = [f"venue-g{i}" for i in range(5)]
    context.venue_ids = vids
    reviews_per_venue = 10
    for vid in vids:
        _seed_venue(context, vid)
        items = [
            _raw_item(f"{vid}-r{i}", f"A{i}", f"text{i}", context.now - timedelta(days=i))
            for i in range(reviews_per_venue)
        ]
        context.fake_apify_reviews.program(_place_id(vid), items)
    _override_setting(context, "reviews_deep_monthly_review_budget", reviews_per_venue * 2)
    context.reviews_per_venue = reviews_per_venue


@when("the operator runs a deep review crawl for the five venues")
def step_run_five_venues(context):
    context.run_result = _run(_service(context).run(venue_ids=context.venue_ids))


@then("the reviews fetched for the first two venues are stored")
def step_first_two_stored(context):
    for vid in context.venue_ids[:2]:
        deep = context.repository.get_venue_reviews_deep(vid)
        assert deep is not None and len(deep.reviews) == context.reviews_per_venue, vid


@then("the run summary names the three venues it did not reach")
def step_summary_names_three_not_reached(context):
    assert set(context.run_result["not_reached_venues"]) == set(context.venue_ids[2:]), context.run_result


@then("the run outcome is partial")
def step_outcome_partial(context):
    assert context.run_result["outcome"] == "partial", context.run_result


# ── Scenario: one venue's failure does not abort the run ───────────────────
@given("the actor fails for the second of three selected venues")
def step_actor_fails_for_second(context):
    vids = ["venue-h0", "venue-h1", "venue-h2"]
    context.venue_ids = vids
    for i, vid in enumerate(vids):
        _seed_venue(context, vid)
        if i == 1:
            context.fake_apify_reviews.program(_place_id(vid), RuntimeError("actor crashed"))
        else:
            context.fake_apify_reviews.program(
                _place_id(vid), [_raw_item(f"{vid}-r0", "A", "text", context.now - timedelta(days=1))]
            )
    context.failed_venue_id = vids[1]


@then("the first and third venues are stored")
def step_first_and_third_stored(context):
    for vid in (context.venue_ids[0], context.venue_ids[2]):
        deep = context.repository.get_venue_reviews_deep(vid)
        assert deep is not None and len(deep.reviews) == 1, vid


@then("the run summary names the failed venue")
def step_summary_names_failed_venue(context):
    assert context.failed_venue_id in context.run_result["failed_venues"], context.run_result


# ── Scenario: the existing Google Places review path is untouched ──────────
@given("a venue with five reviews from Google Places enrichment")
def step_venue_with_places_reviews(context):
    vid = "venue-i"
    context.venue_id = vid
    _seed_venue(context, vid)
    places_reviews = VenueReviews(
        venue_id=vid,
        reviews=[
            VenueReview(
                author_name=f"Places{i}", rating=5, text=f"places review {i}",
                relative_time=f"{i} months ago", publish_time=(context.now - timedelta(days=i)).isoformat(),
            )
            for i in range(5)
        ],
    )
    context.repository.set_venue_reviews(places_reviews)
    # Populate the Redis serving projection (venue_reviews_v1) BEFORE the deep
    # crawl, so "unchanged" is a real byte-for-byte comparison, not a
    # both-sides-empty vacuous pass.
    context.redis_projection_service.rebuild_redis_from_rds()
    context.places_reviews_rds_before = context.rds_store.get_enrichment("google_places.reviews", vid)
    context.places_reviews_redis_before = context.fake_redis.get(f"venue_reviews_v1:{vid}")
    assert context.places_reviews_redis_before is not None, "projection setup failed"
    # Give the venue some deep reviews to fetch, so the deep crawl actually
    # does something (a no-op run would prove nothing about isolation).
    context.fake_apify_reviews.program(
        _place_id(vid), [_raw_item(f"{vid}-deep0", "Deep Author", "deep text", context.now - timedelta(days=2))]
    )


@then("the google_places reviews payload is unchanged")
def step_google_places_payload_unchanged(context):
    after = context.rds_store.get_enrichment("google_places.reviews", context.venue_id)
    assert after["payload"] == context.places_reviews_rds_before["payload"], (
        after, context.places_reviews_rds_before,
    )


@then("the venue_reviews_v1 projection for that venue is unchanged")
def step_venue_reviews_v1_unchanged(context):
    # Re-run the projector too — the deep crawl must not have altered the RDS
    # google_places.reviews row it reads from, so a fresh projection cycle
    # must reproduce the exact same Redis payload.
    context.redis_projection_service.rebuild_redis_from_rds()
    after = context.fake_redis.get(f"venue_reviews_v1:{context.venue_id}")
    assert after == context.places_reviews_redis_before, (after, context.places_reviews_redis_before)


# ── Scenario: the projection carries a bounded slice ────────────────────────
@given("a venue with {n:d} stored deep reviews")
def step_venue_with_n_stored_deep_reviews(context, n):
    vid = "venue-j"
    context.venue_id = vid
    _seed_venue(context, vid)
    reviews = [
        VenueReview(
            author_name=f"A{i}", rating=5, text=f"text {i}", relative_time="",
            publish_time=(context.now - timedelta(hours=i)).isoformat(),
            review_id=f"{vid}-r{i}", source="apify_gmaps",
        )
        for i in range(n)
    ]
    context.stored_deep_reviews = reviews
    publish_times = [r.publish_time for r in reviews]
    deep = VenueReviewsDeep(
        venue_id=vid, reviews=reviews, window_days=settings.reviews_deep_window_days,
        fetched_at=context.now.isoformat(),
        oldest_publish_time=min(publish_times), newest_publish_time=max(publish_times),
        truncated=False,
    )
    context.repository.set_venue_reviews_deep(deep)


@given("the projection slice is {n:d}")
def step_projection_slice_is_n(context, n):
    _override_setting(context, "reviews_deep_projection_max", n)


@then("the deep review projection for that venue holds {n:d} reviews")
def step_projection_holds_n(context, n):
    projected = context.redis_only_dao.get_venue_reviews_deep(context.venue_id)
    assert projected is not None
    assert len(projected.reviews) == n, len(projected.reviews)


@then("they are the {n:d} newest")
def step_projection_is_newest(context, n):
    projected = context.redis_only_dao.get_venue_reviews_deep(context.venue_id)
    expected_ids = {
        r.review_id
        for r in sorted(context.stored_deep_reviews, key=lambda r: r.publish_time, reverse=True)[:n]
    }
    actual_ids = {r.review_id for r in projected.reviews}
    assert actual_ids == expected_ids, (actual_ids, expected_ids)


@then("the RDS record still holds {n:d} reviews")
def step_rds_still_holds_n(context, n):
    full = context.repository.get_venue_reviews_deep(context.venue_id)
    assert len(full.reviews) == n, len(full.reviews)


# ── Scenario: a soft-deleted deep review row is removed from the projection ─
@given("a venue whose deep review row is soft-deleted")
def step_venue_with_soft_deleted_row(context):
    vid = "venue-k"
    context.venue_id = vid
    _seed_venue(context, vid)
    review = VenueReview(
        author_name="A", rating=5, text="text", relative_time="",
        publish_time=context.now.isoformat(), review_id=f"{vid}-r0", source="apify_gmaps",
    )
    deep = VenueReviewsDeep(
        venue_id=vid, reviews=[review], window_days=settings.reviews_deep_window_days,
        fetched_at=context.now.isoformat(),
        oldest_publish_time=review.publish_time, newest_publish_time=review.publish_time,
        truncated=False,
    )
    context.repository.set_venue_reviews_deep(deep)
    # Project it first so there is a real key to observe disappearing.
    context.redis_projection_service.rebuild_redis_from_rds()
    assert context.redis_only_dao.get_venue_reviews_deep(vid) is not None, "projection setup failed"
    context.repository.delete_venue_reviews_deep(vid)


@then("the deep review key for that venue is absent from Redis")
def step_deep_review_key_absent(context):
    assert context.redis_only_dao.get_venue_reviews_deep(context.venue_id) is None


# ── Scenario: a filter may narrow but never widen an explicit selection ────
@given("the operator selects three venue ids")
def step_operator_selects_three_venue_ids(context):
    ids = ["venue-l0", "venue-l1", "venue-l2"]
    for vid in ids:
        _seed_venue(context, vid)
    context.venue_ids = ids


@given("a filter that matches fifty venues")
def step_filter_matches_fifty(context):
    # The three explicitly-selected ids are WITHIN the 50 the filter matches
    # (all share venue_type="BAR", set by _seed_venue) — this is the case
    # that actually distinguishes correct "narrow, never widen" behavior
    # from a bug that unions the filter's matches into the run.
    for i in range(47):
        _seed_venue(context, f"venue-l-extra-{i}")
    context.filter_spec = {"venue_type": "BAR"}


@then("at most those three venues are crawled")
def step_at_most_three_crawled(context):
    crawled = set(context.run_result["stored_venues"]) | set(context.run_result["failed_venues"])
    assert crawled <= set(context.venue_ids), crawled
    assert len(crawled) <= 3, crawled
