"""Unit tests for app/services/instagram_crawl_service.py.

Covers the plan's own unit test plan (plans/260809_scheduled-incremental-
instagram-crawl.md):
  - the exact bound string sent to the actor, for every case
  - cursor selection is the max timestamp, not run-clock, out-of-order safe
  - the cursor is UTC while the cron trigger is Recife-local — a case that
    FAILS if the two are swapped
  - overlap arithmetic at 3h and 6h
  - handle-keyed dedup: two venue rows, one handle, one Apify call
  - the budget gate refuses BEFORE the client is called (call count, not
    return value)
  - crontab validation rejects a malformed string at write time
  - reels and posts cursors move independently

BDD (tests/bdd/enrichment/scheduled-incremental-instagram-crawl.feature)
covers the externally observable behavior end to end; these tests pin the
lower-level functions and edge cases directly, per CLAUDE.md's "Do not
duplicate BDD assertions in pytest unless the unit test protects a lower
level edge case or failure mode."
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.api.apify_instagram_client import ApifyCreditExhaustedError, FetchPostsResult
from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services import job_lock
from app.services.instagram_crawl_service import (
    OUTCOME_SKIPPED_DISABLED,
    OUTCOME_SKIPPED_SEEDED,
    STREAM_POSTS,
    STREAM_REELS,
    CrawlServiceConfig,
    InstagramCrawlChainer,
    InvalidCrawlTargetConfig,
    ScheduledInstagramCrawlService,
    _newest_timestamp,
    _split_kept_and_dropped,
    build_cron_trigger,
    compute_bound,
    dedupe_posts_by_shortcode,
    group_venue_ids_by_handle,
    lock_name_for,
    posts_never_seeded,
    reels_already_seeded,
    reels_skip_reason,
    resolve_results_limit,
    validate_crontab,
)
from tests.rds_fake import InMemoryRdsVenueStore


def setup_function(_):
    """`job_lock` is a module-level registry (app/services/job_lock.py) —
    it persists across tests otherwise, exactly like tests/test_job_lock.py
    already has to guard against for the SAME module."""
    job_lock._running.clear()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


NOW = datetime(2026, 8, 8, 0, 0, 0, tzinfo=timezone.utc)


# ── compute_bound: exact string sent to the actor ───────────────────────────
class TestComputeBound:
    def test_no_cursor_sends_the_configured_lookback_unparsed(self):
        bound = compute_bound(None, overlap_hours=6.0, lookback_text="3 months", now=NOW)
        assert bound.actor_value == "3 months"

    def test_no_cursor_sends_a_targets_own_lookback_when_configured(self):
        bound = compute_bound(None, overlap_hours=6.0, lookback_text="45 days", now=NOW)
        assert bound.actor_value == "45 days"

    def test_a_cursor_sends_cursor_minus_overlap_as_utc_iso(self):
        cursor = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
        bound = compute_bound(cursor, overlap_hours=6.0, lookback_text="3 months", now=NOW)
        assert bound.actor_value == "2026-08-07T06:00:00Z"

    def test_overlap_arithmetic_at_3_hours(self):
        cursor = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
        bound = compute_bound(cursor, overlap_hours=3.0, lookback_text="3 months", now=NOW)
        assert bound.actor_value == "2026-08-07T09:00:00Z"

    def test_overlap_arithmetic_at_6_hours(self):
        cursor = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
        bound = compute_bound(cursor, overlap_hours=6.0, lookback_text="3 months", now=NOW)
        assert bound.actor_value == "2026-08-07T06:00:00Z"

    def test_cursor_bound_never_uses_the_run_wall_clock(self):
        # `now` is far ahead of the cursor; the bound must track the CURSOR,
        # never drift to reflect when the run happened.
        cursor = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        later_now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
        bound = compute_bound(cursor, overlap_hours=6.0, lookback_text="3 months", now=later_now)
        assert bound.actor_value == "2025-12-31T18:00:00Z"


# ── cursor selection: max timestamp, order-independent ──────────────────────
class TestNewestTimestamp:
    def test_picks_the_max_not_the_last_element(self):
        posts = [
            {"timestamp": "2026-08-01T10:00:00.000Z"},
            {"timestamp": "2026-08-05T10:00:00.000Z"},  # newest, in the middle
            {"timestamp": "2026-08-03T10:00:00.000Z"},
        ]
        assert _newest_timestamp(posts) == datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)

    def test_an_unparseable_timestamp_is_ignored_not_fatal(self):
        posts = [{"timestamp": "not-a-date"}, {"timestamp": "2026-08-01T10:00:00.000Z"}]
        assert _newest_timestamp(posts) == datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)

    def test_empty_list_yields_none(self):
        assert _newest_timestamp([]) is None


# ── §G: pinned-post drop, never moves the cursor ─────────────────────────────
class TestSplitKeptAndDropped:
    def test_a_pinned_post_older_than_the_cutoff_is_dropped_and_counted(self):
        cutoff = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        posts = [
            {"shortcode": "fresh", "timestamp": "2026-08-05T10:00:00.000Z", "is_pinned": False},
            {"shortcode": "oldpin", "timestamp": "2023-09-20T14:07:30.000Z", "is_pinned": True},
        ]
        kept, dropped = _split_kept_and_dropped(posts, cutoff)
        assert [p["shortcode"] for p in kept] == ["fresh"]
        assert dropped == 1

    def test_a_non_pinned_old_post_is_kept_not_dropped(self):
        # Only a PINNED post can be older than the bound at all (the actor's
        # own filter already excludes a non-pinned one) — this must never be
        # treated as a reason to drop.
        cutoff = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        posts = [{"shortcode": "old", "timestamp": "2023-01-01T00:00:00.000Z", "is_pinned": False}]
        kept, dropped = _split_kept_and_dropped(posts, cutoff)
        assert len(kept) == 1 and dropped == 0

    def test_no_cutoff_keeps_everything(self):
        posts = [{"shortcode": "a", "timestamp": "2020-01-01T00:00:00.000Z", "is_pinned": True}]
        kept, dropped = _split_kept_and_dropped(posts, None)
        assert kept == posts and dropped == 0


# ── crontab validation at write time ─────────────────────────────────────────
class TestValidateCrontab:
    def test_a_valid_crontab_does_not_raise(self):
        validate_crontab("0 22 * * 5,6")

    def test_a_malformed_crontab_raises_at_write_time(self):
        with pytest.raises(InvalidCrawlTargetConfig):
            validate_crontab("not a crontab")


# ── day-of-week: build_cron_trigger means STANDARD Unix cron, not
# APScheduler-native ─────────────────────────────────────────────────────────
# `CronTrigger.from_crontab` passes a day-of-week digit straight through to
# APScheduler's OWN 0=Monday..6=Sunday field with NO translation, despite its
# docstring claiming "a standard crontab expression" (standard cron is
# 0/7=Sunday..6=Saturday). Verified independently, live, against APScheduler
# 3.10.4 in America/Recife: `CronTrigger.from_crontab("0 22 * * 5", ...)`
# fires SATURDAY. These tests pin the weekday `build_cron_trigger` actually
# fires on — not just that a trigger object was built — and would FAIL
# against the unfixed `CronTrigger.from_crontab` path.
class TestBuildCronTriggerWeekday:
    # Monday NOON UTC = Monday 09:00 in America/Recife (UTC-3) — squarely
    # inside Monday in BOTH frames, so "the next Friday/Saturday/Sunday
    # 22:00" is unambiguous. (A midnight-UTC anchor converts to Sunday
    # 21:00 in Recife and silently matches the SAME evening — a real trap
    # this test tripped over while being written.)
    AFTER_MONDAY = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

    def _next_weekday(self, cron: str, *, after=None) -> str:
        trigger = build_cron_trigger(cron, timezone="America/Recife")
        fire = trigger.get_next_fire_time(None, after or self.AFTER_MONDAY)
        return fire.strftime("%A")

    def test_digit_5_means_friday(self):
        assert self._next_weekday("0 22 * * 5") == "Friday"

    def test_digit_0_means_sunday(self):
        assert self._next_weekday("0 0 * * 0") == "Sunday"

    def test_digit_7_also_means_sunday(self):
        assert self._next_weekday("0 0 * * 7") == "Sunday"

    def test_digit_6_means_saturday(self):
        assert self._next_weekday("0 22 * * 6") == "Saturday"

    def test_list_5_6_0_means_friday_saturday_sunday(self):
        trigger = build_cron_trigger("0 22 * * 5,6,0", timezone="America/Recife")
        seen = set()
        cursor = self.AFTER_MONDAY
        for _ in range(3):
            fire = trigger.get_next_fire_time(None, cursor)
            seen.add(fire.strftime("%A"))
            cursor = fire + timedelta(minutes=1)  # strictly past this fire, or it re-matches
        assert seen == {"Friday", "Saturday", "Sunday"}, seen

    def test_named_weekday_passes_through_unchanged(self):
        assert self._next_weekday("0 22 * * fri") == "Friday"

    def test_ascending_range_5_dash_6_is_friday_and_saturday(self):
        trigger = build_cron_trigger("0 22 * * 5-6", timezone="America/Recife")
        fire1 = trigger.get_next_fire_time(None, self.AFTER_MONDAY)
        fire2 = trigger.get_next_fire_time(None, fire1 + timedelta(minutes=1))
        assert {fire1.strftime("%A"), fire2.strftime("%A")} == {"Friday", "Saturday"}

    def test_step_expression_is_rejected_not_guessed(self):
        with pytest.raises(InvalidCrawlTargetConfig):
            build_cron_trigger("0 22 * * */2")

    def test_a_week_wrapping_range_is_rejected_not_guessed(self):
        # Standard-cron "6-1" = Saturday through Monday, which wraps past
        # Sunday once translated into APScheduler's Monday-first ordering —
        # genuinely ambiguous to resolve silently.
        with pytest.raises(InvalidCrawlTargetConfig):
            build_cron_trigger("0 22 * * 6-1")

    def test_wrong_field_count_is_rejected(self):
        with pytest.raises(InvalidCrawlTargetConfig):
            build_cron_trigger("0 22 * *")

    def test_out_of_range_digit_is_rejected(self):
        with pytest.raises(InvalidCrawlTargetConfig):
            build_cron_trigger("0 22 * * 8")


# ── §D: cursor is UTC, cron trigger is Recife-local — fails if swapped ──────
def test_cursor_bound_is_utc_and_cron_trigger_is_recife_local_not_swapped():
    """If the cursor's UTC formatting and the cron trigger's local timezone
    were ever swapped (cursor sent local, cron evaluated as UTC), every
    bound this feature computes would be off by exactly Recife's fixed
    3-hour UTC offset (Brazil has had no DST since 2019, so this is a
    constant, not a seasonal flake). This test fails on EITHER swap.
    """
    # Cursor side: a cursor already in UTC must round-trip EXACTLY (no extra
    # -3h/+3h shift applied on top).
    cursor = datetime(2026, 8, 7, 23, 30, 0, tzinfo=timezone.utc)
    bound = compute_bound(cursor, overlap_hours=0.0, lookback_text="3 months", now=NOW)
    assert bound.actor_value == "2026-08-07T23:30:00Z"

    # Cron side: "0 22 * * *" (22:00 every day) interpreted in America/Recife
    # (UTC-3) must fire at 01:00 UTC THE NEXT DAY — three hours LATER in UTC
    # than the wrong reading you'd get by evaluating the same crontab as UTC
    # (which would fire at 22:00 UTC the SAME day). A daily cron carries no
    # day-of-week field to translate, so this is irrelevant to what THIS
    # test proves (the UTC/local HOUR arithmetic only) — see
    # TestBuildCronTriggerWeekday below for the separate day-of-week fix.
    trigger = build_cron_trigger("0 22 * * *", timezone="America/Recife")
    fire = trigger.get_next_fire_time(None, datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc))
    fire_utc = fire.astimezone(timezone.utc)
    assert (fire_utc.month, fire_utc.day, fire_utc.hour) == (8, 7, 1), fire_utc
    # And the UTC-mistaken reading is a DIFFERENT, distinguishable moment —
    # proves the assertion above actually depends on the timezone being
    # honored, not just close enough to pass by coincidence.
    wrong_utc_reading = datetime(2026, 8, 6, 22, 0, tzinfo=timezone.utc)
    assert fire_utc != wrong_utc_reading


# ── group_venue_ids_by_handle: the whole 1,114/1,066 argument ──────────────
def test_group_venue_ids_by_handle_keeps_every_venue_sharing_a_handle():
    handles = {"venue_a": "sharedhandle", "venue_b": "SharedHandle", "venue_c": "otherhandle"}
    grouped = group_venue_ids_by_handle(handles)
    assert grouped["sharedhandle"] == ["venue_a", "venue_b"]
    assert grouped["otherhandle"] == ["venue_c"]


# ── service-level: handle-keyed dedup, budget gate, reels/posts independence ─
class _FakeApifyClient:
    def __init__(self):
        self.calls: list[dict] = []
        self._posts: dict[tuple[str, str], list[dict]] = {}
        self._next_error: dict[tuple[str, str], Exception] = {}
        # plans/260812_crawl-error-visibility.md §A: an Apify error item,
        # programmed per (handle, results_type) — distinct from
        # `_next_error` above (a transport-level EXCEPTION `fetch_recent_
        # posts` itself raises), this is the STRUCTURED item the real client
        # now surfaces via FetchPostsResult instead of silently dropping.
        self._error_items: dict[tuple[str, str], dict] = {}

    def program(self, handle, results_type, posts):
        self._posts[(handle, results_type)] = posts

    def fail_next(self, handle, results_type, exc):
        self._next_error[(handle, results_type)] = exc

    def program_error(self, handle, results_type, *, code, request_error_count=0, description=None):
        self._error_items[(handle, results_type)] = {
            "code": code, "request_error_count": request_error_count, "description": description,
        }

    async def fetch_recent_posts(
        self, handle, results_limit=10, *, only_posts_newer_than=None, results_type="posts",
    ):
        self.calls.append({
            "handle": handle, "results_limit": results_limit,
            "only_posts_newer_than": only_posts_newer_than, "results_type": results_type,
        })
        key = (handle, results_type)
        if key in self._next_error:
            raise self._next_error.pop(key)
        error = self._error_items.get(key)
        return FetchPostsResult(
            posts=list(self._posts.get(key, [])),
            error_code=error.get("code") if error else None,
            error_description=error.get("description") if error else None,
            request_error_count=error.get("request_error_count", 0) if error else 0,
        )


class _FakeBudgetDao:
    def __init__(self):
        self._counts: dict[str, int] = {}

    def current_year_month_utc(self, now=None):
        now = now or datetime.now(timezone.utc)
        return now.strftime("%Y-%m")

    def get_month_count(self, year_month):
        return self._counts.get(year_month, 0)

    def increment_month(self, year_month, n):
        self._counts[year_month] = self._counts.get(year_month, 0) + n
        return self._counts[year_month]


def _venue_dao():
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


def _service(venue_dao, apify_client, budget_dao, **config_overrides):
    config = CrawlServiceConfig(
        overlap_hours=6.0, default_initial_lookback="3 months", default_results_limit=10,
        monthly_result_budget=1000, max_consecutive_failures=5, result_cost_usd=0.0027,
    )
    for k, v in config_overrides.items():
        setattr(config, k, v)
    return ScheduledInstagramCrawlService(
        venue_dao=venue_dao, apify_client=apify_client, budget_dao=budget_dao,
        config=config, chainer=None, now_provider=lambda: NOW,
    )


def test_handle_keyed_dedup_two_venue_rows_one_handle_one_call():
    dao = _venue_dao()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="Venue 1", venue_lat=-8.0, venue_lng=-34.9))
    dao.upsert_venue(Venue(venue_id="v2", venue_name="Venue 2", venue_lat=-8.0, venue_lng=-34.9))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="onehandle", status="found"))
    dao.set_venue_instagram(VenueInstagram(venue_id="v2", instagram_handle="onehandle", status="found"))
    dao.upsert_crawl_target("onehandle", {"kind": "venue", "cron": "0 22 * * 5,6"})

    apify = _FakeApifyClient()
    service = _service(dao, apify, _FakeBudgetDao())

    _run(service.run_target("onehandle"))

    calls = [c for c in apify.calls if c["results_type"] == "posts"]
    assert len(calls) == 1, calls


def test_budget_gate_refuses_before_the_client_is_called():
    """Asserted on CALL COUNT, not on the return value — a gate that runs
    AFTER the spend is not a gate (the plan's own words)."""
    dao = _venue_dao()
    dao.upsert_crawl_target("budgeted", {"kind": "venue", "cron": "0 22 * * 5,6"})
    apify = _FakeApifyClient()
    budget = _FakeBudgetDao()
    budget.increment_month(budget.current_year_month_utc(NOW), 1000)  # fully spent
    service = _service(dao, apify, budget, monthly_result_budget=1000)

    report = _run(service.run_target("budgeted"))

    assert apify.calls == []
    assert report["streams"]["posts"]["outcome"] == "skipped_budget"


# ── plans/260812_crawl-error-visibility.md: outcomes that mean what they
# say, billing that excludes an error item, and consecutive_failures
# arithmetic across a failed run then a successful one ──────────────────────
class TestCrawlErrorVisibility:
    def test_blocked_no_items_with_request_errors_is_recorded_as_blocked(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("blockedtarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("blockedtarget", "posts", code="no_items", request_error_count=11)
        service = _service(dao, apify, _FakeBudgetDao())

        report = _run(service.run_target("blockedtarget"))

        assert report["streams"]["posts"]["outcome"] == "blocked"

    def test_no_items_with_no_request_errors_stays_empty(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("emptytarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("emptytarget", "posts", code="no_items", request_error_count=0)
        service = _service(dao, apify, _FakeBudgetDao())

        report = _run(service.run_target("emptytarget"))

        assert report["streams"]["posts"]["outcome"] == "empty"
        row = dao.get_crawl_target("emptytarget")
        assert row["consecutive_failures"] == 0, row

    def test_not_found_is_recorded_as_handle_not_found_and_disables_the_target(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("deadtarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("deadtarget", "posts", code="not_found")
        service = _service(dao, apify, _FakeBudgetDao())

        report = _run(service.run_target("deadtarget"))

        assert report["streams"]["posts"]["outcome"] == "handle_not_found"
        row = dao.get_crawl_target("deadtarget")
        assert row["enabled"] is False, row
        assert row["last_failure_kind"] == "handle_not_found", row
        assert row["last_failure_at"] is not None, row

    def test_an_unrecognised_error_code_is_a_transient_failure_not_empty(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("weirdtarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("weirdtarget", "posts", code="some_new_code")
        service = _service(dao, apify, _FakeBudgetDao())

        report = _run(service.run_target("weirdtarget"))

        assert report["streams"]["posts"]["outcome"] == "failed"
        row = dao.get_crawl_target("weirdtarget")
        assert row["consecutive_failures"] == 1, row

    def test_an_error_item_is_never_billed(self):
        """The number of KEPT/billed results comes from `.posts`, which the
        client never populates with an error item — this is the fix for
        'an error item currently costs money' verified end to end through
        run_target's own bookkeeping write."""
        dao = _venue_dao()
        dao.upsert_crawl_target("freetarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("freetarget", "posts", code="no_items", request_error_count=5)
        budget = _FakeBudgetDao()
        service = _service(dao, apify, budget)

        _run(service.run_target("freetarget"))

        assert budget.get_month_count(budget.current_year_month_utc(NOW)) == 0
        row = dao.get_crawl_target("freetarget")
        assert row["last_run_results"] == 0, row
        assert row["last_run_cost_usd"] == 0, row

    def test_consecutive_failures_increments_on_a_failed_run_then_resets_on_success(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("flappingtarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("flappingtarget", "posts", code="no_items", request_error_count=3)
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("flappingtarget"))
        row = dao.get_crawl_target("flappingtarget")
        assert row["consecutive_failures"] == 1, row

        apify.program("flappingtarget", "posts", [
            {
                "caption": "post", "likes_count": 0, "comments_count": 0,
                "timestamp": "2026-08-07T10:00:00.000Z", "post_type": "image",
                "shortcode": "recov1", "permalink": "https://instagram.com/p/recov1",
                "image_urls": [], "is_pinned": False,
            },
        ])
        _run(service.run_target("flappingtarget"))
        row = dao.get_crawl_target("flappingtarget")
        assert row["consecutive_failures"] == 0, row

    def test_cursor_does_not_advance_on_a_blocked_stream(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("nocursortarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("nocursortarget", "posts", code="no_items", request_error_count=11)
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("nocursortarget"))

        row = dao.get_crawl_target("nocursortarget")
        assert row["cursor_posts_at"] is None, row


# ── plans/260813_crawl-transport-failure-visibility.md §B: a transport
# failure (FetchPostsResult.error_code == "timeout"/"http_error"/
# "request_error", plan §A) reaches _run_stream through the SAME generic
# "any non-Apify, non-None error_code is a failure" branch 260812 already
# shipped for an unrecognised Apify code -- no new branch, no new outcome
# label. These tests pin that the wiring genuinely reaches consecutive_
# failures/last_failure_kind/cursor exactly like every other FAILURE_
# OUTCOMES member, for each of the three transport codes specifically (not
# just "some unrecognised string", which TestCrawlErrorVisibility already
# covers) -- protecting against a future change to _run_stream's
# classification accidentally special-casing these three strings out of the
# failure path.
class TestCrawlTransportFailureVisibility:
    @pytest.mark.parametrize("code", ["timeout", "http_error", "request_error"])
    def test_a_transport_code_is_recorded_as_a_failure_not_empty(self, code):
        dao = _venue_dao()
        dao.upsert_crawl_target("transporttarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("transporttarget", "posts", code=code, request_error_count=0)
        service = _service(dao, apify, _FakeBudgetDao())

        report = _run(service.run_target("transporttarget"))

        assert report["streams"]["posts"]["outcome"] == "failed", report
        row = dao.get_crawl_target("transporttarget")
        assert row["consecutive_failures"] == 1, row
        assert row["last_failure_kind"] == "failed", row
        assert row["last_failure_at"] is not None, row
        assert row["cursor_posts_at"] is None, row

    def test_a_timeout_increments_an_existing_failure_streak(self):
        dao = _venue_dao()
        dao.upsert_crawl_target(
            "streaktarget", {"kind": "venue", "cron": "0 22 * * 5,6", "consecutive_failures": 1},
        )
        apify = _FakeApifyClient()
        apify.program_error("streaktarget", "posts", code="timeout", request_error_count=0)
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("streaktarget"))

        row = dao.get_crawl_target("streaktarget")
        assert row["consecutive_failures"] == 2, row

    def test_a_timeout_never_bills_and_never_disables_the_target(self):
        """Unlike handle_not_found (permanent -- disables the target), a
        transport failure is transient: the target must stay enabled and
        retryable on the next scheduled fire, and nothing is billed since
        `.posts` is empty."""
        dao = _venue_dao()
        dao.upsert_crawl_target("transienttarget", {"kind": "venue", "cron": "0 22 * * 5,6"})
        apify = _FakeApifyClient()
        apify.program_error("transienttarget", "posts", code="timeout", request_error_count=0)
        budget = _FakeBudgetDao()
        service = _service(dao, apify, budget)

        _run(service.run_target("transienttarget"))

        row = dao.get_crawl_target("transienttarget")
        assert row["enabled"] is True, row
        assert row["last_run_results"] == 0, row
        assert budget.get_month_count(budget.current_year_month_utc(NOW)) == 0


# ── §C split in two: the seed cap vs the steady-state cap ───────────────────
# A brand-new target with a null cursor used to be capped by the SAME small
# `results_limit` (default 10) a steady-state run uses — silently undoing
# the 3-month seed date bound's own job (a venue posting daily has ~90 posts
# in that window). `seed_results_limit` (default 200) is now a SEPARATE
# setting, applied ONLY when the relevant cursor (posts/reels, independently)
# is still null.
class TestSeedVsSteadyStateCap:
    def test_a_null_cursor_uses_the_seed_cap_not_the_steady_state_default(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("seedtarget", {"kind": "venue", "cron": "0 22 * * *"})
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao(), default_seed_results_limit=200)

        _run(service.run_target("seedtarget"))

        assert apify.calls[0]["results_limit"] == 200

    def test_a_null_cursor_uses_the_targets_own_seed_cap_over_the_default(self):
        dao = _venue_dao()
        dao.upsert_crawl_target(
            "ownseedtarget", {"kind": "venue", "cron": "0 22 * * *", "seed_results_limit": 42},
        )
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("ownseedtarget"))

        assert apify.calls[0]["results_limit"] == 42

    def test_a_set_cursor_uses_the_steady_state_cap_not_the_seed_default(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("steadytarget", {
            "kind": "venue", "cron": "0 22 * * *",
            "cursor_posts_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        })
        apify = _FakeApifyClient()
        service = _service(
            dao, apify, _FakeBudgetDao(), default_results_limit=10, default_seed_results_limit=200,
        )

        _run(service.run_target("steadytarget"))

        assert apify.calls[0]["results_limit"] == 10

    def test_a_targets_own_steady_state_cap_is_never_used_for_its_seed(self):
        """The core regression: a target with an explicit STEADY-STATE
        `results_limit` but no cursor yet must still use the SEED cap for
        its first crawl — the two settings are independent, not one
        falling back to the other."""
        dao = _venue_dao()
        dao.upsert_crawl_target(
            "mixedcaptarget", {"kind": "venue", "cron": "0 22 * * *", "results_limit": 5},
        )
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao(), default_seed_results_limit=200)

        _run(service.run_target("mixedcaptarget"))

        assert apify.calls[0]["results_limit"] == 200

    def test_posts_and_reels_seed_independently_by_their_own_cursor(self):
        """A target whose POSTS stream already has a cursor but whose REELS
        stream has never run must seed reels (large cap) while posts stays
        steady-state (small cap) — the seed/steady split is per-stream, the
        same way the cursor itself already is."""
        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="mixedstreamtarget", status="found"))
        dao.upsert_crawl_target("mixedstreamtarget", {
            "kind": "venue", "cron": "0 22 * * *", "crawl_reels": True,
            "cursor_posts_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            # cursor_reels_at intentionally left null.
        })
        apify = _FakeApifyClient()
        service = _service(
            dao, apify, _FakeBudgetDao(), default_results_limit=10, default_seed_results_limit=200,
        )

        _run(service.run_target("mixedstreamtarget"))

        calls_by_type = {c["results_type"]: c["results_limit"] for c in apify.calls}
        assert calls_by_type["posts"] == 10
        assert calls_by_type["reels"] == 200


# ── the monthly budget as an effective cap, not just a refusal ─────────────
# §F's budget check used to be pure refusal ("is there any left at all?"),
# which left a hole: a 200-result seed cap against, say, 50 remaining could
# overshoot to 200 before anything noticed. Refusing outright instead
# (remaining < cap) was considered and rejected — a target with 150 left
# would never seed at all, worse than seeding 150 deep — so the remaining
# budget becomes the EFFECTIVE cap for that call instead.
class TestBudgetCapsTheEffectiveLimit:
    def test_remaining_budget_below_the_cap_reduces_the_request(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("cappedbybudget", {"kind": "venue", "cron": "0 22 * * *"})
        apify = _FakeApifyClient()
        budget = _FakeBudgetDao()
        budget.increment_month(budget.current_year_month_utc(NOW), 950)  # 50 left of 1000
        service = _service(dao, apify, budget, monthly_result_budget=1000, default_seed_results_limit=200)

        _run(service.run_target("cappedbybudget"))

        assert apify.calls[0]["results_limit"] == 50

    def test_remaining_budget_at_or_above_the_cap_does_not_reduce_it(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("plentyofbudget", {"kind": "venue", "cron": "0 22 * * *"})
        apify = _FakeApifyClient()
        budget = _FakeBudgetDao()
        budget.increment_month(budget.current_year_month_utc(NOW), 800)  # 200 left of 1000
        service = _service(dao, apify, budget, monthly_result_budget=1000, default_seed_results_limit=200)

        _run(service.run_target("plentyofbudget"))

        assert apify.calls[0]["results_limit"] == 200  # exactly the cap, not reduced further

    def test_exactly_one_remaining_still_calls_the_actor_for_one(self):
        """The boundary just above the existing `remaining <= 0` refusal —
        1 remaining must still attempt a call (for 1), never silently
        collapse into the same refusal as 0."""
        dao = _venue_dao()
        dao.upsert_crawl_target("oneleft", {"kind": "venue", "cron": "0 22 * * *"})
        apify = _FakeApifyClient()
        budget = _FakeBudgetDao()
        budget.increment_month(budget.current_year_month_utc(NOW), 999)  # 1 left of 1000
        service = _service(dao, apify, budget, monthly_result_budget=1000)

        report = _run(service.run_target("oneleft"))

        assert apify.calls[0]["results_limit"] == 1
        assert report["streams"]["posts"]["outcome"] != "skipped_budget"


# ── reels-specific caps: their own fallback chain, never a new setting ──────
# Posts and reels are already separate billed actor runs with independent
# cursors, so one shared cap over-fetches the higher-volume stream and
# starves the other. `reels_results_limit`/`reels_seed_results_limit` are
# per-target overrides that fall back to the POSTS column, then the same
# settings default posts would use — deliberately no `crawl_default_reels_*`
# setting (three months of reels is far fewer items than three months of
# posts, so the existing seed default will rarely bind on reels anyway).
class TestReelsSpecificCaps:
    def test_reels_seed_falls_back_to_the_posts_seed_cap_when_unset(self):
        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="reelsfallback1", status="found"))
        dao.upsert_crawl_target("reelsfallback1", {
            "kind": "venue", "cron": "0 22 * * *", "crawl_reels": True, "seed_results_limit": 77,
        })
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("reelsfallback1"))

        calls_by_type = {c["results_type"]: c["results_limit"] for c in apify.calls}
        assert calls_by_type["reels"] == 77  # the POSTS seed cap, not the settings default (200)

    def test_reels_seed_uses_its_own_override_over_the_posts_seed_cap(self):
        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="reelsoverride1", status="found"))
        dao.upsert_crawl_target("reelsoverride1", {
            "kind": "venue", "cron": "0 22 * * *", "crawl_reels": True,
            "seed_results_limit": 77, "reels_seed_results_limit": 5,
        })
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("reelsoverride1"))

        calls_by_type = {c["results_type"]: c["results_limit"] for c in apify.calls}
        assert calls_by_type["posts"] == 77   # posts untouched by the reels override
        assert calls_by_type["reels"] == 5    # reels' own override wins

    def test_reels_steady_state_cap_resolution_is_unchanged_but_no_longer_reachable(self):
        """Pre-plans/260811_reels-on-seed-only.md, this asserted a REAL
        Apify call: reels ran on every steady-state run (cursor already
        set) and its cap fell back to the posts steady-state cap. That
        call can no longer happen at all -- reels-on-seed-only's whole
        point is that once `cursor_reels_at` is set, the reels stream
        never runs again (Desired Behavior #2 / Acceptance Criteria). What
        is genuinely unchanged (§Non-goals: "changing... the caps... or
        the cursors' meaning") is `resolve_results_limit`'s own fallback
        chain -- proven directly against the pure function below, since
        `run_target` will never again exercise this branch for reels. The
        full run is still asserted, but for the NEW behavior: reels is
        skipped outright, regardless of what its resolved cap would have
        been.
        """
        config = CrawlServiceConfig(default_results_limit=10, default_seed_results_limit=200)
        target = {"handle": "h", "kind": "venue", "results_limit": 8}
        assert resolve_results_limit(target, STREAM_REELS, is_seed=False, config=config) == 8

        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="reelsfallback2", status="found"))
        dao.upsert_crawl_target("reelsfallback2", {
            "kind": "venue", "cron": "0 22 * * *", "crawl_reels": True, "results_limit": 8,
            "cursor_posts_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "cursor_reels_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        })
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("reelsfallback2"))

        types = {c["results_type"] for c in apify.calls}
        assert types == {"posts"}, types

    def test_reels_steady_state_own_override_cap_resolution_is_unchanged_but_no_longer_reachable(self):
        """Same update as the sibling test above, for the reels-specific-
        override case."""
        config = CrawlServiceConfig(default_results_limit=10, default_seed_results_limit=200)
        target = {"handle": "h", "kind": "venue", "results_limit": 8, "reels_results_limit": 3}
        assert resolve_results_limit(target, STREAM_REELS, is_seed=False, config=config) == 3

        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="reelsoverride2", status="found"))
        dao.upsert_crawl_target("reelsoverride2", {
            "kind": "venue", "cron": "0 22 * * *", "crawl_reels": True,
            "results_limit": 8, "reels_results_limit": 3,
            "cursor_posts_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "cursor_reels_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        })
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("reelsoverride2"))

        calls_by_type = {c["results_type"]: c["results_limit"] for c in apify.calls}
        assert calls_by_type == {"posts": 8}, calls_by_type

    def test_reels_falls_all_the_way_through_to_the_settings_default(self):
        """Neither the reels column NOR the posts column is set — reels
        must land on the exact same settings default posts would use."""
        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="reelsdefault", status="found"))
        dao.upsert_crawl_target("reelsdefault", {"kind": "venue", "cron": "0 22 * * *", "crawl_reels": True})
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao(), default_seed_results_limit=200)

        _run(service.run_target("reelsdefault"))

        calls_by_type = {c["results_type"]: c["results_limit"] for c in apify.calls}
        assert calls_by_type["posts"] == 200
        assert calls_by_type["reels"] == 200


