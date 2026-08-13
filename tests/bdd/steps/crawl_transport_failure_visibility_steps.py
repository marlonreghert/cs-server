"""Behave steps for
tests/bdd/enrichment/crawl-transport-failure-visibility.feature.

See plans/260813_crawl-transport-failure-visibility.md.

Reuses the SAME context/fakes `scheduled_incremental_instagram_crawl_steps.py`
builds, and reuses (never re-registers) the Background/driving/assertion
steps `crawl_error_visibility_steps.py` already defined for the identical
literal text this feature's own scenarios share with 260812's — Behave
dispatches purely by (step-type, text), so "a crawl target ... that has never
been seeded", "the crawl runs for ...", "the posts stream outcome is
recorded as a failure", "the consecutive failure count for ... is N" (dual
given/then), "the posts cursor for ... is still unset", "an operator reads
the crawl targets from the admin API", "... reports its last failure kind",
and the two dataset-error-item regression steps are ALL satisfied without a
line of new code here — only genuinely NEW step text gets a new function.

## Simulating a transport failure at the FetchPostsResult boundary

`_FakeApifyClient.fetch_recent_posts` stands in for the WHOLE
`ApifyInstagramClient`, never the real httpx-based `_run_actor_sync` — the
same boundary crawl_error_visibility_steps.py already fakes at for Apify's
OWN error items. After plan §A, a transport failure (timeout/HTTP error/
connection error, caught inside `_run_actor_sync`) surfaces at this exact
boundary as a `FetchPostsResult` whose `.error_code` is one of `timeout`/
`http_error`/`request_error` — indistinguishable, from `instagram_crawl_
service`'s point of view, from any other non-Apify error code. So these
"Given the Apify call ... times out / fails with an HTTP error / fails to
connect" steps reuse the EXISTING `program_error` fake method with the
plan's own transport codes rather than inventing a second fake method that
would do exactly the same thing under a different name.

This means the classification-level scenarios in this feature (timeout/
http/connection -> failure, the counter, the cursor, the failure kind, the
crawl-service log line) exercise `instagram_crawl_service._run_stream`'s
EXISTING generic "any non-Apify, non-None error_code is a failure" branch
(already shipped by 260812 — see that feature's "unrecognised Apify error
code" scenario) — they do not, and structurally cannot, drive the real
defect this plan fixes, because the fake never touches the real client's own
None-vs-[] collapse. That defect (the actual production bug — a timeout
returning `error_code=None`, read as a genuinely empty account) is pinned
directly against the real `ApifyInstagramClient` by
tests/test_apify_instagram_media.py's `TestTransportFailureClassification`
(pytest, not BDD — mirrors TestErrorItemClassification's existing precedent
for testing the client's OWN translation logic outside Gherkin, and CLAUDE.md's
"do not require live ... network calls in BDD"). See the PR notes for the
honest before/after read on which of THIS file's scenarios were ever
expected to go red.

## Log capture

Reuses `crawl_error_visibility_steps.step_when_the_crawl_runs_for`'s root-
logger capture (`context.ic_captured_log_messages`) — no new capture
mechanism needed.
"""
from __future__ import annotations

from behave import given, then  # type: ignore[import-untyped]

from tests.bdd.steps.scheduled_incremental_instagram_crawl_steps import _ensure_context


# ── transport failures, simulated at the FetchPostsResult boundary ─────────
@given('the Apify call for "{handle}" posts times out')
def step_given_apify_posts_times_out(context, handle):
    _ensure_context(context)
    context.ic_apify.program_error(handle, "posts", code="timeout", request_error_count=0)


@given('the Apify call for "{handle}" posts fails with an HTTP error')
def step_given_apify_posts_http_error(context, handle):
    _ensure_context(context)
    context.ic_apify.program_error(handle, "posts", code="http_error", request_error_count=0)


@given('the Apify call for "{handle}" posts fails to connect')
def step_given_apify_posts_connection_error(context, handle):
    _ensure_context(context)
    context.ic_apify.program_error(handle, "posts", code="request_error", request_error_count=0)


# ── a genuine empty is still a genuine empty ────────────────────────────────
@given('Apify returns no posts and no error item for "{handle}"')
def step_given_apify_returns_nothing(context, handle):
    _ensure_context(context)
    # Explicit, even though an unprogrammed (handle, "posts") key already
    # defaults to this in the fake -- states the arrangement rather than
    # leaning on the fake's own default silently.
    context.ic_apify.program_posts(handle, "posts", [])


@then("the posts stream outcome is recorded as empty")
def step_then_posts_outcome_empty(context):
    assert context.ic_last_report["streams"]["posts"]["outcome"] == "empty", context.ic_last_report


# ── the log must name the handle and the stream ─────────────────────────────
@then('the timeout is logged naming the handle "{handle}"')
def step_then_timeout_logged_naming_handle(context, handle):
    messages = " | ".join(context.ic_captured_log_messages)
    assert handle in messages, messages


@then('the timeout is logged naming the stream "{stream}"')
def step_then_timeout_logged_naming_stream(context, stream):
    messages = " | ".join(context.ic_captured_log_messages)
    assert stream in messages, messages


# ── a stream that has never worked is louder than one that failed today ────
@given('"{handle}" has run before and its posts cursor is still unset')
def step_given_has_run_before_cursor_unset(context, handle):
    _ensure_context(context)
    row = context.ic_dao.update_crawl_target(handle, {"last_run_at": context.ic_now})
    assert row["cursor_posts_at"] is None, row


@given('"{handle}" has never run')
def step_given_has_never_run(context, handle):
    _ensure_context(context)
    # No-op arrange: `_create_target` (this feature's own Background) never
    # sets `last_run_at`, so a freshly created target already satisfies
    # "never run". Asserted explicitly so the scenario reads as a
    # deliberate arrange, not an accidental pass riding on the Background.
    row = context.ic_dao.get_crawl_target(handle)
    assert row["last_run_at"] is None, row


@then('"{handle}" reports that its posts stream has never seeded')
def step_then_reports_never_seeded(context, handle):
    target = context.ic_admin_targets[handle]
    assert target.posts_never_seeded is True, target


@then('"{handle}" does not report that its posts stream has never seeded')
def step_then_does_not_report_never_seeded(context, handle):
    target = context.ic_admin_targets[handle]
    assert target.posts_never_seeded is False, target
