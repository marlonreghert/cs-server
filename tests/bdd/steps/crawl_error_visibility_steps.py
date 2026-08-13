"""Behave steps for tests/bdd/enrichment/crawl-error-visibility.feature.

Shares the SAME context/fakes `scheduled_incremental_instagram_crawl_steps.py`
already built (imported, never duplicated) — the `_FakeApifyClient.
program_error`/`_FakeOpenAIClient.program_multi` extensions that feature
needed live THERE (a shared fake genuinely used by more than one feature),
mirroring reels_on_seed_only_steps.py's existing precedent for reusing this
module's context rather than building a second one.

See plans/260812_crawl-error-visibility.md.

## The Background step also resets context

This feature's own Background ("Given a crawl target ... that has never been
seeded") is the FIRST step of every scenario here, so it — not `scheduled_
incremental_instagram_crawl_steps.step_given_scheduler_configured` — is what
calls `_reset_context`, giving every scenario a fresh fake Apify/OpenAI/media
store/RDS fake.

## The dual-purpose "consecutive failure count is N" step

Two scenarios use the IDENTICAL text "the consecutive failure count for X is
N" for BOTH the arrange step (Given, before the crawl runs) and the assert
step (Then, after it). Behave dispatches purely by TEXT, not by Given/When/
Then, so this can only be ONE step function — it is written to act as
"arrange" before the crawl has run this scenario (`context.ic_last_report is
None`) and "assert" after (`context.ic_last_report is not None`).

## Log capture

"the run logs the Apify error code and the blocked-request count" and "a
warning is logged naming the handle" need to inspect what was actually
logged, not just the returned report — `step_when_the_crawl_runs_for`
attaches a handler to the ROOT logger for the duration of the call (both
`app.api.apify_instagram_client`, which logs the raw Apify fields, and
`app.services.instagram_crawl_service`, which logs the decision to disable a
handle-not-found target, propagate to it by default) and stores every
WARNING-or-above message on the context.
"""
from __future__ import annotations

import json
import logging

from behave import given, then, when  # type: ignore[import-untyped]

import sys

# `app.routers` package's own __init__.py does `from app.routers.
# admin_crawl_router import router as admin_crawl_router` — that binds the
# NAME `admin_crawl_router` on the `app.routers` package to the APIRouter
# INSTANCE, not the module, and that binding wins over the parent package's
# own automatic submodule attribute (Python sets it once, then __init__.py's
# own top-level assignment overwrites it). `import app.routers.
# admin_crawl_router as admin_crawl_router` resolves through that same
# shadowed attribute and silently hands back the router instance, not the
# module — verified directly against this environment. Going through
# sys.modules after the import sidesteps the package's own attribute
# entirely and gets the real module every time.
import app.routers.admin_crawl_router  # noqa: F401 -- populates sys.modules

admin_crawl_router = sys.modules["app.routers.admin_crawl_router"]
from app.services.event_extraction_service import EventExtractionService, EventPostSource
from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS
from tests.bdd.steps.scheduled_incremental_instagram_crawl_steps import (
    _create_target,
    _ensure_context,
    _extraction_json,
    _post,
    _reset_context,
    _run,
)


# ── log capture (see module docstring) ───────────────────────────────────────
class _ListLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _many_events_json(n: int) -> str:
    base = {
        "description": None, "time_text": None, "is_recurring": False,
        "recurrence_text": None, "lineup": [], "ticket_url": None,
        "price_text": None, "location_text": None, "confidence": 0.9,
    }
    events = [
        {**base, "title": f"Roteiro Evento {i + 1}", "date_text": f"{(i % 28) + 1:02d}/08"}
        for i in range(n)
    ]
    return json.dumps({"events": events})


# ── Background ────────────────────────────────────────────────────────────
@given('a crawl target "{handle}" that has never been seeded')
def step_given_crawl_target_never_seeded(context, handle):
    _reset_context(context)
    context.ic_handle = _create_target(context, handle)


# ── Driving the crawl, with log capture ─────────────────────────────────────
@when('the crawl runs for "{handle}"')
def step_when_the_crawl_runs_for(context, handle):
    root_logger = logging.getLogger()
    handler = _ListLogHandler()
    handler.setLevel(logging.WARNING)
    root_logger.addHandler(handler)
    try:
        context.ic_last_report = _run(context.ic_service.run_target(handle))
    finally:
        root_logger.removeHandler(handler)
    context.ic_captured_log_messages = [r.getMessage() for r in handler.records]


