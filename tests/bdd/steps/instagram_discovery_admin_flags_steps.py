"""Behave steps for tests/bdd/api/instagram-discovery-admin-flags.feature.

Drives the REAL AddVenueHandler against a REAL InstagramCascadeService wired
with the SAME small fakes add_venue_instagram_discovery_steps.py uses at the
cascade's source boundaries — reused here (not redefined) so both features
exercise the identical cascade wiring. This file adds only what is specific
to resolving the "instagram_discovery" admin-config key: the Settings
override steps, the admin-config write steps, the credential-presence
Background step, and the generic-CRUD validator scenarios.

Venue naming: unlike add_venue_instagram_discovery_steps.py (which pins a
venue name per scenario via "BestTime creates the venue "X" successfully"),
this feature's scenarios add an unnamed "a venue" — sometimes twice in one
scenario ("Take effect on the next add"). A per-scenario sequence counter
generates a fresh, deterministic venue name for each add so a second add
never short-circuits on the address-hash cache of the first. A Given fixture
that must seed a source fake BEFORE the venue is named (e.g. "the
Google-search tier returns a candidate for the venue") peeks the NEXT name
the following "I add a venue..." step will assign, without consuming it.
"""
from __future__ import annotations

import asyncio

from behave import given, when, then  # type: ignore[import-untyped]

from app.config import settings
from app.handlers.add_venue_handler import (
    INSTAGRAM_DISCOVERY_CONFIG_KEY,
    AddVenueByAddressRequest,
)
from add_venue_instagram_discovery_steps import (  # type: ignore[import-not-found]
    _DEFAULT_ADDRESS,
    _DEFAULT_LAT,
    _DEFAULT_LNG,
    _FakeJudge,
    _FakePaidSearch,
    _FakeWebsiteSource,
    _StepResponse,
    _install_dynamic_besttime,
    _override_setting,
    _venue_id_for,
)
from app.services.instagram_cascade_service import InstagramCascadeService

_DEFAULT_CANDIDATE_URL = "https://instagram.com/admin_flags_default_candidate"


# ---------------------------------------------------------------------------
# Venue-name sequencing (see module docstring)
# ---------------------------------------------------------------------------


def _venue_name_for_seq(n: int) -> str:
    return f"Admin Flags Venue {n}"


def _venue_seq_peek(context) -> int:
    """The name the NEXT 'I add a venue...' step will assign, without
    consuming the sequence."""
    return getattr(context, "_ig_flags_venue_seq", 0) + 1


def _venue_seq_next(context) -> int:
    context._ig_flags_venue_seq = _venue_seq_peek(context)
    return context._ig_flags_venue_seq


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("the Google-search credential and the judge credential are both present")
def step_credentials_present(context):
    """Wires a real InstagramCascadeService with the free-source fakes plus
    the Google-search and judge fakes standing in for the real Container
    collaborators — "present" here means the collaborator objects exist on
    the service, mirroring Container building them on credential presence."""
    context.ig_google_listing = _FakeWebsiteSource()
    context.ig_archive = _FakeWebsiteSource()
    context.ig_venue_website = _FakeWebsiteSource()
    context.ig_google_search = _FakeWebsiteSource()
    context.ig_paid_search = _FakePaidSearch()
    context.ig_judge = _FakeJudge()

    service = InstagramCascadeService(
        venue_dao=context.venue_dao,
        google_listing=context.ig_google_listing,
        archive=context.ig_archive,
        venue_website=context.ig_venue_website,
        google_search=context.ig_google_search,
        paid_search=context.ig_paid_search,
        probe=None,  # blocked in production; every real caller also has none
        judge=context.ig_judge,
    )
    context.instagram_cascade_service = service
    context.add_venue_handler.instagram_cascade_service = service
    # Alias so instagram_handle_cascade_steps.py's shared "the judge is
    # consulted" / "the judge is not consulted" @then steps (registered once,
    # globally) read the SAME fake this file wires under context.ig_judge —
    # avoids a duplicate/ambiguous step definition.
    context.judge = context.ig_judge
    _install_dynamic_besttime(context)


@given("the Google-search credential is not configured")
def step_google_search_credential_missing(context):
    """Simulates Container._build_google_search_source() returning None
    (no APIFY_API_TOKEN): the collaborator itself is absent, independent of
    whatever the resolved admin-config/Settings flag says."""
    context.instagram_cascade_service.google_search = None


# ---------------------------------------------------------------------------
# Settings-level fallback (deploy-time defaults)
# ---------------------------------------------------------------------------


@given("the setting for the Google-search tier is enabled")
def step_setting_google_search_enabled(context):
    _override_setting(context, "instagram_google_search_enabled", True)


@given("the setting for the Google-search tier is disabled")
def step_setting_google_search_disabled(context):
    _override_setting(context, "instagram_google_search_enabled", False)


@given("the setting for the judge is enabled")
def step_setting_judge_enabled(context):
    _override_setting(context, "instagram_judge_enabled", True)


@given("the setting for the judge is disabled")
def step_setting_judge_disabled(context):
    _override_setting(context, "instagram_judge_enabled", False)


# ---------------------------------------------------------------------------
# Admin-config fixture writes (the resolved-per-add value)
# ---------------------------------------------------------------------------


@given('no "instagram_discovery" admin config is stored')
def step_no_admin_config_stored(context):
    assert context.admin_config_service.get(INSTAGRAM_DISCOVERY_CONFIG_KEY) is None


def _set_instagram_discovery_field(context, field, value):
    """Merges into whatever is already stored so two fixture steps in the same
    scenario (one per field) compose rather than clobber each other."""
    current = context.admin_config_service.get(INSTAGRAM_DISCOVERY_CONFIG_KEY) or {}
    updated = dict(current)
    updated[field] = value.strip().lower() == "true"
    context.admin_config_service.set(
        INSTAGRAM_DISCOVERY_CONFIG_KEY, updated, updated_by="bdd-test"
    )


