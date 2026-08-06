"""Behave steps for tests/bdd/api/event-cover-presign.feature.

Self-contained per scenario, mirroring instagram_event_extraction_steps.py's
pattern: a fresh FastAPI app with only admin_events_router registered, and a
fresh in-memory fake DAO + fake MediaArchiveStore per scenario (no live
S3/Postgres). Fakes RAISE on a call the scenario never set up for, rather than
returning a silent default — a fake that answers "successfully" to a call
nobody programmed is how a route regression (e.g. calling presign() when it
must not) slips past green.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

from behave import given, when, then  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container

RESULT_LABELS = ("signed", "no_key", "not_found", "failed")

# A clearly-attacker-shaped key, distinct from any key a fixture ever uses as
# the event's own cover_photo_key, so "was the injected key ever signed?" is
# unambiguous.
_INJECTED_KEY = "raw/source=attacker/year=2099/month=01/day=01/run_id=EVIL/all-venues.json.gz"


# ── fakes at the true boundary (no live Postgres/S3) ────────────────────────
class _FakeEventStore:
    """Stands in for container.pipeline_repository's get_event(event_id).

    `forbid_lookups` lets the "not authenticated" scenario prove the admin
    gate runs BEFORE any business logic: if the route looked the event up
    anyway, this raises instead of quietly returning None, which would let a
    missing/misordered auth dependency masquerade as a 404.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.get_event_calls: list[str] = []
        self.forbid_lookups = False

    def get_event(self, event_id: str):
        if self.forbid_lookups:
            raise AssertionError(
                "BDD harness: the event store must not be queried for an "
                "unauthenticated request"
            )
        self.get_event_calls.append(event_id)
        return self.events.get(event_id)


class _FakeMediaStore:
    """Stands in for container.media_archive_store's presign(key, expires_in).

    Unprogrammed (the default) means "this scenario must never call this" —
    calling it raises rather than returning a default url, so a scenario like
    "no cover key" or "unknown event" proves presign really is skipped, not
    just that the test forgot to check.
    """

    _UNSET = object()

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._programmed = self._UNSET

    def program_success(self, url: str) -> None:
        self._programmed = url

    def program_failure(self) -> None:
        self._programmed = None

    async def presign(self, key: str, expires_in: int = 900) -> Optional[str]:
        self.calls.append({"key": key, "expires_in": expires_in})
        if self._programmed is self._UNSET:
            raise AssertionError(
                "BDD harness: presign() called without being programmed — "
                "this scenario expected it to be skipped entirely"
            )
        return self._programmed


class _ListLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


_root_logger = logging.getLogger()
_installed_handler: Optional[_ListLogHandler] = None


def _install_log_capture(context) -> None:
    """Capture every log record emitted anywhere during the scenario, at
    DEBUG, so "the signed url does not appear in the logs" is a real check
    against whatever any module actually logged — not just the router's own
    logger, and not gated by a level that would silently swallow a leak."""
    global _installed_handler
    if _installed_handler is not None:
        _root_logger.removeHandler(_installed_handler)
    context.log_handler = _ListLogHandler()
    context.log_handler.setLevel(logging.DEBUG)
    _root_logger.addHandler(context.log_handler)
    _root_logger.setLevel(logging.DEBUG)
    _installed_handler = context.log_handler


def _override_setting(context, name, value) -> None:
    """Set a global setting for the scenario, remembering the original so
    environment.after_scenario restores it (no cross-scenario leakage) —
    mirrors on_demand_venue_photos_steps.py's helper."""
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)


def _headers(context) -> dict:
    override = getattr(context, "request_headers", None)
    return override if override is not None else context.admin_headers


def _snapshot_counts() -> dict:
    from app.metrics import EVENT_COVER_PRESIGN_TOTAL

    return {r: EVENT_COVER_PRESIGN_TOTAL.labels(result=r)._value.get() for r in RESULT_LABELS}


def _request_cover(context, event_id: str, *, extra_headers: Optional[dict] = None,
                    params: Optional[dict] = None) -> None:
    context._metric_before = _snapshot_counts()
    headers = dict(_headers(context))
    if extra_headers:
        headers.update(extra_headers)
    context.response = context.ecp_client.get(
        f"/admin/events/{event_id}/cover", headers=headers, params=params,
    )


# ── background ───────────────────────────────────────────────────────────────
@given("the operator is authenticated as an admin")
def step_admin_authenticated(context):
    app = FastAPI()
    app.include_router(admin_events_router)

    context.event_store = _FakeEventStore()
    context.media_store = _FakeMediaStore()
    container = SimpleNamespace(
        pipeline_repository=context.event_store,
        media_archive_store=context.media_store,
    )
    set_events_container(container)
    context.ecp_client = TestClient(app)

    _override_setting(context, "admin_api_key", "bdd-admin-secret")
    context.admin_headers = {"X-Admin-Api-Key": "bdd-admin-secret"}
    context.request_headers = None
    context.injected_key = None

    _install_log_capture(context)


# ── given ────────────────────────────────────────────────────────────────────
@given("an event whose cover photo is archived")
def step_event_with_cover(context):
    context.event_id = "evt-cover-1"
    context.cover_key = (
        "retrieved/source=instagram/year=2026/month=08/day=06/"
        "run_id=01J000000000000000000000/venue_id=venue-1/media/cover/evt-cover-1.jpg"
    )
    context.event_store.events[context.event_id] = {
        "event_id": context.event_id,
        "cover_photo_key": context.cover_key,
    }
    context.media_store.program_success(
        "https://datalake.example.com/signed-cover?sig=SECRETTOKEN&expires=900"
    )