class TestPostsPathUnchangedByReelsCaps:
    """The explicit regression the coordinator asked for: a target with NO
    reels-specific overrides must produce EXACTLY the same posts request it
    did before reels-specific caps existed — proven by setting reels
    columns to values that would be obviously visible if they leaked into
    the posts resolution."""

    def test_posts_ignores_reels_columns_entirely_seed_case(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("postsunaffected1", {
            "kind": "venue", "cron": "0 22 * * *",
            # No posts overrides; reels set to a very different, obviously
            # distinguishable value that must NEVER reach the posts stream.
            "reels_seed_results_limit": 999,
        })
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao(), default_seed_results_limit=200)

        _run(service.run_target("postsunaffected1"))

        assert apify.calls[0]["results_type"] == "posts"
        assert apify.calls[0]["results_limit"] == 200  # the plain settings default, untouched

    def test_posts_ignores_reels_columns_entirely_steady_state_case(self):
        dao = _venue_dao()
        dao.upsert_crawl_target("postsunaffected2", {
            "kind": "venue", "cron": "0 22 * * *", "results_limit": 6,
            "cursor_posts_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "reels_results_limit": 999,
        })
        apify = _FakeApifyClient()
        service = _service(dao, apify, _FakeBudgetDao())

        _run(service.run_target("postsunaffected2"))

        assert apify.calls[0]["results_type"] == "posts"
        assert apify.calls[0]["results_limit"] == 6  # its own cap, not the reels override

    def test_resolve_results_limit_posts_output_is_identical_with_and_without_reels_columns_present(self):
        """Directly against the pure resolver: adding/removing reels-only
        keys on the target dict must never change what POSTS resolves to,
        for both the seed and steady-state cases."""
        from app.services.instagram_crawl_service import resolve_results_limit

        config = CrawlServiceConfig(default_results_limit=10, default_seed_results_limit=200)
        base_seed = {"handle": "h", "kind": "venue"}
        with_reels_seed = {**base_seed, "reels_seed_results_limit": 1, "reels_results_limit": 2}
        assert (
            resolve_results_limit(base_seed, "posts", is_seed=True, config=config)
            == resolve_results_limit(with_reels_seed, "posts", is_seed=True, config=config)
            == 200
        )

        base_steady = {"handle": "h", "kind": "venue", "results_limit": 15}
        with_reels_steady = {**base_steady, "reels_seed_results_limit": 1, "reels_results_limit": 2}
        assert (
            resolve_results_limit(base_steady, "posts", is_seed=False, config=config)
            == resolve_results_limit(with_reels_steady, "posts", is_seed=False, config=config)
            == 15
        )


