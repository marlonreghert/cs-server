"""Behave steps for tests/bdd/enrichment/dormant-vs-broken-targets.feature.

See plans/260813_dormant-vs-broken-targets.md.

Shares the SAME context/fakes `scheduled_incremental_instagram_crawl_steps.py`
already built (imported, never duplicated) — mirrors crawl_error_visibility_
steps.py's and crawl_transport_failure_visibility_steps.py's existing
precedent of reusing that module's Background/`_create_target`/`_ensure_
context` rather than building a second harness. Several step TEXTS this
feature file uses are ALSO already registered by those two sibling step
files under the identical literal wording (the Background, "the crawl runs
for X", "Apify returns an error item...", "the posts stream outcome is
recorded as blocked", "an operator reads the crawl targets from the admin
API", "Apify returns N posts for X", "X reports that its posts stream has
never seeded") — Behave's step registry is text-keyed and global, so this
file never redefines any of them, only what is genuinely new here.

## The admin-config seed lookback fixture

`ScheduledInstagramCrawlService` resolves the seed lookback (plans/260813_
dormant-vs-broken-targets.md §B) through an OPTIONAL `admin_config_service`
collaborator — the SAME "None means use the Settings-level default, degrade
gracefully" shape `ClosureDetectionService`/`EligibilityRuleService` already
use, not the raw-redis-plus-`load_*` shape `menu_lifecycle.py` uses (that
shape is for router-level readers; this is a SERVICE constructor dependency).
`_FakeAdminConfigService` here is the minimal `.get(key)`/`.set(key, value)`
double the real `AdminConfigService` exposes — good enough for a pure
resolution test, no real Redis/RDS involved.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then  # type: ignore[import-untyped]

from tests.bdd.steps.scheduled_incremental_instagram_crawl_steps import _ensure_context


class _FakeAdminConfigService:
    """Minimal `.get(key)`/`.set(key, value, updated_by=None)` double for the
    real `AdminConfigService` (app/services/admin_config_service.py) — bare
    keys, no `admin_config:` prefix (the real service adds that internally),
    already-decoded Python values (no JSON round-trip needed for a test
    double)."""

    def __init__(self):
        self._store: dict = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, updated_by=None):
        self._store[key] = value
        return value


def _admin_config(context) -> _FakeAdminConfigService:
    svc = getattr(context.ic_service, "admin_config_service", None)
    if not isinstance(svc, _FakeAdminConfigService):
        svc = _FakeAdminConfigService()
        context.ic_service.admin_config_service = svc
    return svc


# ── §A: the dormancy signal ──────────────────────────────────────────────
@then('"{handle}" is reported as dormant')
def step_then_reported_as_dormant(context, handle):
    row = context.ic_dao.get_crawl_target(handle)
    assert row.get("posts_dormant") is True, row


@given('"{handle}" is reported as dormant')
def step_given_reported_as_dormant(context, handle):
    """Arranges a target that is ALREADY dormant from some earlier run —
    poking the stored field directly rather than driving a real crawl,
    since the scenario using this ("Stop reporting dormancy once the
    target produces a post") cares only about the TRANSITION the next
    crawl makes, not how the prior dormant state was reached."""
    _ensure_context(context)
    context.ic_dao.update_crawl_target(
        handle, {"posts_dormant": True, "last_run_at": context.ic_now},
    )


@then('"{handle}" is not reported as dormant')
def step_then_not_reported_as_dormant(context, handle):
    row = context.ic_dao.get_crawl_target(handle)
    assert row.get("posts_dormant") is not True, row


@then('"{handle}" is still enabled')
def step_then_still_enabled(context, handle):
    row = context.ic_dao.get_crawl_target(handle)
    assert row.get("enabled") is True, row


# "quietbar" reports that its posts stream has never seeded" is ALREADY
# registered, identically, by crawl_transport_failure_visibility_steps.py
# (step_then_reports_never_seeded) — reused, not redefined.


@then('"{handle}" reports that its account is dormant')
def step_then_admin_reports_dormant(context, handle):
    target = context.ic_admin_targets[handle]
    assert getattr(target, "posts_dormant", None) is True, target


# ── §B: the seed lookback ────────────────────────────────────────────────
@given("the seed lookback is {n:d} months")
def step_given_seed_lookback_is_n_months(context, n):
    _ensure_context(context)
    _admin_config(context).set("crawl_seed_lookback", f"{n} months", updated_by="bdd-test")


@given("the steady-state lookback is {n:d} months")
def step_given_steady_state_lookback_is_n_months(context, n):
    """The STEADY-STATE bound is never a lookback string at all once a
    cursor is set (`compute_bound` sends `cursor - overlap`, an absolute
    timestamp) — this step exists for the scenario's own narrative
    symmetry with "the seed lookback is N months" and documents the value
    an operator would see next to the seed one in the admin config, but it
    is not consulted by any code path this feature drives. See the PR
    notes for this as a named deviation from a literal reading of the
    feature text."""
    _ensure_context(context)
    context.ic_config.default_initial_lookback = f"{n} months"


@given('the posts cursor for "{handle}" is already set')
def step_given_posts_cursor_already_set(context, handle):
    context.ic_dao.update_crawl_target(
        handle, {"cursor_posts_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
    )


@given('"{handle}" overrides its seed lookback to {n:d} months')
def step_given_target_overrides_seed_lookback(context, handle, n):
    context.ic_dao.update_crawl_target(handle, {"initial_lookback": f"{n} months"})


@then("Apify is asked for posts newer than {n:d} months")
def step_then_apify_asked_for_n_months(context, n):
    calls = [c for c in context.ic_apify.calls if c["results_type"] == "posts"]
    assert calls, "expected at least one posts-stream Apify call"
    assert calls[-1]["only_posts_newer_than"] == f"{n} months", calls


@then("Apify is not asked for posts newer than {n:d} months")
def step_then_apify_not_asked_for_n_months(context, n):
    calls = [c for c in context.ic_apify.calls if c["results_type"] == "posts"]
    assert calls, "expected at least one posts-stream Apify call"
    assert calls[-1]["only_posts_newer_than"] != f"{n} months", calls


# "Apify returns 3 posts for "quietbar"" is ALREADY registered, generically
# over any count, by crawl_error_visibility_steps.py
# (step_given_apify_returns_n_posts) — reused, not redefined.