@given("an event with no cover photo key")
def step_event_without_cover(context):
    context.event_id = "evt-no-cover"
    context.event_store.events[context.event_id] = {
        "event_id": context.event_id,
        "cover_photo_key": None,
    }


@given("no event exists for the requested id")
def step_no_such_event(context):
    context.event_id = "evt-does-not-exist"


@given("signing the object fails")
def step_signing_fails(context):
    context.media_store.program_failure()


@given("the caller is not authenticated as an admin")
def step_not_authenticated(context):
    context.request_headers = {}
    context.event_store.forbid_lookups = True


# ── when ─────────────────────────────────────────────────────────────────────
@when("the cover url for that event is requested")
def step_request_cover_for_event(context):
    _request_cover(context, context.event_id)


@when("the cover url for an event is requested")
def step_request_cover_generic(context):
    event_id = getattr(context, "event_id", "any-event-id")
    _request_cover(context, event_id)


@when("the cover url is requested with an object key supplied in the request")
def step_request_cover_with_injected_key(context):
    context.injected_key = _INJECTED_KEY
    _request_cover(
        context,
        context.event_id,
        extra_headers={"X-Object-Key": _INJECTED_KEY},
        params={
            "key": _INJECTED_KEY,
            "object_key": _INJECTED_KEY,
            "s3_key": _INJECTED_KEY,
            "cover_photo_key": _INJECTED_KEY,
        },
    )


# ── then ─────────────────────────────────────────────────────────────────────
@then("a signed url is returned")
def step_assert_signed_url(context):
    assert context.response.status_code == 200, context.response.text
    body = context.response.json()
    assert body.get("url"), body
    context.response_body = body


@then("an expiry is returned with it")
def step_assert_expiry(context):
    body = context.response_body
    assert body.get("expires_at"), body
    assert isinstance(body.get("expires_in"), int) and body["expires_in"] > 0, body


@then('the result "{result}" is counted')
def step_assert_result_counted(context, result):
    from app.metrics import EVENT_COVER_PRESIGN_TOTAL

    after = EVENT_COVER_PRESIGN_TOTAL.labels(result=result)._value.get()
    before = context._metric_before.get(result, 0)
    assert after == before + 1, (
        f'expected "{result}" count to increase by 1, before={before} after={after}'
    )


@then("the request is rejected as not found")
def step_assert_not_found(context):
    assert context.response.status_code == 404, context.response.text
    context.response_body = context.response.json()


@then("the reason distinguishes a missing cover from a missing event")
def step_assert_reason_no_key(context):
    detail = context.response_body.get("detail")
    assert detail == "Event has no archived cover photo", detail
    assert detail != "Event not found", detail


@then("the reason names the missing event")
def step_assert_reason_not_found(context):
    detail = context.response_body.get("detail")
    assert detail == "Event not found", detail


@then("the request fails with a server error")
def step_assert_server_error(context):
    assert context.response.status_code == 502, context.response.text
    context.response_body = context.response.json()


@then("no successful response carrying an empty url is returned")
def step_assert_no_empty_success(context):
    assert context.response.status_code != 200, context.response.text
    if context.response.status_code == 200:  # belt-and-suspenders
        assert context.response.json().get("url"), context.response.json()


@then("the request is rejected as unauthorised")
def step_assert_unauthorised(context):
    assert context.response.status_code == 401, context.response.text


@then("the signed object is the one recorded on the event")
def step_assert_signed_object(context):
    assert context.media_store.calls, "presign() was never called"
    signed_key = context.media_store.calls[-1]["key"]
    assert signed_key == context.cover_key, (
        f"signed {signed_key!r}, expected the event's own {context.cover_key!r}"
    )


@then("the key supplied in the request is ignored")
def step_assert_injected_key_ignored(context):
    assert context.response.status_code == 200, context.response.text
    signed_key = context.media_store.calls[-1]["key"]
    assert signed_key != context.injected_key, "the request-supplied key was signed"
    assert context.injected_key not in signed_key
    body = context.response.json()
    assert context.injected_key not in body.get("url", "")


@then("the signed url does not appear in the logs")
def step_assert_url_not_logged(context):
    body = context.response.json()
    url = body["url"]
    assert url, "no url to check against the logs"
    for record in context.log_handler.records:
        message = record.getMessage()
        assert url not in message, f"signed url leaked into a log record: {message!r}"
        if record.exc_text:
            assert url not in record.exc_text, f"signed url leaked into log traceback: {record.exc_text!r}"


@then("the cover route handles the request")
def step_assert_cover_route_handles(context):
    assert context.response.status_code == 200, context.response.text
    body = context.response.json()
    assert {"url", "expires_at", "expires_in"} <= set(body.keys()), body


@then("the request is not handled as a single-event lookup")
def step_assert_not_single_event_lookup(context):
    body = context.response.json()
    # EventOut's shape (the single-event GET) — a route-ordering regression
    # would produce this shape (or a bare 404 "Event not found") instead.
    assert "status" not in body, body
    assert "source_handle" not in body, body