# ── plans/260811_reels-on-seed-only.md: the reels gate ─────────────────────
class TestReelsOnSeedOnlyGate:
    """`reels_skip_reason`/`reels_already_seeded` are pure functions over a
    target dict — the ONE place this gate is computed, shared by
    `run_target` (what actually runs) and the admin read model's cost
    estimate. Covers the plan's own unit test plan: "The gate: reels
    cursor null, set, and set-with-posts-cursor-null."."""

    def test_runs_when_the_reels_cursor_is_null(self):
        target = {"crawl_reels": True, "cursor_reels_at": None}
        assert reels_skip_reason(target) is None
        assert reels_already_seeded(target) is False

    def test_skips_once_the_reels_cursor_is_set(self):
        target = {
            "crawl_reels": True,
            "cursor_reels_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
        assert reels_skip_reason(target) == OUTCOME_SKIPPED_SEEDED
        assert reels_already_seeded(target) is True

    def test_the_posts_cursor_never_suppresses_a_reels_seed(self):
        """The two streams keep fully independent gates, matching their
        independent cursors — the posts cursor advancing must never
        suppress a reels seed, and this is the case that would prove it
        wrong: posts already has a cursor, reels does not."""
        target = {
            "crawl_reels": True, "cursor_reels_at": None,
            "cursor_posts_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
        assert reels_skip_reason(target) is None

    def test_disabled_is_reported_distinctly_from_already_seeded(self):
        assert reels_skip_reason({"crawl_reels": False}) == OUTCOME_SKIPPED_DISABLED
        assert reels_skip_reason({"crawl_reels": False, "cursor_reels_at": None}) == (
            OUTCOME_SKIPPED_DISABLED
        )

    def test_an_unparseable_cursor_string_is_treated_as_unset(self):
        """Mirrors `_as_utc_dt`'s own "a corrupt cursor must not crash the
        crawl, only be treated as absent" convention — a target read back
        through a JSON boundary (e.g. the admin API) with a garbled
        `cursor_reels_at` must still be seedable, not stuck skipped
        forever."""
        target = {"crawl_reels": True, "cursor_reels_at": "not-a-date"}
        assert reels_skip_reason(target) is None


# ── plans/260813_crawl-transport-failure-visibility.md §E: a stream that has
# never worked is louder than one that failed today. `posts_never_seeded` is
# a pure function over a target dict — the plan's own unit test plan asks
# for exactly the three cases below: cursor null with no runs (not yet
# reportable), cursor null with runs (reportable), cursor set (never
# reportable).
class TestPostsNeverSeededPredicate:
    def test_cursor_null_with_no_runs_is_not_yet_reportable(self):
        """A brand-new target that has never run must not read as broken on
        creation — there is nothing yet to be alarmed about."""
        target = {"cursor_posts_at": None, "last_run_at": None}
        assert posts_never_seeded(target) is False

    def test_cursor_null_with_runs_is_reportable(self):
        """The exact shape downtownbeergarden_ sat in for months: tried at
        least once, never once produced a cursor."""
        target = {
            "cursor_posts_at": None,
            "last_run_at": datetime(2026, 8, 13, 1, 18, tzinfo=timezone.utc),
        }
        assert posts_never_seeded(target) is True

    def test_cursor_set_is_never_reportable(self):
        target = {
            "cursor_posts_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "last_run_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
        }
        assert posts_never_seeded(target) is False

    def test_an_unparseable_cursor_string_is_treated_as_unset(self):
        """Mirrors `reels_already_seeded`'s own convention: a target read
        back through a JSON boundary (e.g. the admin API) with a garbled
        `cursor_posts_at` must still be reportable, not silently hidden."""
        target = {
            "cursor_posts_at": "not-a-date",
            "last_run_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
        }
        assert posts_never_seeded(target) is True


def test_a_failed_seeds_bookkeeping_write_leaves_the_reels_cursor_null_and_the_retry_seeds_reels_again():
    """§4 of the plan's Desired Behavior: "A seed that fails still crawls
    reels when it is retried." Simulates the exact production shape
    (`_BookkeepingFailsOnceDao`, the same fake `TestBookkeepingWriteFailure
    CountsAsAFailure` uses): the scrape succeeds and is billed, but the
    bookkeeping write that would have recorded `cursor_reels_at` fails, so
    the cursor -- the ONLY thing the gate reads -- stays null. The retry
    must therefore seed reels again, not skip it as "already seeded"."""
    inner = _venue_dao()
    inner.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
    inner.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="reelsretry", status="found"))
    inner.upsert_crawl_target("reelsretry", {"kind": "venue", "cron": "0 22 * * *", "crawl_reels": True})
    dao = _BookkeepingFailsOnceDao(inner)
    apify = _FakeApifyClient()
    apify.program("reelsretry", "posts", [
        {"shortcode": "p1", "timestamp": "2026-08-08T10:00:00.000Z"},
    ])
    apify.program("reelsretry", "reels", [
        {"shortcode": "r1", "timestamp": "2026-08-08T09:00:00.000Z"},
    ])
    service = _service(dao, apify, _FakeBudgetDao())

    with pytest.raises(RuntimeError):
        _run(service.run_target("reelsretry"))

    row = inner.get_crawl_target("reelsretry")
    assert row["cursor_reels_at"] is None
    assert row["cursor_posts_at"] is None

    # The retry: the same wrapper's failure is a one-shot (it is now
    # disarmed), so this second call's bookkeeping write lands for real.
    _run(service.run_target("reelsretry"))

    retry_calls = apify.calls[2:]  # after the two calls the first attempt made
    assert {c["results_type"] for c in retry_calls} == {"posts", "reels"}, retry_calls


