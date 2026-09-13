"""Behave steps for
tests/bdd/persistence/events-venue-night-duplication-display-title.feature.

See plans/260912_events-venue-night-duplication.md §G. Runs on the SAME
`context.dedup_*` harness `event_dedup_fuzzy_title_steps.py` builds (so
"auto-merge is enabled", `"X" runs one night rather than a programme` and the
venue-night fixtures are reused verbatim), with a REAL
`RedisProjectionService` built over that same store — the plan's own reason
this feature lives in the persistence domain: "the chosen title reaches the
serving projection" can only be proved by driving the real projector, never
by hand-seeding a projection key.

The model is a programmable fake at the true boundary. CLAUDE.md's BDD policy
forbids a live OpenAI call in BDD, and the prompt's own load-bearing sentence
is pinned by a unit wiring guard instead (`tests/test_event_display_title.py`,
in the shape of `tests/test_event_extraction_prompt_kind.py`).
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from behave import given, then, when  # type: ignore[import-untyped]

from app.config import settings
from app.services.event_display_title import (
    ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY,
    EventDisplayTitleService,
)
from app.services.event_merge import merge_touched_events
from tests.bdd.steps import event_dedup_fuzzy_title_steps as _dedup_steps

_SATURDAY = "2026-09-12"


class _FakeTitleModel:
    """One programmed answer per call, in order. `.calls` is the
    cost-gate proof: it must stay empty for every group with an obvious
    canonical title, a single source, or an operator-edited title."""

    def __init__(self):
        self._answers: list = []
        self._cycle: list = []
        self.calls: list = []

    def program(self, answer) -> None:
        self._answers.append(answer)

    def program_cycle(self, *answers) -> None:
        """Answer DIFFERENTLY on every call — the fixture for "a model answer
        can never change which events survive"."""
        self._cycle = list(answers)

    async def pick_display_title(self, *, venue_name, local_date, source_titles, lineup):
        self.calls.append({
            "venue_name": venue_name, "local_date": local_date,
            "source_titles": list(source_titles), "lineup": list(lineup),
        })
        if self._cycle:
            return self._cycle[(len(self.calls) - 1) % len(self._cycle)]
        if not self._answers:
            raise AssertionError("the fake title model was called more times than programmed")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _ensure(context) -> None:
    _dedup_steps._ensure_context(context)
    if not hasattr(context, "dt_model"):
        context.dt_model = _FakeTitleModel()
        context.dt_event_ids = []
        context.dt_projector = None


def _projector(context):
    from app.services.redis_projection_service import RedisProjectionService

    if context.dt_projector is None:
        context.dt_projector = RedisProjectionService(
            redis_only_dao=context.redis_only_dao, rds_store=context.dedup_dao,
        )
    return context.dt_projector


def _service(context) -> EventDisplayTitleService:
    return EventDisplayTitleService(
        context.dedup_dao, context.dt_model, redis_client=context.dedup_redis,
    )


def _run_display_title_pass(context) -> None:
    asyncio.run(_service(context).run_for_events(_ids(context)))


def _run_merge(context) -> None:
    merge_touched_events(
        context.dedup_dao, _ids(context), _dedup_steps._NOW,
        redis_like=context.dedup_redis,
    )


def _ids(context) -> list:
    """Every row this scenario seeded — through this file's own fixtures or
    through the enrichment file's reused ones (`vnd_night_ids`). One
    accessor, so a Then never has to know which Given ran."""
    ids = list(context.dt_event_ids)
    for event_id in getattr(context, "vnd_night_ids", None) or []:
        if event_id not in ids:
            ids.append(event_id)
    return ids


def _survivors(context) -> list:
    return [eid for eid in _ids(context) if _dedup_steps._alive(context, eid)]


def _survivor(context) -> dict:
    survivors = _survivors(context)
    assert len(survivors) == 1, [
        context.dedup_dao.get_event(e)["title"] for e in survivors
    ]
    return context.dedup_dao.get_event(survivors[0])


# ── Background ────────────────────────────────────────────────────────────
@given("the events serving projection is enabled")
def step_given_projection_enabled(context):
    _ensure(context)
    if not hasattr(context, "_settings_overrides"):
        context._settings_overrides = {}
    for name, value in (("events_projection_enabled", True), ("events_projection_horizon_days", 21)):
        context._settings_overrides.setdefault(name, getattr(settings, name))
        setattr(settings, name, value)