@when("the post is extracted by the crawl")
def step_when_the_post_is_extracted_by_the_crawl(context):
    """§D's two cap-truncation scenarios phrase the When around the POST,
    not the crawl, but the fetch -> archive -> extract chain this feature
    drives is the same one call as "the crawl runs" above. Worded "...by the
    crawl", not the plan's own bare "the post is extracted" — that exact
    phrase is ALREADY registered, against a DIFFERENT harness (`context.
    ee_*`), by event_ticket_info_and_attractions_steps.py; Behave's step
    registry is global and text-keyed, so a second identical registration
    raises AmbiguousStep. date_correctness_and_path_parity_steps.py hit this
    identical collision before and resolved it the same way — see that
    module's own docstring ("worded slightly off the feature file's most
    obvious phrasing specifically to avoid AmbiguousStep... against steps
    already registered for a DIFFERENT context under the identical literal
    text") — this follows that established precedent rather than inventing
    a second one. See the PR notes for this as a named deviation from the
    plan's own feature-file text."""
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


# ── §A/§B: the error item itself ─────────────────────────────────────────────
@given('Apify returns an error item for "{handle}" posts with code "{code}"')
def step_given_apify_error_item_posts(context, handle, code):
    _ensure_context(context)
    context.ic_error_handle = handle
    context.ic_error_stream = "posts"
    context.ic_error_code = code
    context.ic_apify.program_error(handle, "posts", code=code, request_error_count=0)


@given('Apify returns an error item for "{handle}" reels with code "{code}"')
def step_given_apify_error_item_reels(context, handle, code):
    _ensure_context(context)
    # The reels stream only runs at all when crawl_reels is on and this is
    # the target's one-time seed (app.services.instagram_crawl_service.
    # reels_skip_reason) — this scenario's whole point is what the REELS
    # stream reports, so it must actually fire.
    context.ic_dao.update_crawl_target(handle, {"crawl_reels": True})
    context.ic_error_handle = handle
    context.ic_error_stream = "reels"
    context.ic_error_code = code
    context.ic_apify.program_error(handle, "reels", code=code, request_error_count=0)


@given("that error item reports {n:d} blocked requests")
def step_given_error_item_reports_n_blocked(context, n):
    context.ic_apify.program_error(
        context.ic_error_handle, context.ic_error_stream,
        code=context.ic_error_code, request_error_count=n,
    )


@given("that error item reports no blocked requests")
def step_given_error_item_reports_no_blocked(context):
    context.ic_apify.program_error(
        context.ic_error_handle, context.ic_error_stream,
        code=context.ic_error_code, request_error_count=0,
    )


@then("the posts stream outcome is recorded as blocked")
def step_then_posts_outcome_blocked(context):
    assert context.ic_last_report["streams"]["posts"]["outcome"] == "blocked", context.ic_last_report


@then("the posts stream outcome is recorded as a handle-not-found failure")
def step_then_posts_outcome_handle_not_found(context):
    assert context.ic_last_report["streams"]["posts"]["outcome"] == "handle_not_found", (
        context.ic_last_report
    )


@then("the posts stream outcome is recorded as a failure")
def step_then_posts_outcome_failed(context):
    assert context.ic_last_report["streams"]["posts"]["outcome"] == "failed", context.ic_last_report


@then("the outcome is not recorded as empty")
def step_then_outcome_not_empty(context):
    assert context.ic_last_report["streams"]["posts"]["outcome"] != "empty", context.ic_last_report


@then("the reels stream outcome is recorded as empty")
def step_then_reels_outcome_empty(context):
    assert context.ic_last_report["streams"]["reels"]["outcome"] == "empty", context.ic_last_report


@then("the run logs the Apify error code and the blocked-request count")
def step_then_run_logs_error_code_and_count(context):
    messages = " | ".join(context.ic_captured_log_messages)
    assert "no_items" in messages, messages
    assert "11" in messages, messages


@then("a warning is logged naming the handle")
def step_then_warning_logged_naming_handle(context):
    messages = " | ".join(context.ic_captured_log_messages)
    assert context.ic_handle in messages, messages