# ── the budget IS re-read between streams within one run ────────────────────
# Confirmed (not assumed, per the coordinator's instruction) by inspection of
# `_run_stream`/`run_target`: the loop in `run_target` `await`s each stream
# to completion before starting the next (no concurrency), and `_run_stream`
# reads `self.budget_dao.get_month_count(...)` FRESH on every call — never a
# snapshot taken once and shared. So a reels seed that runs after a posts
# seed in the SAME `run_target` call sees the budget already reduced by
# whatever posts just spent, and clamps accordingly. This test proves it
# rather than trusting the reading of the code.
async def test_the_reels_seed_sees_the_budget_already_spent_by_the_posts_seed_in_the_same_run():
    dao = _venue_dao()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="budgetsharedhandle", status="found"))
    dao.upsert_crawl_target("budgetsharedhandle", {"kind": "venue", "cron": "0 22 * * *", "crawl_reels": True})

    apify = _FakeApifyClient()
    # Exactly 200 posts programmed so requesting the (unclamped) 200-seed
    # cap returns exactly 200 results — making the budget arithmetic exact,
    # not an upper bound.
    apify.program("budgetsharedhandle", "posts", [
        {"shortcode": f"p{i}", "timestamp": "2026-08-05T09:00:00.000Z", "is_pinned": False,
         "caption": "", "image_urls": []}
        for i in range(200)
    ])
    budget = _FakeBudgetDao()
    budget.increment_month(budget.current_year_month_utc(NOW), 750)  # 250 left of 1000
    service = _service(dao, apify, budget, monthly_result_budget=1000, default_seed_results_limit=200)

    await service.run_target("budgetsharedhandle")

    calls_by_type = {c["results_type"]: c["results_limit"] for c in apify.calls}
    assert calls_by_type["posts"] == 200, "250 remaining >= 200 cap: posts is not reduced"
    assert calls_by_type["reels"] == 50, (
        "reels must see remaining=50 (250 - the 200 posts ACTUALLY spent), "
        "not the pre-run snapshot of 250 — a stale snapshot would send 200 "
        "here too and let the run spend 400 against only 250 available"
    )