# Registered under BOTH @given and @when: the "Take effect on the next add"
# scenario uses this same phrasing mid-scenario as a When (a Gherkin keyword
# choice, not a distinct behavior), and behave matches step text per-type
# rather than treating given/when/then as interchangeable aliases.
@given('"instagram_discovery" admin config sets "{field}" to {value}')
def step_set_instagram_discovery_field_given(context, field, value):
    _set_instagram_discovery_field(context, field, value)


@when('"instagram_discovery" admin config sets "{field}" to {value}')
def step_set_instagram_discovery_field_when(context, field, value):
    _set_instagram_discovery_field(context, field, value)


@given("the admin config read fails")
def step_admin_config_read_fails(context):
    def _boom(key):
        raise RuntimeError("admin config read failed (simulated RDS outage)")

    context.rds_store.get_admin_config = _boom


@given("the Google-search tier returns a candidate for the venue")
def step_google_search_candidate(context):
    """Also guarantees the tier is actually reached: these two scenarios
    ("Enable/disable the judge from admin config alone") are testing the
    JUDGE flag in isolation, so the Google-search tier's own resolved value
    is deliberately pinned on via the Settings fallback rather than left to
    whatever default the scenario didn't set."""
    _override_setting(context, "instagram_google_search_enabled", True)
    vid = _venue_id_for(_venue_name_for_seq(_venue_seq_peek(context)))
    context.ig_google_search.websites[vid] = _DEFAULT_CANDIDATE_URL


# ---------------------------------------------------------------------------
# When: add a venue
# ---------------------------------------------------------------------------


def _do_add_venue(context) -> None:
    n = _venue_seq_next(context)
    name = _venue_name_for_seq(n)
    vid = _venue_id_for(name)
    # A default Google-search candidate is always available unless a scenario
    # already seeded one (via "the Google-search tier returns a candidate for
    # the venue") — free sources stay empty per this feature's own framing
    # ("no Instagram on any free source"); whether google_search is actually
    # REACHED is entirely governed by the resolved flag, not by whether a
    # candidate exists to find.
    context.ig_google_search.websites.setdefault(vid, _DEFAULT_CANDIDATE_URL)
    _install_dynamic_besttime(context)

    request = AddVenueByAddressRequest.model_validate(
        {
            "venue_name": name,
            "venue_address": _DEFAULT_ADDRESS,
            "venue_lat": _DEFAULT_LAT,
            "venue_lng": _DEFAULT_LNG,
        }
    )
    outcome = asyncio.run(context.add_venue_handler.add(request))
    context.response = _StepResponse(outcome.status_code, outcome.body)


@when("I add a venue with no Instagram on any free source")
def step_add_a_venue(context):
    _do_add_venue(context)


@when("I add another venue with no Instagram on any free source")
def step_add_another_venue(context):
    _do_add_venue(context)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the venue is still created")
def step_venue_still_created(context):
    assert context.response.status_code == 201, context.response.text
    assert context.response.json().get("status") == "created", context.response.json()


# ---------------------------------------------------------------------------
# Generic admin-config CRUD (validator accept/reject, over real HTTP)
# ---------------------------------------------------------------------------

_INSTAGRAM_DISCOVERY_URL = "/admin/config/instagram_discovery"


def _snapshot_instagram_discovery(context) -> None:
    context._instagram_discovery_before = context.admin_config_service.get(
        INSTAGRAM_DISCOVERY_CONFIG_KEY
    )


def _record_write_result(context) -> None:
    """Mirrors force_update_config_validator_steps.py's context.set_error
    convention so its shared "the write succeeds" @then step (registered
    once, globally, by behave's step registry) works for this feature's HTTP
    -driven writes too, without a duplicate/ambiguous step definition."""
    if context.response.status_code == 200:
        context.set_error = None
    else:
        context.set_error = AssertionError(context.response.text)


@when('an operator writes "instagram_discovery" with "{field}" set to "{value}"')
def step_write_bad_value(context, field, value):
    _snapshot_instagram_discovery(context)
    context.response = context.client.put(
        _INSTAGRAM_DISCOVERY_URL, json={field: value}
    )
    _record_write_result(context)


@when(
    'an operator writes "instagram_discovery" with an unknown field "{field}"'
)
def step_write_unknown_field(context, field):
    _snapshot_instagram_discovery(context)
    context.response = context.client.put(
        _INSTAGRAM_DISCOVERY_URL, json={field: True}
    )
    _record_write_result(context)


@when(
    'an operator writes "instagram_discovery" with "google_search_enabled" '
    'true and "judge_enabled" false'
)
def step_write_valid(context):
    _snapshot_instagram_discovery(context)
    context.response = context.client.put(
        _INSTAGRAM_DISCOVERY_URL,
        json={"google_search_enabled": True, "judge_enabled": False},
    )
    _record_write_result(context)


@then("the write is rejected")
def step_write_rejected(context):
    assert context.response.status_code == 400, context.response.text


@then('the stored "instagram_discovery" config is unchanged')
def step_stored_unchanged(context):
    current = context.admin_config_service.get(INSTAGRAM_DISCOVERY_CONFIG_KEY)
    before = context._instagram_discovery_before
    assert current == before, (current, before)


@then(
    'reading "instagram_discovery" returns "google_search_enabled" true and '
    '"judge_enabled" false'
)
def step_read_back(context):
    resp = context.client.get(_INSTAGRAM_DISCOVERY_URL)
    assert resp.status_code == 200, resp.text
    value = resp.json()["value"]
    assert value == {"google_search_enabled": True, "judge_enabled": False}, value