@then("the target records why it will no longer be retried on a schedule")
def step_then_target_records_why_no_longer_retried(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["enabled"] is False, row
    assert row["last_failure_kind"] == "handle_not_found", row


# ── §B: the failure counter ──────────────────────────────────────────────────
@given('the consecutive failure count for "{handle}" is {n:d}')
@then('the consecutive failure count for "{handle}" is {n:d}')
def step_consecutive_failure_count_is(context, handle, n):
    """Dual-purpose — see the module docstring. Registered under BOTH
    @given and @then: behave's step registry is keyed by (type, text), not
    text alone, so a step used as a literal `Given` in one place and a
    literal `Then` in another needs both decorators on the SAME function,
    not just text reuse. Before the crawl has run this scenario, this
    ARRANGES the starting count; after, it ASSERTS the count the run left
    behind."""
    if context.ic_last_report is None:
        context.ic_dao.update_crawl_target(handle, {"consecutive_failures": n})
    else:
        row = context.ic_dao.get_crawl_target(handle)
        assert row["consecutive_failures"] == n, row


@given('Apify returns {n:d} posts for "{handle}"')
def step_given_apify_returns_n_posts(context, n, handle):
    _ensure_context(context)
    posts = [
        _post(f"reset{i}", f"2026-08-0{(i % 7) + 1}T10:00:00.000Z",
              image_urls=[f"https://cdn.example.com/reset{i}.jpg"])
        for i in range(n)
    ]
    context.ic_apify.program_posts(handle, "posts", posts)


# ── §B: billing ───────────────────────────────────────────────────────────
@given("the monthly result budget has {n:d} results remaining")
def step_given_budget_has_n_remaining(context, n):
    _ensure_context(context)
    spent = context.ic_config.monthly_result_budget - n
    year_month = context.ic_budget.current_year_month_utc(context.ic_now)
    if spent > 0:
        context.ic_budget.increment_month(year_month, spent)


@then("the monthly result budget still has {n:d} results remaining")
def step_then_budget_still_has_n_remaining(context, n):
    year_month = context.ic_budget.current_year_month_utc(context.ic_now)
    spent = context.ic_budget.get_month_count(year_month)
    remaining = context.ic_config.monthly_result_budget - spent
    assert remaining == n, remaining


@then("the target's last run records {n:d} results")
def step_then_last_run_records_n_results(context, n):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["last_run_results"] == n, row


@then("the target's last run cost is {n:d}")
def step_then_last_run_cost_is_n(context, n):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["last_run_cost_usd"] == n, row


@given('Apify returns {n:d} posts and {m:d} error item for "{handle}" posts')
def step_given_apify_mixed_posts_and_error(context, n, m, handle):
    _ensure_context(context)
    posts = [
        _post(f"mix{i}", f"2026-08-0{(i % 7) + 1}T10:00:00.000Z",
              image_urls=[f"https://cdn.example.com/mix{i}.jpg"])
        for i in range(n)
    ]
    context.ic_apify.program_posts(handle, "posts", posts)
    context.ic_apify.program_error(handle, "posts", code="no_items", request_error_count=0)


@then('{n:d} posts are archived for "{handle}"')
def step_then_n_posts_archived(context, n, handle):
    assert len(context.ic_media_store.images) == n, context.ic_media_store.images


@then('the posts cursor for "{handle}" is still unset')
def step_then_posts_cursor_still_unset(context, handle):
    row = context.ic_dao.get_crawl_target(handle)
    assert row["cursor_posts_at"] is None, row


# ── §B: the admin read model ──────────────────────────────────────────────
@when("an operator reads the crawl targets from the admin API")
def step_when_operator_reads_crawl_targets(context):
    class _Container:
        pass

    container = _Container()
    container.pipeline_repository = context.ic_dao
    admin_crawl_router.set_container(container)
    # `list_crawl_targets` is called here as a PLAIN Python function, not
    # through FastAPI's ASGI pipeline — response_model validation/
    # serialization (dict -> CrawlTargetOut) only happens on that pipeline,
    # so the route function itself returns plain dicts (see `_to_out`'s own
    # return type). Wrapped here to exercise the SAME model the real HTTP
    # response would validate against, not just its raw dict shape.
    targets = [
        admin_crawl_router.CrawlTargetOut(**row)
        for row in admin_crawl_router.list_crawl_targets()
    ]
    context.ic_admin_targets = {t.handle: t for t in targets}


@then('"{handle}" reports its last failure kind')
def step_then_reports_last_failure_kind(context, handle):
    """Asserts only that SOME failure kind was recorded — not which one.
    plans/260813_crawl-transport-failure-visibility.md reuses this IDENTICAL
    step text for a scenario whose failure kind is `failed` (a timeout), not
    `handle_not_found`; Behave dispatches by literal text, so a single
    function must serve both. The `not_found` scenario THIS feature file
    covers already pins the specific value via the separate "the posts
    stream outcome is recorded as a handle-not-found failure" step above."""
    target = context.ic_admin_targets[handle]
    assert target.last_failure_kind is not None, target


@then('"{handle}" reports when that failure was observed')
def step_then_reports_last_failure_at(context, handle):
    target = context.ic_admin_targets[handle]
    assert target.last_failure_at is not None, target


# ── §C: media type persistence ───────────────────────────────────────────
@given('Apify returns 1 post for "{handle}" whose media type is "{media_type}"')
def step_given_apify_one_post_with_media_type(context, handle, media_type):
    _ensure_context(context)
    context.ic_media_type_shortcode = "mtpost1"
    post = _post(
        context.ic_media_type_shortcode, "2026-08-05T20:00:00.000Z",
        caption="Festa hoje! Ingressos abertos, vem cedo.",
        image_urls=["https://cdn.example.com/mtpost1.jpg"],
    )
    post["post_type"] = media_type
    context.ic_apify.program_posts(handle, "posts", [post])
    context.ic_openai.program(_extraction_json())


@when("an event is extracted from that post")
def step_when_an_event_is_extracted_from_that_post(context):
    assert context.ic_openai.calls >= 1, (
        "expected extraction to have run as part of the crawl's chaining"
    )


@then('the stored source records its media type as "{media_type}"')
def step_then_stored_source_media_type(context, media_type):
    rows = context.ic_dao.list_events_by_source(context.ic_handle, context.ic_media_type_shortcode)
    assert rows, "expected at least one persisted source for this post"
    assert rows[0]["source_media_type"] == media_type, rows


# ── §D: cap truncation ────────────────────────────────────────────────────
@given("the per-post event cap is {n:d}")
def step_given_per_post_event_cap_is_n(context, n):
    _ensure_context(context)
    context.ic_extraction = EventExtractionService(
        venue_dao=context.ic_dao,
        post_source=EventPostSource(
            media_store=context.ic_media_store, archive_source=SOURCE_INSTAGRAM_POSTS,
        ),
        openai_client=context.ic_openai,
        min_confidence=0.0,
        max_events_per_post=n,
        now_provider=lambda: context.ic_now,
    )
    context.ic_chainer.event_extraction_service = context.ic_extraction


@given('a post for "{handle}" from which the model returns {n:d} events')
def step_given_post_model_returns_n_events(context, handle, n):
    context.ic_cap_shortcode = "cappedpost1"
    post = _post(
        context.ic_cap_shortcode, "2026-08-05T20:00:00.000Z",
        caption="Roteiro da semana! Confira todos os eventos, ingressos abertos.",
        image_urls=["https://cdn.example.com/cappedpost1.jpg"],
    )
    context.ic_apify.program_posts(handle, "posts", [post])
    context.ic_openai.program_multi(_many_events_json(n))


@then("{n:d} events are stored for that post")
def step_then_n_events_stored_for_post(context, n):
    rows = context.ic_dao.list_events_by_source(context.ic_handle, context.ic_cap_shortcode)
    assert len(rows) == n, rows


@then("the stored sources record that the post was truncated by the cap")
def step_then_sources_record_truncated(context):
    rows = context.ic_dao.list_events_by_source(context.ic_handle, context.ic_cap_shortcode)
    assert rows, "expected at least one persisted source for this post"
    assert all(r.get("source_events_truncated") is True for r in rows), rows


@then("the stored sources do not record a cap truncation")
def step_then_sources_do_not_record_truncated(context):
    rows = context.ic_dao.list_events_by_source(context.ic_handle, context.ic_cap_shortcode)
    assert rows, "expected at least one persisted source for this post"
    assert all(not r.get("source_events_truncated") for r in rows), rows