def test_reels_and_posts_cursors_move_independently():
    dao = _venue_dao()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="reelshandle", status="found"))
    dao.upsert_crawl_target("reelshandle", {"kind": "venue", "cron": "0 22 * * 5,6", "crawl_reels": True})
    apify = _FakeApifyClient()
    apify.program("reelshandle", "posts", [{
        "shortcode": "p1", "timestamp": "2026-08-05T09:00:00.000Z", "is_pinned": False,
        "caption": "", "image_urls": [],
    }])
    # No reels programmed -> empty list -> reels outcome is "empty", cursor
    # must stay untouched (null, since it was never set).
    service = _service(dao, apify, _FakeBudgetDao())

    _run(service.run_target("reelshandle"))

    row = dao.get_crawl_target("reelshandle")
    assert row["cursor_posts_at"] == datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    assert row["cursor_reels_at"] is None


def test_credit_exhaustion_is_reported_and_does_not_raise():
    dao = _venue_dao()
    dao.upsert_crawl_target("exhausted", {"kind": "venue", "cron": "0 22 * * 5,6"})
    apify = _FakeApifyClient()
    apify.fail_next("exhausted", "posts", ApifyCreditExhaustedError("no credit"))
    service = _service(dao, apify, _FakeBudgetDao())

    report = _run(service.run_target("exhausted"))

    assert report["credit_exhausted"] is True
    assert report["streams"]["posts"]["outcome"] == "credit_exhausted"


def test_consecutive_failures_increments_on_failure_and_resets_on_success():
    dao = _venue_dao()
    dao.upsert_crawl_target("flaky", {"kind": "venue", "cron": "0 22 * * 5,6"})
    apify = _FakeApifyClient()
    apify.fail_next("flaky", "posts", RuntimeError("boom"))
    service = _service(dao, apify, _FakeBudgetDao())

    _run(service.run_target("flaky"))
    assert dao.get_crawl_target("flaky")["consecutive_failures"] == 1

    apify.program("flaky", "posts", [])  # succeeds (empty result)
    _run(service.run_target("flaky"))
    assert dao.get_crawl_target("flaky")["consecutive_failures"] == 0


def test_a_target_past_the_failure_threshold_is_skipped_without_a_call():
    dao = _venue_dao()
    dao.upsert_crawl_target(
        "deadhandle", {"kind": "venue", "cron": "0 22 * * 5,6", "consecutive_failures": 5},
    )
    apify = _FakeApifyClient()
    service = _service(dao, apify, _FakeBudgetDao(), max_consecutive_failures=5)

    report = _run(service.run_target("deadhandle"))

    assert apify.calls == []
    assert report["outcome"] == "skipped_failures"


# ── §H promoter-kind chaining: `InstagramCrawlChainer.chain_promoter` ───────
# calls PromoterCrawlService's OWN per-post units (`_archive_post_images`,
# `_process_post`) directly rather than `.run()` — see instagram_crawl_
# service.py's module docstring for why `.run()` would either re-fetch (
# double-billing) or silently skip a target not ALSO marked active in the
# separate promoter_account registry. This test proves that reuse actually
# persists a real event through the shared reconciliation path, using the
# same fakes-at-the-boundary pattern tests/test_promoter_crawl_service.py
# already established for PromoterCrawlService itself.
class _FakePromoterDownloader:
    async def download(self, url, timeout=15.0, max_bytes=None):
        return b"FAKE_IMAGE_BYTES", "image/jpeg"


class _FakePromoterMediaStore:
    def __init__(self):
        self.images: list[str] = []
        self.manifests: list[tuple] = []

    async def put_promoter_image(self, *, prefix, handle, photo_id, data, content_type, category=None):
        key = f"{prefix}promoter={handle}/media/{photo_id}.jpg"
        self.images.append(key)
        return key

    async def put_promoter_manifest(self, *, prefix, handle, manifest):
        self.manifests.append((prefix, handle, manifest))
        return f"{prefix}promoter={handle}/info/_manifest.json"


class _FakePromoterOpenAIClient:
    def __init__(self, response: str):
        self._response = response
        self.calls = 0

    async def extract_events(self, *, caption, image_data_uri=None, max_events):
        import json as _json

        self.calls += 1
        wrapped = _json.dumps({"events": [_json.loads(self._response)]})
        return wrapped, False


def test_chain_promoter_archives_and_persists_a_real_event():
    from app.services.promoter_crawl_service import PromoterCrawlService

    dao = _venue_dao()
    media_store = _FakePromoterMediaStore()
    downloader = _FakePromoterDownloader()
    openai_client = _FakePromoterOpenAIClient(
        '{"title": "Festa", "description": null, "date_text": null, '
        '"time_text": null, "is_recurring": false, "recurrence_text": null, '
        '"lineup": [], "ticket_url": null, "price_text": null, '
        '"location_text": null, "confidence": 0.9}'
    )
    promoter_service = PromoterCrawlService(
        venue_dao=dao, posts_client=None, media_store=media_store,
        downloader=downloader, openai_client=openai_client, min_confidence=0.0,
    )
    chainer = InstagramCrawlChainer(
        media_store=None, downloader=None, event_extraction_service=None,
        promoter_crawl_service=promoter_service,
    )
    post = {
        "shortcode": "promopost1", "caption": "Ingressos abertos! Vem pro role.",
        "permalink": "https://instagram.com/p/promopost1", "timestamp": "2026-08-05T20:00:00.000Z",
        "image_urls": ["https://cdn.example.com/promopost1.jpg"], "is_pinned": False,
    }

    report = _run(chainer.chain_promoter(handle="promohandle", new_posts=[post], now=NOW))

    assert report.archived == 1
    assert report.extracted is True
    assert openai_client.calls == 1
    row = dao.get_event_by_source("promohandle", "promopost1")
    assert row is not None and row["title"] == "Festa"


def test_chain_venue_and_chain_promoter_no_op_without_new_posts():
    chainer = InstagramCrawlChainer()
    report = _run(chainer.chain_venue(handle="h", venue_ids=["v1"], new_posts=[], now=NOW))
    assert report.archived == 0 and report.extracted is False
    report2 = _run(chainer.chain_promoter(handle="h", new_posts=[], now=NOW))
    assert report2.archived == 0 and report2.extracted is False


# ── §H venue-kind chaining classifies images: an image-only flyer must
# still reach extraction ────────────────────────────────────────────────────
# The scheduled crawl REPLACES a manual VenuePhotoArchiveService run that
# already classifies photos (flyer detection). Before this fix,
# `chain_venue` wrote manifest entries with no `category` at all, so
# `post_qualifies` (event_extraction_service.py) could only ever qualify a
# post via `matches_event_marker(caption)` — an image-only flyer (the
# graphic carries every word, the caption is a few emoji) was silently
# skipped, and the outcome (`not_event_like`) read as "no event" rather
# than "never classified". These tests prove classification now runs by
# default, reaches extraction end-to-end, is skippable per target, and is
# distinguishable in CRAWL_CHAIN_CLASSIFICATION_TOTAL when it does not run.
class _FakeVenueMediaStore:
    """Implements BOTH the write side `InstagramCrawlChainer` calls
    (`put_image`/`put_manifest`) and the read side the REAL `EventPostSource`
    calls (`list_run_prefixes`/`read_manifest`) over one in-memory dict —
    mirrors tests/bdd/steps/scheduled_incremental_instagram_crawl_steps.py's
    `_FakeMediaStore`, proving archiving and extraction are wired together
    end to end rather than asserted as two independent mock calls."""

    def __init__(self):
        self.images: list[str] = []
        self.manifests: dict[tuple, dict] = {}
        self._prefixes: list[str] = []

    async def put_image(self, *, prefix, venue_id, photo_id, data, content_type, category=None):
        key = f"{prefix}venue_id={venue_id}/media/{photo_id}.jpg"
        self.images.append(key)
        return key

    async def put_manifest(self, *, prefix, venue_id, manifest):
        self.manifests[(prefix, venue_id)] = manifest
        if prefix not in self._prefixes:
            self._prefixes.append(prefix)
        return f"{prefix}venue_id={venue_id}/info/_manifest.json"

    async def list_run_prefixes(self, source):
        return sorted(self._prefixes)

    async def read_manifest(self, prefix, venue_id):
        return self.manifests.get((prefix, venue_id))

    async def read_image_data_uri(self, key):
        return f"data:image/jpeg;base64,FAKE_{key}" if key else None


class _FakeVenueDownloader:
    async def download(self, url, timeout=15.0, max_bytes=None):
        return b"FAKE_IMAGE_BYTES", "image/jpeg"


class _FakePhotoClassifier:
    """Simulates `PhotoClassificationService.annotate`: attaches `category`/
    `classification_confidence` to every photo IN PLACE — the exact contract
    `InstagramCrawlChainer` depends on."""

    def __init__(self, category="flyer", confidence=0.92):
        self.category = category
        self.confidence = confidence
        self.calls = 0

    async def annotate(self, photos, *, derive_attributes=True, venue_id="", require_bytes=False):
        self.calls += 1
        for photo in photos:
            photo["category"] = self.category
            photo["classification_confidence"] = self.confidence
        return {
            "classified": len(photos), "attributed": 0, "cost_usd": 0.0,
            "input_tokens": 0, "output_tokens": 0,
        }


class _FailingPhotoClassifier:
    async def annotate(self, photos, *, derive_attributes=True, venue_id="", require_bytes=False):
        raise RuntimeError("classifier exploded")


def _venue_extraction_service(dao, media_store, openai_client):
    from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS
    from app.services.event_extraction_service import EventExtractionService, EventPostSource

    return EventExtractionService(
        venue_dao=dao,
        post_source=EventPostSource(media_store=media_store, archive_source=SOURCE_INSTAGRAM_POSTS),
        openai_client=openai_client, min_confidence=0.0, now_provider=lambda: NOW,
    )


