"""Behave steps for tests/bdd/enrichment/skip-already-extracted-posts.feature.

plans/260826_skip-already-extracted-posts.md: proves `EventExtractionService.
run()`'s non-handles branch (`event_candidates`/`venue_ids`) skips the OpenAI
call for a post it has already successfully turned into an event -- BEFORE
`max_posts_per_venue` truncates the post list -- while a post whose every
prior attempt failed is still retried, a brand-new post is unaffected, the
deliberate `mode="handles"` re-extraction path keeps calling the model
unconditionally, and a skipped post's menu-item freshness (`last_seen_at`)
still advances.

Drives the REAL `EventExtractionService` over the SAME `context.ee_*` harness
`instagram_event_extraction_steps.py` builds. Reused UNMODIFIED (never
redefined): the Background steps, `_add_post`, `_extraction_json`,
`_run_extraction`, and "a post already extracted into an event" (that file's
own Given -- used here for the two scenarios that need a post which genuinely
went through a real first extraction, rather than a hand-seeded row) plus
"extraction runs for that handle" (`extract_by_handle_steps.py`) and "the
per-venue post cap is {n:d}"/"{n:d} posts are reported as qualifying"
(`extraction_newest_posts_first_steps.py`) and "a stored menu item last seen
months ago"'s sibling seeding helper `_seed_menu_item`
(`menu_item_lifecycle_steps.py`) -- the same cross-file reuse convention
every sibling extraction feature file in this suite already follows.

`_seed_already_extracted_post` mirrors `event_ticket_info_and_attractions_
steps.py`'s own `_seed_existing_event`: seed the "already extracted" row
DIRECTLY through the DAO (no model call spent setting up the fixture), then
also archive the SAME shortcode again via `_add_post` -- exactly what a real
crawl does every run within the lookback window, whether or not anything new
happened to the post.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then  # type: ignore[import-untyped]

from app.services.event_reconciliation import new_event_id
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _extraction_json,
    _run_extraction,
)
from tests.bdd.steps.menu_item_lifecycle_steps import _seed_menu_item

_TS = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)


def _seed_already_extracted_post(context, shortcode: str, *, timestamp: datetime) -> str:
    event_id = new_event_id()
    context.ee_dao.insert_event({
        "event_id": event_id, "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": shortcode, "source_event_key": f"{shortcode}_key",
        "status": "accepted", "title": f"Evento {shortcode}",
        "starts_at": timestamp, "raw_extraction": {"time_known": True},
    })
    _add_post(context, shortcode, timestamp=timestamp)
    return event_id


# ── Scenario: already-extracted post is not re-sent ──────────────────────────
@then("the model is not called a second time for that post")
def step_then_model_not_called_second_time(context):
    # "a post already extracted into an event" (the Given this pairs with)
    # spends exactly one real model call setting the fixture up; a correctly
    # skipping run must add none on top of it.
    assert context.ee_openai.calls == 1, context.ee_openai.calls


# ── Scenario: a failed extraction is retried, never skipped forever ─────────
@given("a post whose only prior extraction attempt failed")
def step_given_post_whose_only_prior_attempt_failed(context):
    _add_post(context, "sae_retry", timestamp=_TS)
    context.ee_openai.program("not valid json at all {{{")
    _run_extraction(context)
    row = context.ee_dao.get_event_by_source(context.ee_handle, "sae_retry")
    assert row is not None and row["status"] == "extraction_failed", row


@given("the model now returns a valid response for it")
def step_given_model_now_returns_valid_response(context):
    # An explicit resolvable date/time -- the default `_extraction_json()`
    # payload has `date_text=None`, which resolves to "no_date", not
    # "accepted"; this scenario is about the retry happening at all, not
    # date resolution, so a clean, auto-acceptable answer isolates that.
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h"))


# ── Scenario: the cap is spent on unprocessed posts, not skipped ones ───────
@given("two already-extracted posts newer than a new, unprocessed post")
def step_given_two_already_extracted_posts_newer_than_a_new_post(context):
    # Deliberately NEWER than the not-yet-extracted post below: under the
    # pre-fix bug, `_sort_newest_first` + `posts[:cap]` would keep these two
    # (the newest) and starve the genuinely new post out of the cap entirely
    # -- the exact regression this scenario guards against.
    _seed_already_extracted_post(
        context, "sae_cap_seen_a",
        timestamp=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )
    _seed_already_extracted_post(
        context, "sae_cap_seen_b",
        timestamp=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
    )
    _add_post(
        context, "sae_cap_new",
        timestamp=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    context.ee_openai.program(_extraction_json())


@then("the new post is extracted")
def step_then_the_new_post_is_extracted(context):
    row = context.ee_dao.get_event_by_source(context.ee_handle, "sae_cap_new")
    assert row is not None, (
        "expected sae_cap_new to be extracted -- the cap starved the new "
        "post instead of the already-extracted ones"
    )


# ── Scenario: a first-ever run is unaffected ─────────────────────────────────
@given("a post that has never been extracted before")
def step_given_a_post_never_extracted_before(context):
    _add_post(context, "sae_first_run", timestamp=_TS)
    context.ee_openai.program(_extraction_json())


@then('no post is counted with the outcome "{outcome}"')
def step_then_no_post_is_counted_with_the_outcome(context, outcome):
    assert context.ee_result["outcomes"].get(outcome, 0) == 0, context.ee_result["outcomes"]


# ── Scenario: deliberate re-extraction is unaffected by the skip ────────────
@given("the model returns a fresh response for it")
def step_given_model_returns_a_fresh_response_for_it(context):
    context.ee_openai.program(_extraction_json(title="Evento Reextraido"))


@then("the model is called again for that post")
def step_then_the_model_is_called_again_for_that_post(context):
    assert context.ee_openai.calls == 2, context.ee_openai.calls


# ── Scenario: a skipped post still keeps menu freshness current (§5b) ───────
@given("a stored menu item last seen months ago, from a post seen again this run")
def step_given_stored_menu_item_seen_again_this_run(context):
    # Sets context.ml_dish_event_id, which the reused Then step below reads.
    context.ml_dish_event_id = _seed_menu_item(context, "sae_menu_dish", days_ago=90)
    _add_post(context, "sae_menu_dish")