@given('the venue catalog carries "{name}" as a servable venue')
def step_given_catalog_carries_servable_venue(context, name):
    _ensure(context)
    _dedup_steps._ensure_venue(context, name)
    context.dt_venue = name


@given("display-title selection is enabled")
def step_given_display_title_enabled(context):
    _ensure(context)
    context.dedup_redis.set(ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY, json.dumps(True))


@given("display-title selection is disabled")
def step_given_display_title_disabled(context):
    _ensure(context)
    context.dedup_redis.delete(ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY)


# ── fixtures ──────────────────────────────────────────────────────────────
def _seed(context, title, *, lineup=None, venue=None, display_title=None, source_title=None):
    """One row whose SOURCE carries the post's own extracted title — the
    display-title pass reads `raw_extraction["title"]` per source, never the
    single surviving `title`, which is the arbitrary pick §G exists to
    replace."""
    _ensure(context)
    event_id = _dedup_steps._seed_item(
        context, title, venue or context.dt_venue,
        starts_at=_dedup_steps._local_dt(_SATURDAY, "22:00"),
        lineup=list(lineup or []), first_seen_at=_dedup_steps._NOW,
        # `accepted`, not the seeder's `pending_review` default:
        # `is_selectable` only ever serves accepted/confirmed rows, and this
        # feature's whole point is what the PROJECTION does with the row.
        status="accepted",
    )
    update = {"raw_extraction": {"title": source_title or title}}
    if display_title is not None:
        update["display_title"] = display_title
    context.dedup_dao.update_event(event_id, update)
    context.dt_event_ids.append(event_id)
    return event_id


@given('stored events at "{venue}" on one Saturday titled "{a}" and "{b}"')
def step_given_two_titled_events(context, venue, a, b):
    _ensure(context)
    context.dt_venue = venue
    context.dt_stored_titles = [a, b]
    _seed(context, a, venue=venue)
    _seed(context, b, venue=venue)


_FIVE_ACTS = (
    ("ROWKA", ["ROWKA"]),
    ("VITINHO POLÊMICO", ["VITINHO POLÊMICO"]),
    ("SÁBADO VAI FERVER", ["MC Baixinho"]),
    ("SECRET CLUB com @neguindabasersv", ["@neguindabasersv"]),
    ("estreia de @neguindabasersv", ["@neguindabasersv"]),
)


# 'five stored events at "X" on one Saturday, each naming a different act' is
# NOT redefined here: it is already bound in
# `events_venue_night_duplication_steps.py`, against the SAME `dedup_*`
# harness this feature uses, so it is reused as-is and its ids are picked up
# through `_ids` below. The shorter wording this feature also uses delegates
# to it rather than seeding a second, subtly-different cluster.
@given('stored events at "{venue}" on one Saturday, each naming a different act')
def step_given_display_title_acts(context, venue):
    from tests.bdd.steps.events_venue_night_duplication_steps import (
        step_given_five_acts,
    )

    _ensure(context)
    context.dt_venue = venue
    step_given_five_acts(context, venue)


@given('a single-source stored event at "{venue}" on one Saturday')
def step_given_single_source_event(context, venue):
    _ensure(context)
    context.dt_venue = venue
    _seed(context, "ROWKA", venue=venue)


@given("an operator has edited the title of the surviving event")
def step_given_operator_edited_surviving_title(context):
    _run_merge(context)
    survivors = _survivors(context)
    assert len(survivors) == 1, survivors
    context.dt_operator_title = "Sábado no Club com ROWKA"
    context.dedup_dao.update_event(survivors[0], {
        "title": context.dt_operator_title, "operator_edited_fields": ["title"],
    })
    context.dt_merged = True


@given("the model answers with a title naming an act no post mentioned")
def step_given_model_invents_an_act(context):
    _ensure(context)
    context.dt_model.program(json.dumps({"display_title": "ROWKA e ANITTA"}))


@given("the model answers with a blank title")
def step_given_model_answers_blank(context):
    _ensure(context)
    context.dt_model.program(json.dumps({"display_title": "   "}))


@given("the model call fails")
def step_given_model_call_fails(context):
    _ensure(context)
    context.dt_model.program(RuntimeError("the title API is down"))