def test_image_only_flyer_with_no_caption_marker_reaches_extraction_when_classified():
    dao = _venue_dao()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="flyerhandle", status="found"))

    media_store = _FakeVenueMediaStore()
    classifier = _FakePhotoClassifier(category="flyer", confidence=0.92)
    openai_client = _FakePromoterOpenAIClient(
        '{"title": "Festa Surpresa", "description": null, "date_text": null, '
        '"time_text": null, "is_recurring": false, "recurrence_text": null, '
        '"lineup": [], "ticket_url": null, "price_text": null, '
        '"location_text": null, "confidence": 0.9}'
    )
    extraction_service = _venue_extraction_service(dao, media_store, openai_client)
    chainer = InstagramCrawlChainer(
        media_store=media_store, downloader=_FakeVenueDownloader(),
        event_extraction_service=extraction_service, photo_classifier=classifier,
    )
    # No event-marker anywhere in the caption — pure emoji, exactly the
    # image-only-flyer case that was silently dropped before this fix.
    post = {
        "shortcode": "flyerpost1", "caption": "\U0001F525\U0001F525\U0001F525",
        "permalink": "https://instagram.com/p/flyerpost1", "timestamp": "2026-08-05T20:00:00.000Z",
        "image_urls": ["https://cdn.example.com/flyerpost1.jpg"], "is_pinned": False,
    }

    report = _run(chainer.chain_venue(
        handle="flyerhandle", venue_ids=["v1"], new_posts=[post], now=NOW, classify_images=True,
    ))

    assert classifier.calls == 1
    assert report.classification_outcome == "classified"
    assert openai_client.calls == 1, "extraction must have run for the image-only flyer"
    row = dao.get_event_by_source("flyerhandle", "flyerpost1")
    assert row is not None and row["title"] == "Festa Surpresa"


def test_image_only_flyer_is_missed_when_classification_is_skipped():
    """The BEFORE picture, pinned as a contrast: with no classifier wired,
    the same image-only-caption post never qualifies for extraction — proves
    the fix in the previous test is what makes the difference, not an
    unrelated change."""
    dao = _venue_dao()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="noclassifierhandle", status="found"))

    media_store = _FakeVenueMediaStore()
    openai_client = _FakePromoterOpenAIClient('{"title": "x", "description": null, '
        '"date_text": null, "time_text": null, "is_recurring": false, '
        '"recurrence_text": null, "lineup": [], "ticket_url": null, '
        '"price_text": null, "location_text": null, "confidence": 0.9}')
    extraction_service = _venue_extraction_service(dao, media_store, openai_client)
    chainer = InstagramCrawlChainer(
        media_store=media_store, downloader=_FakeVenueDownloader(),
        event_extraction_service=extraction_service, photo_classifier=None,
    )
    post = {
        "shortcode": "noclassifierpost1", "caption": "\U0001F525\U0001F525\U0001F525",
        "permalink": "https://instagram.com/p/noclassifierpost1", "timestamp": "2026-08-05T20:00:00.000Z",
        "image_urls": ["https://cdn.example.com/noclassifierpost1.jpg"], "is_pinned": False,
    }

    report = _run(chainer.chain_venue(
        handle="noclassifierhandle", venue_ids=["v1"], new_posts=[post], now=NOW, classify_images=True,
    ))

    assert report.classification_outcome == "skipped_no_classifier"
    assert openai_client.calls == 0, "an unclassified, caption-less flyer must not qualify"
    assert dao.get_event_by_source("noclassifierhandle", "noclassifierpost1") is None


def test_classify_images_false_skips_classification_even_with_a_classifier_wired():
    dao = _venue_dao()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="cheapmodehandle", status="found"))

    media_store = _FakeVenueMediaStore()
    classifier = _FakePhotoClassifier()
    chainer = InstagramCrawlChainer(
        media_store=media_store, downloader=_FakeVenueDownloader(),
        event_extraction_service=None, photo_classifier=classifier,
    )
    post = {
        "shortcode": "cheapmodepost1", "caption": "no marker here",
        "permalink": "https://instagram.com/p/cheapmodepost1", "timestamp": "2026-08-05T20:00:00.000Z",
        "image_urls": ["https://cdn.example.com/cheapmodepost1.jpg"], "is_pinned": False,
    }

    report = _run(chainer.chain_venue(
        handle="cheapmodehandle", venue_ids=["v1"], new_posts=[post], now=NOW, classify_images=False,
    ))

    assert classifier.calls == 0, "the per-target toggle must gate the classifier call itself"
    assert report.classification_outcome == "skipped_target_disabled"


def test_a_classifier_failure_archives_without_a_category_and_is_recorded_as_failed():
    dao = _venue_dao()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="failinghandle", status="found"))

    media_store = _FakeVenueMediaStore()
    chainer = InstagramCrawlChainer(
        media_store=media_store, downloader=_FakeVenueDownloader(),
        event_extraction_service=None, photo_classifier=_FailingPhotoClassifier(),
    )
    post = {
        "shortcode": "failingpost1", "caption": "no marker here",
        "permalink": "https://instagram.com/p/failingpost1", "timestamp": "2026-08-05T20:00:00.000Z",
        "image_urls": ["https://cdn.example.com/failingpost1.jpg"], "is_pinned": False,
    }

    report = _run(chainer.chain_venue(
        handle="failinghandle", venue_ids=["v1"], new_posts=[post], now=NOW, classify_images=True,
    ))

    # A blown-up classifier must never cost the photo its archive.
    assert report.archived == 1
    assert report.classification_outcome == "classification_failed"


# ── `start_run`/`is_running`: the admin run-now endpoint's two defects ──────
# #164 shipped `run_crawl_target_now` awaiting `run_target` inline, with no
# lock. Both bite hardest on exactly the run an operator wants most: the big
# historical seed of a target they just created. Fixed by `start_run`, which
# acquires the SAME per-handle lock the scheduler uses (`lock_name_for`) and
# returns immediately via `asyncio.create_task` — these tests are genuine
# `async def` tests (this repo's pytest-asyncio runs in auto mode) so a
# background task actually gets a chance to run concurrently with the test
# body, which a sync test wrapped in its own fresh event loop cannot exercise
# at all.
class _GatedApifyClient:
    """`fetch_recent_posts` blocks on an `asyncio.Event` until the test lets
    it proceed — the seam that makes "start_run returns before the crawl
    finishes" and "running flips true then false" provable rather than
    assumed."""

    def __init__(self, gate: asyncio.Event):
        self._gate = gate
        self.calls = 0

    async def fetch_recent_posts(
        self, handle, results_limit=10, *, only_posts_newer_than=None, results_type="posts",
    ):
        self.calls += 1
        await self._gate.wait()
        return FetchPostsResult(posts=[])


async def test_start_run_returns_immediately_and_running_flips_true_then_false():
    dao = _venue_dao()
    dao.upsert_crawl_target("slowhandle", {"kind": "venue", "cron": "0 22 * * *"})
    gate = asyncio.Event()
    apify = _GatedApifyClient(gate)
    service = _service(dao, apify, _FakeBudgetDao())

    assert service.is_running("slowhandle") is False

    result = await service.start_run("slowhandle")

    # Returned WITHOUT waiting for fetch_recent_posts (still blocked on the
    # gate) to complete — the whole point of the fix.
    assert result == {"started": True}
    assert service.is_running("slowhandle") is True

    await asyncio.sleep(0)  # let the background task actually run up to the gate
    assert apify.calls == 1

    gate.set()
    for _ in range(100):
        if not service.is_running("slowhandle"):
            break
        await asyncio.sleep(0)
    assert service.is_running("slowhandle") is False

    row = dao.get_crawl_target("slowhandle")
    assert row["last_run_at"] is not None  # the background task actually completed


async def test_start_run_holds_a_strong_reference_to_the_background_task_until_it_completes():
    """`asyncio.create_task`'s return value is only WEAKLY referenced by the
    event loop — CPython's own docs warn an unreferenced task "can
    disappear mid-execution" via garbage collection. Here that would mean
    the per-handle lock's `finally` never runs: the handle becomes
    uncrawlable for the rest of the process's life. This asserts the
    bookkeeping that prevents it — the task is tracked while in flight and
    untracked once done — WITHOUT provoking garbage collection, which would
    be slow, flaky, and prove nothing an honest test couldn't already
    show more directly."""
    dao = _venue_dao()
    dao.upsert_crawl_target("refheldhandle", {"kind": "venue", "cron": "0 22 * * *"})
    gate = asyncio.Event()
    apify = _GatedApifyClient(gate)
    service = _service(dao, apify, _FakeBudgetDao())

    assert service._background_tasks == set()

    result = await service.start_run("refheldhandle")
    assert result == {"started": True}
    await asyncio.sleep(0)  # let the task actually start and reach the gate

    assert len(service._background_tasks) == 1
    in_flight_task = next(iter(service._background_tasks))
    assert not in_flight_task.done()

    gate.set()
    for _ in range(100):
        if not service._background_tasks:
            break
        await asyncio.sleep(0)
    assert service._background_tasks == set()
    assert in_flight_task.done()


async def test_a_second_run_now_while_one_is_in_flight_is_refused_without_calling_the_actor():
    dao = _venue_dao()
    dao.upsert_crawl_target("racyhandle", {"kind": "venue", "cron": "0 22 * * *"})
    gate = asyncio.Event()
    apify = _GatedApifyClient(gate)
    service = _service(dao, apify, _FakeBudgetDao())

    first = await service.start_run("racyhandle")
    assert first == {"started": True}
    await asyncio.sleep(0)  # let the background task actually run up to the gate
    assert apify.calls == 1

    second = await service.start_run("racyhandle")

    assert second == {"started": False, "reason": "already_running"}
    assert apify.calls == 1, "the second attempt must never reach the actor"

    gate.set()
    for _ in range(100):
        if not service.is_running("racyhandle"):
            break
        await asyncio.sleep(0)


async def test_start_run_budget_gate_refuses_before_the_client_is_called():
    """Asserted on call count, not on the return value — the plan's own
    words, now also true of the BACKGROUNDED entry point: a backgrounded
    start must never become a way around the check."""
    dao = _venue_dao()
    dao.upsert_crawl_target("budgetedhandle", {"kind": "venue", "cron": "0 22 * * *"})
    apify = _FakeApifyClient()
    budget = _FakeBudgetDao()
    budget.increment_month(budget.current_year_month_utc(NOW), 1000)  # fully spent
    service = _service(dao, apify, budget, monthly_result_budget=1000)

    result = await service.start_run("budgetedhandle")

    assert result == {"started": False, "reason": "skipped_budget"}
    assert apify.calls == []
    assert service.is_running("budgetedhandle") is False, "the lock must be released, not held forever"


async def test_start_run_refuses_a_disabled_target_without_a_lock_left_behind():
    dao = _venue_dao()
    dao.upsert_crawl_target("disabledhandle", {"kind": "venue", "cron": "0 22 * * *", "enabled": False})
    apify = _FakeApifyClient()
    service = _service(dao, apify, _FakeBudgetDao())

    result = await service.start_run("disabledhandle")

    assert result == {"started": False, "reason": "skipped_disabled"}
    assert apify.calls == []
    assert service.is_running("disabledhandle") is False


async def test_start_run_refuses_a_target_past_the_failure_threshold():
    dao = _venue_dao()
    dao.upsert_crawl_target(
        "failedoutthreshold", {"kind": "venue", "cron": "0 22 * * *", "consecutive_failures": 5},
    )
    apify = _FakeApifyClient()
    service = _service(dao, apify, _FakeBudgetDao(), max_consecutive_failures=5)

    result = await service.start_run("failedoutthreshold")

    assert result == {"started": False, "reason": "skipped_failures"}
    assert apify.calls == []


