"""Behave steps for tests/bdd/api/user-activity-tracking.feature.

Exercises the engagement session API end-to-end:
- "a session is recorded …" POSTs /v1/sessions (the production write path).
- "the admin requests user activity counts" GETs /admin/users/activity-counts.
- Backdated "had a session …" steps seed the fake activity store directly so
  window math (1d/7d/30d) can be exercised deterministically.

Dates are bucketed in America/Recife to match the service's recife_today(); the
step computes it inline (no dependency on the production helper) so the scenario
reaches a meaningful red before the helper exists.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytz
from behave import given, when, then  # type: ignore[import-untyped]

_RECIFE = pytz.timezone("America/Recife")


def _recife_today():
    return datetime.now(_RECIFE).date()


# ── Background ────────────────────────────────────────────────────────────────
@given("a clean engagement activity store")
def step_clean_activity_store(context):
    # A fresh in-memory store is built per scenario by environment.py; nothing to
    # do beyond asserting the activity store starts empty.
    assert context.rds_store.count_users(None) == 0


# ── recording sessions (production write path) ────────────────────────────────
@when('a session is recorded for user "{uid}"')
def step_record_session(context, uid):
    context.response = context.client.post("/v1/sessions", json={"user_id": uid})


@when("a session is recorded with no user id")
def step_record_session_no_uid(context):
    context.response = context.client.post("/v1/sessions", json={})


# ── seeding historical activity (window math) ─────────────────────────────────
def _seed_session(context, uid, activity_date):
    pseudo = context.engagement_service.pseudonymize(uid)
    context.rds_store.record_app_session(pseudo, activity_date)


@given('user "{uid}" had a session today')
def step_session_today(context, uid):
    _seed_session(context, uid, _recife_today())


@given('user "{uid}" had a session {days:d} days ago')
def step_session_days_ago(context, uid, days):
    _seed_session(context, uid, _recife_today() - timedelta(days=days))


# ── reading counts ────────────────────────────────────────────────────────────
@when("the admin requests user activity counts")
def step_request_counts(context):
    context.response = context.client.get("/admin/users/activity-counts")
    context.counts = context.response.json()


def _count(context, field):
    assert isinstance(context.counts, dict), context.counts
    return context.counts.get(field)


@then("total_users is {n:d}")
def step_total_users(context, n):
    assert _count(context, "total_users") == n, context.counts


@then("active_1d is {n:d}")
def step_active_1d(context, n):
    assert _count(context, "active_1d") == n, context.counts


@then("active_7d is {n:d}")
def step_active_7d(context, n):
    assert _count(context, "active_7d") == n, context.counts


@then("active_30d is {n:d}")
def step_active_30d(context, n):
    assert _count(context, "active_30d") == n, context.counts


# ── pseudonymization ──────────────────────────────────────────────────────────
@then('the engagement activity store holds no row equal to "{uid}"')
def step_no_raw_row(context, uid):
    assert not context.rds_store.contains_raw_value(uid)


@then("it holds one pseudonymized activity row for today")
def step_one_pseudo_row_today(context):
    rows = context.rds_store.app_session_rows_for(_recife_today())
    assert len(rows) == 1, rows
    assert rows[0] != "uid-alice"


# ── status assertions ─────────────────────────────────────────────────────────
@then("the response status is {status:d}")
def step_response_status(context, status):
    assert context.response.status_code == status, context.response.text