@given("the model answers with a different title on every call")
def step_given_model_answers_differently(context):
    _ensure(context)
    context.dt_model.program_cycle(
        json.dumps({"display_title": "ROWKA e VITINHO POLÊMICO"}),
        json.dumps({"display_title": "SÁBADO VAI FERVER com ROWKA"}),
    )


@given('a stored event at "{venue}" carrying a chosen display title')
def step_given_event_with_display_title(context, venue):
    _ensure(context)
    context.dt_venue = venue
    context.dt_chosen_title = "ROWKA e VITINHO POLÊMICO"
    _seed(context, "ROWKA", venue=venue, display_title=context.dt_chosen_title)


@given('a stored event at "{venue}" carrying no display title')
def step_given_event_without_display_title(context, venue):
    _ensure(context)
    context.dt_venue = venue
    _seed(context, "ROWKA", venue=venue)


# ── When ──────────────────────────────────────────────────────────────────
def _program_default_answer(context) -> None:
    """Every scenario that reaches a real call and has not programmed its own
    answer gets a valid one — built only from words the five posts contain,
    so it passes the validation gate."""
    if not context.dt_model._answers and not context.dt_model._cycle:
        context.dt_model.program(json.dumps({"display_title": "ROWKA e VITINHO POLÊMICO"}))
        context.dt_expected_title = "ROWKA e VITINHO POLÊMICO"


@when("the merge pass and the display-title pass run for that venue")
def step_when_merge_and_display_title_pass(context):
    _program_default_answer(context)
    if not getattr(context, "dt_merged", False):
        _run_merge(context)
    _run_display_title_pass(context)


@when("the display-title pass runs for that venue")
def step_when_display_title_pass(context):
    _run_display_title_pass(context)


@when("the merge pass and the display-title pass run twice for that venue")
def step_when_merge_and_display_title_twice(context):
    outcomes = []
    for _ in range(2):
        _run_merge(context)
        _run_display_title_pass(context)
        outcomes.append((
            tuple(sorted(_survivors(context))),
            tuple(sorted(
                eid for eid in _ids(context)
                if not _dedup_steps._alive(context, eid)
            )),
        ))
    context.dt_two_run_outcomes = outcomes


@when("the merge pass and the display-title pass run, and the display-title pass runs again")
def step_when_display_title_pass_twice(context):
    _program_default_answer(context)
    _run_merge(context)
    _run_display_title_pass(context)
    context.dt_title_after_first = _survivor(context).get("display_title")
    _run_display_title_pass(context)


# "the events projection runs" is already bound (events_serving_projection_
# steps.py, against the SHARED harness). Reworded in the feature file to
# "... over the stored events" — this repo's documented distinguishing-wording
# convention for a step text two features legitimately share.
@when("the events projection runs over the stored events")
def step_when_events_projection_runs(context):
    context.dt_summary = _projector(context).project_events(
        now=_dedup_steps._local_dt(_SATURDAY, "12:00"),
    )


@when("the merge pass, the display-title pass and the events projection run over the stored events")
def step_when_everything_runs(context):
    _program_default_answer(context)
    _run_merge(context)
    _run_display_title_pass(context)
    step_when_events_projection_runs(context)


@when("a later post about that same Saturday is absorbed into it")
def step_when_a_later_post_is_absorbed(context):
    _seed(context, "ROWKA ao vivo", venue=context.dt_venue)
    _run_merge(context)


# ── Then ──────────────────────────────────────────────────────────────────
@then('the display title is "{title}"')
def step_then_display_title_is(context, title):
    assert _survivor(context).get("display_title") == title, _survivor(context)


@then("the display title is one of the two stored titles")
def step_then_display_title_is_one_of_the_two(context):
    chosen = _survivor(context).get("display_title")
    assert chosen in context.dt_stored_titles, (chosen, context.dt_stored_titles)


# "no model call is made" is already bound (event_venue_targeting_steps.py);
# reworded in the feature file to "no TITLE model call is made", this repo's
# documented distinguishing-wording convention.
@then("no title model call is made")
def step_then_no_model_call(context):
    assert context.dt_model.calls == [], context.dt_model.calls


@then("exactly one model call is made")
def step_then_exactly_one_model_call(context):
    assert len(context.dt_model.calls) == 1, context.dt_model.calls