async def test_start_run_missing_target_is_reported_not_raised():
    dao = _venue_dao()
    service = _service(dao, _FakeApifyClient(), _FakeBudgetDao())

    result = await service.start_run("nosuchhandle")

    assert result == {"started": False, "reason": "not_found"}


def test_lock_name_for_matches_the_namespace_crawl_schedule_sync_imports():
    """crawl_schedule_sync.py imports THIS function rather than defining its
    own — asserting identity here would be circular; asserting the shape is
    what actually matters: any string is fine as long as there is exactly
    ONE function computing it, which the import (not a duplicate literal)
    is what guarantees."""
    assert lock_name_for("somehandle") == "crawl_target:somehandle"


# ── the post-run bookkeeping write failing must not look like a healthy run ──
# Production incident, 2026-08-09 (entreamigos.praia): the bookkeeping write
# used to be `upsert_crawl_target(handle, updates)` — a partial field set
# (cursor/last_run only, no kind/cron) against a row that already existed.
# A real Postgres validates NOT NULL on the INSERT attempt itself, before it
# ever evaluates ON CONFLICT, so the write raised NotNullViolation even
# though the row certainly existed — AFTER 32 results had already been
# scraped and billed. The exception escaped past the path that increments
# `consecutive_failures`, so the target looked perfectly healthy (0 failures,
# `running` false — the lock's own `finally` released it correctly) while
# spending money and landing nothing, forever, on every future run.
class _BookkeepingFailsOnceDao:
    """Wraps a real VenueRepository (backed by the in-memory fake, so reads
    reflect a genuine row shape) and makes exactly the FIRST
    `update_crawl_target` call raise — simulating the incident's real write
    failure. Every later call (the fallback failure-count write, and
    anything a later run does) goes through to the real delegate."""

    def __init__(self, inner):
        self._inner = inner
        self._armed = True

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def update_crawl_target(self, handle, fields):
        if self._armed:
            self._armed = False
            raise RuntimeError('null value in column "kind" violates not-null constraint')
        return self._inner.update_crawl_target(handle, fields)


class _BookkeepingAlwaysFailsDao:
    """Every `update_crawl_target` call raises — simulating the database
    itself being unreachable, so even the fallback failure-count write
    cannot land."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def update_crawl_target(self, handle, fields):
        raise RuntimeError("connection refused")


class TestBookkeepingWriteFailureCountsAsAFailure:
    def test_a_successful_scrape_whose_bookkeeping_write_then_fails_reraises_and_still_bills(self):
        """The scrape and the billing (CRAWL_RESULTS_TOTAL / the budget
        increment) already happened inside `_run_stream`, entirely before
        the bookkeeping write — proven here by the actor call count, exactly
        the code-path evidence (not S3 inference) for "did the $0.10 buy
        anything real": yes, the scrape and the bill, but nothing else."""
        inner = _venue_dao()
        inner.upsert_crawl_target("bookkeepfail", {"kind": "venue", "cron": "0 22 * * *"})
        dao = _BookkeepingFailsOnceDao(inner)
        apify = _FakeApifyClient()
        apify.program("bookkeepfail", "posts", [
            {"shortcode": "a", "timestamp": "2026-08-08T10:00:00.000Z"},
        ])
        budget = _FakeBudgetDao()
        service = _service(dao, apify, budget)

        with pytest.raises(RuntimeError):
            _run(service.run_target("bookkeepfail"))

        assert len(apify.calls) == 1  # the scrape genuinely happened, once

        row = inner.get_crawl_target("bookkeepfail")
        assert row["consecutive_failures"] == 1, (
            "a bookkeeping failure must count as a failure -- otherwise the "
            "target looks healthy forever while spending and landing nothing"
        )
        # The failed write's OWN fields (cursor/last_run_*) never landed —
        # only the fallback failure-count write did.
        assert row["last_run_at"] is None
        assert row["cursor_posts_at"] is None

    def test_the_failure_count_accumulates_on_top_of_prior_failures(self):
        inner = _venue_dao()
        inner.upsert_crawl_target(
            "bookkeepfail2", {"kind": "venue", "cron": "0 22 * * *", "consecutive_failures": 2},
        )
        dao = _BookkeepingFailsOnceDao(inner)
        apify = _FakeApifyClient()
        apify.program("bookkeepfail2", "posts", [
            {"shortcode": "a", "timestamp": "2026-08-08T10:00:00.000Z"},
        ])
        service = _service(dao, apify, _FakeBudgetDao())

        with pytest.raises(RuntimeError):
            _run(service.run_target("bookkeepfail2"))

        assert inner.get_crawl_target("bookkeepfail2")["consecutive_failures"] == 3

    def test_a_wholly_unreachable_database_still_reraises_the_original_error(self):
        """If even the fallback failure-count write cannot land (the
        database itself is down, not just the first write's field set),
        this must not be swallowed -- there is nothing left to record, so
        the original exception is the only signal left, and it must reach
        the caller (run_due_targets' per-handle scheduled job, or start_run's
        backgrounded task) so its own error handling and metrics see it."""
        inner = _venue_dao()
        inner.upsert_crawl_target("bookkeepfail3", {"kind": "venue", "cron": "0 22 * * *"})
        dao = _BookkeepingAlwaysFailsDao(inner)
        apify = _FakeApifyClient()
        apify.program("bookkeepfail3", "posts", [
            {"shortcode": "a", "timestamp": "2026-08-08T10:00:00.000Z"},
        ])
        service = _service(dao, apify, _FakeBudgetDao())

        with pytest.raises(RuntimeError, match="connection refused"):
            _run(service.run_target("bookkeepfail3"))

    def test_the_archive_and_extract_chain_never_runs_when_the_bookkeeping_write_fails(self):
        """Established from the CODE PATH, not inferred from S3: `_chain`
        (archive/download/classify/extract) is called strictly AFTER the
        bookkeeping write in `run_target` — proven here by making the write
        raise and asserting the chainer is never invoked, for the exact
        target/posts pair that WOULD have been chained had the write
        succeeded."""
        inner = _venue_dao()
        inner.upsert_venue(Venue(venue_id="cv1", venue_name="CV1", venue_lat=-8.0, venue_lng=-34.9))
        inner.set_venue_instagram(
            VenueInstagram(venue_id="cv1", instagram_handle="bookkeepfail4", status="found")
        )
        inner.upsert_crawl_target("bookkeepfail4", {"kind": "venue", "cron": "0 22 * * *"})
        dao = _BookkeepingFailsOnceDao(inner)
        apify = _FakeApifyClient()
        apify.program("bookkeepfail4", "posts", [
            {"shortcode": "a", "timestamp": "2026-08-08T10:00:00.000Z"},
        ])

        class _RecordingChainer:
            def __init__(self):
                self.calls = 0

            async def chain_venue(self, **kwargs):
                self.calls += 1
                return None

            async def chain_promoter(self, **kwargs):
                self.calls += 1
                return None

        chainer = _RecordingChainer()
        config = CrawlServiceConfig(
            overlap_hours=6.0, default_initial_lookback="3 months", default_results_limit=10,
            monthly_result_budget=1000, max_consecutive_failures=5, result_cost_usd=0.0027,
        )
        service = ScheduledInstagramCrawlService(
            venue_dao=dao, apify_client=apify, budget_dao=_FakeBudgetDao(),
            config=config, chainer=chainer, now_provider=lambda: NOW,
        )

        with pytest.raises(RuntimeError):
            _run(service.run_target("bookkeepfail4"))

        assert chainer.calls == 0, "archiving must never run past a failed bookkeeping write"


# ── §A dedupe: plans/260810_stream-dedupe-and-venue-attribution.md ─────────
# A reel is also a grid post, so the posts and reels streams overlap — the
# same image was archived, classified, and extracted twice downstream.
# `dedupe_posts_by_shortcode` is the pure function `run_target` now calls
# instead of a bare concatenation of every stream's kept list.
class TestDedupePostsByShortcode:
    def test_a_shortcode_returned_by_both_streams_appears_once(self):
        posts = [{"shortcode": "a", "caption": "x", "image_urls": ["u1"]}]
        reels = [{"shortcode": "a", "caption": "x", "image_urls": ["u1"]}]
        out = dedupe_posts_by_shortcode({STREAM_POSTS: posts, STREAM_REELS: reels})
        assert [p["shortcode"] for p in out] == ["a"]

    def test_a_shortcode_unique_to_one_stream_is_kept(self):
        posts = [{"shortcode": "a", "caption": "x", "image_urls": ["u1"]}]
        reels = [{"shortcode": "b", "caption": "y", "image_urls": ["u2"]}]
        out = dedupe_posts_by_shortcode({STREAM_POSTS: posts, STREAM_REELS: reels})
        assert {p["shortcode"] for p in out} == {"a", "b"}

    def test_differing_copies_prefer_the_richer_payload_by_image_count(self):
        thin = {"shortcode": "a", "caption": "x", "image_urls": ["u1"]}
        rich = {"shortcode": "a", "caption": "x", "image_urls": ["u1", "u2", "u3"]}
        out = dedupe_posts_by_shortcode({STREAM_POSTS: [thin], STREAM_REELS: [rich]})
        assert out == [rich]
        # Order-independent: swapping which stream carries the richer copy
        # must not change the outcome.
        out2 = dedupe_posts_by_shortcode({STREAM_POSTS: [rich], STREAM_REELS: [thin]})
        assert out2 == [rich]

    def test_differing_copies_prefer_the_richer_payload_by_caption_length(self):
        thin = {"shortcode": "a", "caption": "hi", "image_urls": ["u1"]}
        rich = {"shortcode": "a", "caption": "a much longer caption body", "image_urls": ["u1"]}
        out = dedupe_posts_by_shortcode({STREAM_POSTS: [thin], STREAM_REELS: [rich]})
        assert out == [rich]

    def test_a_richness_tie_keeps_the_posts_stream_copy_regardless_of_dict_order(self):
        posts_copy = {"shortcode": "a", "caption": "x", "image_urls": ["u1"], "_from": "posts"}
        reels_copy = {"shortcode": "a", "caption": "x", "image_urls": ["u1"], "_from": "reels"}
        out = dedupe_posts_by_shortcode({STREAM_REELS: [reels_copy], STREAM_POSTS: [posts_copy]})
        assert out == [posts_copy]

    def test_result_order_is_independent_of_posts_by_stream_dict_key_order(self):
        posts = [{"shortcode": "a", "caption": "", "image_urls": []},
                 {"shortcode": "b", "caption": "", "image_urls": []}]
        reels = [{"shortcode": "c", "caption": "", "image_urls": []}]
        out_a = dedupe_posts_by_shortcode({STREAM_POSTS: posts, STREAM_REELS: reels})
        out_b = dedupe_posts_by_shortcode({STREAM_REELS: reels, STREAM_POSTS: posts})
        assert [p["shortcode"] for p in out_a] == [p["shortcode"] for p in out_b] == ["a", "b", "c"]

    def test_a_post_with_no_shortcode_is_never_dropped(self):
        no_code = {"shortcode": None, "caption": "x", "image_urls": ["u1"]}
        out = dedupe_posts_by_shortcode({STREAM_POSTS: [no_code]})
        assert out == [no_code]

    def test_empty_input_yields_empty_output(self):
        assert dedupe_posts_by_shortcode({}) == []


