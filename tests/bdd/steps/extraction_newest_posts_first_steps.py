"""Behave steps for
tests/bdd/enrichment/extraction-newest-posts-first.feature.

plans/260826_extraction-newest-posts-first.md: proves `run()`'s
`event_candidates`/`venue_ids` branch AND `_run_handles`' `mode="handles"`
branch both spend `max_posts_per_venue` on the NEWEST archived posts, not the
oldest -- regardless of the order `EventPostSource` happens to return them in
(manifest-insertion order, not a recency order).

Drives the REAL `EventExtractionService` over the SAME `context.ee_*` harness
`instagram_event_extraction_steps.py` already builds (`_add_post`/
`_add_handle_post`/`_extraction_json`/`_run_extraction`, and the Background
steps registered there) -- the same reuse pattern
`extract_by_handle_steps.py` already follows, rather than forking a second
harness. The one `@when` this file's handles-mode scenario needs
("extraction runs for that handle") already exists in
`extract_by_handle_steps.py` and is reused unmodified.

Fixture posts are inserted deliberately OUT of chronological order (middle,
then oldest, then newest) so a fix that merely reverses the archive's own
list order -- rather than sorting on each post's own timestamp -- would fail
these scenarios.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_handle_post,
    _add_post,
    _extraction_json,
)

_OLDEST_TS = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
_MIDDLE_TS = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
_NEWEST_TS = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


# ── Given ─────────────────────────────────────────────────────────────────────
@given(
    "three qualifying posts archived out of chronological order: an oldest, "
    "a middle, and a newest"
)
def step_given_three_qualifying_posts_out_of_order(context):
    # Insertion order is deliberately scrambled (middle, oldest, newest) --
    # neither the archive's own order nor a plain reversal of it matches the
    # true newest-first order (newest, middle, oldest), so only an explicit
    # sort on each post's own timestamp can pass this fixture.
    _add_post(context, "post_middle", timestamp=_MIDDLE_TS)
    _add_post(context, "post_oldest", timestamp=_OLDEST_TS)
    _add_post(context, "post_newest", timestamp=_NEWEST_TS)
    for _ in range(3):
        context.ee_openai.program(_extraction_json())


@given(
    "a shared handle with three qualifying posts archived out of "
    "chronological order: an oldest, a middle, and a newest"
)
def step_given_handle_three_qualifying_posts_out_of_order(context):
    _add_handle_post(context, "post_middle", timestamp=_MIDDLE_TS)
    _add_handle_post(context, "post_oldest", timestamp=_OLDEST_TS)
    _add_handle_post(context, "post_newest", timestamp=_NEWEST_TS)
    for _ in range(3):
        context.ee_openai.program(_extraction_json())


@given("the per-venue post cap is {n:d}")
def step_given_the_per_venue_post_cap_is(context, n):
    context.ee_run_config["max_posts_per_venue"] = n


@given(
    "a qualifying post with no usable timestamp alongside two qualifying "
    "dated posts"
)
def step_given_undated_post_alongside_two_dated_posts(context):
    _add_post(context, "post_undated", timestamp=None)
    _add_post(context, "post_dated_a", timestamp=_OLDEST_TS)
    _add_post(context, "post_dated_b", timestamp=_NEWEST_TS)
    for _ in range(3):
        context.ee_openai.program(_extraction_json())


# ── When ──────────────────────────────────────────────────────────────────────
# "event extraction runs" and "extraction runs for that handle" are already
# registered (instagram_event_extraction_steps.py and
# extract_by_handle_steps.py respectively) and reused unmodified.


# ── Then ──────────────────────────────────────────────────────────────────────
def _has_event(context, shortcode: str) -> bool:
    return context.ee_dao.get_event_by_source(context.ee_handle, shortcode) is not None


@then("the newest post is extracted")
def step_then_the_newest_post_is_extracted(context):
    assert _has_event(context, "post_newest"), "expected an event for post_newest"


@then("the middle post is extracted")
def step_then_the_middle_post_is_extracted(context):
    assert _has_event(context, "post_middle"), "expected an event for post_middle"


@then("the oldest post is extracted")
def step_then_the_oldest_post_is_extracted(context):
    assert _has_event(context, "post_oldest"), "expected an event for post_oldest"


@then("the oldest post is not extracted")
def step_then_the_oldest_post_is_not_extracted(context):
    assert not _has_event(context, "post_oldest"), (
        "post_oldest was extracted -- the cap kept the oldest post instead "
        "of the newest ones"
    )


@then("event extraction completes without error")
def step_then_event_extraction_completes_without_error(context):
    assert context.ee_result is not None
    assert "outcomes" in context.ee_result


@then("the post with no usable timestamp is extracted")
def step_then_the_undated_post_is_extracted(context):
    assert _has_event(context, "post_undated"), "expected an event for post_undated"


@then("the post with no usable timestamp is not extracted")
def step_then_the_undated_post_is_not_extracted(context):
    assert not _has_event(context, "post_undated"), (
        "post_undated was extracted ahead of a dated post -- an undated "
        "post must sort behind every dated post, never displace one"
    )


@then("both dated posts are extracted")
def step_then_both_dated_posts_are_extracted(context):
    assert _has_event(context, "post_dated_a"), "expected an event for post_dated_a"
    assert _has_event(context, "post_dated_b"), "expected an event for post_dated_b"