@then("only one model call is made in total")
def step_then_only_one_model_call_in_total(context):
    assert len(context.dt_model.calls) == 1, context.dt_model.calls


@then("the call is given all five source titles")
def step_then_call_given_five_titles(context):
    given_titles = set(context.dt_model.calls[0]["source_titles"])
    assert given_titles == {title for title, _acts in _FIVE_ACTS}, given_titles


@then("the display title is the title the model chose")
def step_then_display_title_is_the_model_s(context):
    assert _survivor(context).get("display_title") == context.dt_expected_title


@then("the event has no display title")
def step_then_event_has_no_display_title(context):
    for event_id in _survivors(context):
        assert context.dedup_dao.get_event(event_id).get("display_title") is None, event_id


@then("the surviving event has no display title")
def step_then_surviving_event_has_no_display_title(context):
    assert _survivor(context).get("display_title") is None, _survivor(context)


@then("the display title is the operator's title")
def step_then_display_title_is_the_operator_s(context):
    row = _survivor(context)
    # `display_title` stays NULL and the projection serves `display_title or
    # title` — so what a reader sees IS the operator's own title, which is
    # what this scenario is about. Never overwritten, never "improved".
    assert row.get("display_title") is None, row
    assert row["title"] == context.dt_operator_title, row["title"]


@then("a rejected display title is counted")
def step_then_rejected_counted(context):
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(
        "event_display_title_total", {"outcome": "rejected"},
    )
    assert value and value > 0, value


@then("the extraction run still completes")
def step_then_run_still_completes(context):
    # The pass caught the failure, counted it, and returned — there is a
    # survivor to read, and it kept its stored title.
    assert _survivor(context) is not None


@then("the same event survives both times")
def step_then_same_event_survives(context):
    first, second = context.dt_two_run_outcomes
    assert first[0] == second[0], (first[0], second[0])


@then("the same events are superseded both times")
def step_then_same_events_superseded(context):
    first, second = context.dt_two_run_outcomes
    assert first[1] == second[1], (first[1], second[1])


@then("the display title is unchanged")
def step_then_display_title_unchanged(context):
    assert _survivor(context).get("display_title") == context.dt_title_after_first


@then("the next display-title pass chooses a title again")
def step_then_next_pass_chooses_again(context):
    _program_default_answer(context)
    _run_display_title_pass(context)
    assert _survivor(context).get("display_title"), _survivor(context)


# ── what the app reads ────────────────────────────────────────────────────
def _projected(context) -> list:
    """Read back through the REAL DAO, by the real key format — never a
    hand-built key string, which is how a projection test quietly stops
    testing the projection."""
    from app.dao.redis_venue_dao import EVENT_OCCURRENCE_KEY_FORMAT

    prefix = EVENT_OCCURRENCE_KEY_FORMAT.format("")
    out = []
    for key in context.fake_redis.scan_iter(match=f"{prefix}*"):
        occurrence_id = key[len(prefix):]
        occurrence = context.redis_only_dao.get_event_occurrence(occurrence_id)
        if occurrence is not None:
            out.append(occurrence)
    return out


@then("the projected occurrence's title is the chosen display title")
def step_then_projected_title_is_display_title(context):
    titles = {occ.title for occ in _projected(context)}
    assert titles == {context.dt_chosen_title}, titles


@then("the projected occurrence's title is the stored title")
def step_then_projected_title_is_stored_title(context):
    titles = {occ.title for occ in _projected(context)}
    assert titles == {"ROWKA"}, titles


@then("the projected occurrence's title is the surviving event's stored title")
def step_then_projected_title_is_survivors_stored_title(context):
    step_when_events_projection_runs(context)
    stored = _survivor(context)["title"]
    titles = {occ.title for occ in _projected(context)}
    assert titles == {stored}, (titles, stored)


@then("one occurrence is projected for that Saturday")
def step_then_one_occurrence_projected(context):
    occurrences = _projected(context)
    assert len(occurrences) == 1, [o.title for o in occurrences]
    context.dt_occurrence = occurrences[0]


@then("the projected occurrence names every one of the five acts")
def step_then_occurrence_names_five_acts(context):
    lineup = set(context.dt_occurrence.lineup or [])
    for _title, acts in _FIVE_ACTS:
        for act in acts:
            assert act in lineup, (act, lineup)