# ── §C single-venue path stays byte-for-byte identical ─────────────────────
class TestSingleVenuePathUnchanged:
    """The common case (a handle mapping to exactly one venue) must behave
    EXACTLY as it did before §C's shared-handle attribution branch existed —
    the likeliest casualty of that change. Proven directly against
    `chain_venue`, the same entry point the pre-existing classification/
    archiving tests above already exercise, now additionally passing the
    NEW `venue_dao` kwarg (as the real caller, `ScheduledInstagramCrawlService.
    _chain`, now always does) to prove it is inert for `len(venue_ids) == 1`."""

    def test_a_single_venue_id_never_enters_the_shared_handle_branch(self):
        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="soloh", status="found"))

        media_store = _FakeVenueMediaStore()
        classifier = _FakePhotoClassifier(category="flyer", confidence=0.92)
        openai_client = _FakePromoterOpenAIClient(
            '{"title": "Solo Event", "description": null, "date_text": null, '
            '"time_text": null, "is_recurring": false, "recurrence_text": null, '
            '"lineup": [], "ticket_url": null, "price_text": null, '
            '"location_text": null, "confidence": 0.9}'
        )
        extraction_service = _venue_extraction_service(dao, media_store, openai_client)
        chainer = InstagramCrawlChainer(
            media_store=media_store, downloader=_FakeVenueDownloader(),
            event_extraction_service=extraction_service, photo_classifier=classifier,
        )
        post = {
            "shortcode": "solopost1", "caption": "Festa hoje! Ingressos abertos.",
            "permalink": "https://instagram.com/p/solopost1", "timestamp": "2026-08-05T20:00:00.000Z",
            "image_urls": ["https://cdn.example.com/solopost1.jpg"], "is_pinned": False,
        }

        # venue_dao IS supplied (as the real caller always does now) but
        # must never be consulted: with one venue_id, resolution never runs.
        report = _run(chainer.chain_venue(
            handle="soloh", venue_ids=["v1"], new_posts=[post], now=NOW,
            classify_images=True, venue_dao=dao,
        ))

        assert report.archived == 1
        assert classifier.calls == 1
        assert openai_client.calls == 1
        row = dao.get_event_by_source("soloh", "solopost1")
        assert row is not None and row["venue_id"] == "v1" and row["title"] == "Solo Event"

    def test_venue_dao_omitted_behaves_identically_for_a_single_venue(self):
        """The pre-existing call shape (no `venue_dao` at all) — every test
        above this class already proves this keeps working; this test
        exists only to name the guarantee explicitly, per the plan's own
        test-plan bullet."""
        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V1", venue_lat=-8.0, venue_lng=-34.9))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="soloh2", status="found"))
        media_store = _FakeVenueMediaStore()
        chainer = InstagramCrawlChainer(
            media_store=media_store, downloader=_FakeVenueDownloader(),
            event_extraction_service=None, photo_classifier=None,
        )
        post = {
            "shortcode": "solopost2", "caption": "no marker here",
            "permalink": "https://instagram.com/p/solopost2", "timestamp": "2026-08-05T20:00:00.000Z",
            "image_urls": ["https://cdn.example.com/solopost2.jpg"], "is_pinned": False,
        }
        report = _run(chainer.chain_venue(handle="soloh2", venue_ids=["v1"], new_posts=[post], now=NOW))
        assert report.archived == 1


class TestSharedHandleMetricsParity:
    """plans/260810_date-correctness-review-reasons-and-path-parity.md §D:
    `_chain_shared_handle` must (1) stamp a venue's own post `venue_post`,
    never `promoter_post`, (2) bump `crawl_venue_attribution_total` per
    post, and (3) refresh `EVENTS_TOTAL` itself, since it never calls
    `EventExtractionService.run()` (the only other place that gauge used to
    be set). Called directly against `_chain_shared_handle`, the real entry
    point `chain_venue` reaches for `len(venue_ids) > 1` — the same pattern
    TestSingleVenuePathUnchanged above uses for the single-venue branch."""

    def _two_venue_dao(self):
        dao = _venue_dao()
        dao.upsert_venue(Venue(venue_id="va", venue_name="Venue A", venue_lat=-8.05, venue_lng=-34.88))
        dao.upsert_venue(Venue(venue_id="vb", venue_name="Venue B", venue_lat=-8.06, venue_lng=-34.90))
        dao.set_venue_instagram(VenueInstagram(venue_id="va", instagram_handle="shared1", status="found"))
        dao.set_venue_instagram(VenueInstagram(venue_id="vb", instagram_handle="shared1", status="found"))
        return dao

    def _chainer(self, dao, openai_client):
        from app.services.promoter_crawl_service import PromoterCrawlService

        promoter_service = PromoterCrawlService(
            venue_dao=dao, posts_client=None, media_store=_FakePromoterMediaStore(),
            downloader=_FakePromoterDownloader(), openai_client=openai_client, min_confidence=0.0,
        )
        return InstagramCrawlChainer(
            media_store=None, downloader=None, event_extraction_service=None,
            promoter_crawl_service=promoter_service,
        )

    def test_source_kind_is_stamped_venue_post_not_promoter_post(self):
        dao = self._two_venue_dao()
        openai_client = _FakePromoterOpenAIClient(
            '{"title": "Shared Post Event", "description": null, "date_text": null, '
            '"time_text": null, "is_recurring": false, "recurrence_text": null, '
            '"lineup": [], "ticket_url": null, "price_text": null, '
            '"location_text": null, "confidence": 0.9}'
        )
        chainer = self._chainer(dao, openai_client)
        post = {
            "shortcode": "sharedpost1", "caption": "Ingressos abertos! Vem pro role.",
            "permalink": "https://instagram.com/p/sharedpost1", "timestamp": "2026-08-05T20:00:00.000Z",
            "image_urls": ["https://cdn.example.com/sharedpost1.jpg"], "is_pinned": False,
        }

        _run(chainer._chain_shared_handle(
            handle="shared1", venue_ids=["va", "vb"], new_posts=[post], now=NOW,
            classify_images=False, venue_dao=dao,
        ))

        row = dao.get_event_by_source("shared1", "sharedpost1")
        assert row is not None
        assert row["source_kind"] == "venue_post", row

    def test_events_total_gauge_is_refreshed_without_calling_event_extraction_service(self):
        """This path never calls EventExtractionService.run() -- the only
        other code that used to refresh EVENTS_TOTAL. A handle that only
        ever goes through this branch must not leave the gauge permanently
        at zero (plan §D: "a permanently empty gauge is worse than no
        gauge")."""
        from prometheus_client import REGISTRY

        dao = self._two_venue_dao()
        openai_client = _FakePromoterOpenAIClient(
            '{"title": "Gauge Event", "description": null, "date_text": null, '
            '"time_text": null, "is_recurring": false, "recurrence_text": null, '
            '"lineup": [], "ticket_url": null, "price_text": null, '
            '"location_text": null, "confidence": 0.9}'
        )
        chainer = self._chainer(dao, openai_client)
        assert chainer.event_extraction_service is None  # proves the gauge cannot come from there
        post = {
            "shortcode": "gaugepost1", "caption": "Ingressos abertos! Vem pro role.",
            "permalink": "https://instagram.com/p/gaugepost1", "timestamp": "2026-08-05T20:00:00.000Z",
            "image_urls": ["https://cdn.example.com/gaugepost1.jpg"], "is_pinned": False,
        }

        _run(chainer._chain_shared_handle(
            handle="shared1", venue_ids=["va", "vb"], new_posts=[post], now=NOW,
            classify_images=False, venue_dao=dao,
        ))

        row = dao.get_event_by_source("shared1", "gaugepost1")
        gauge_value = REGISTRY.get_sample_value("events_total", {"status": row["status"]})
        assert gauge_value is not None and gauge_value >= 1, gauge_value

    def test_a_resolved_post_counts_one_resolved_attribution(self):
        from prometheus_client import REGISTRY

        dao = self._two_venue_dao()
        openai_client = _FakePromoterOpenAIClient(
            '{"title": "Venue A Event", "description": null, "date_text": null, '
            '"time_text": null, "is_recurring": false, "recurrence_text": null, '
            '"lineup": [], "ticket_url": null, "price_text": null, '
            '"location_text": "Venue A", "confidence": 0.9}'
        )
        chainer = self._chainer(dao, openai_client)
        post = {
            "shortcode": "resolvedpost1", "caption": "Ingressos abertos! Vem pro role.",
            "permalink": "https://instagram.com/p/resolvedpost1", "timestamp": "2026-08-05T20:00:00.000Z",
            "image_urls": ["https://cdn.example.com/resolvedpost1.jpg"], "is_pinned": False,
        }
        before = REGISTRY.get_sample_value("crawl_venue_attribution_total", {"outcome": "resolved"}) or 0.0

        _run(chainer._chain_shared_handle(
            handle="shared1", venue_ids=["va", "vb"], new_posts=[post], now=NOW,
            classify_images=False, venue_dao=dao,
        ))

        after = REGISTRY.get_sample_value("crawl_venue_attribution_total", {"outcome": "resolved"}) or 0.0
        assert after - before == 1.0, (before, after)
        row = dao.get_event_by_source("shared1", "resolvedpost1")
        assert row["venue_id"] == "va", row

    def test_an_unresolved_post_counts_one_ambiguous_attribution(self):
        from prometheus_client import REGISTRY

        dao = self._two_venue_dao()
        openai_client = _FakePromoterOpenAIClient(
            '{"title": "Neither Venue Event", "description": null, "date_text": null, '
            '"time_text": null, "is_recurring": false, "recurrence_text": null, '
            '"lineup": [], "ticket_url": null, "price_text": null, '
            '"location_text": null, "confidence": 0.9}'
        )
        chainer = self._chainer(dao, openai_client)
        post = {
            "shortcode": "ambiguouspost1", "caption": "Ingressos abertos! Vem pro role.",
            "permalink": "https://instagram.com/p/ambiguouspost1", "timestamp": "2026-08-05T20:00:00.000Z",
            "image_urls": ["https://cdn.example.com/ambiguouspost1.jpg"], "is_pinned": False,
        }
        before = REGISTRY.get_sample_value("crawl_venue_attribution_total", {"outcome": "ambiguous"}) or 0.0

        _run(chainer._chain_shared_handle(
            handle="shared1", venue_ids=["va", "vb"], new_posts=[post], now=NOW,
            classify_images=False, venue_dao=dao,
        ))

        after = REGISTRY.get_sample_value("crawl_venue_attribution_total", {"outcome": "ambiguous"}) or 0.0
        assert after - before == 1.0, (before, after)
        row = dao.get_event_by_source("shared1", "ambiguouspost1")
        assert row["venue_id"] is None, row
        assert "unresolved_venue" in (row["review_reason"] or ""), row
