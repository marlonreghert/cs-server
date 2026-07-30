"""Behave steps for tests/bdd/enrichment/instagram-probe-fails-open.feature.

Feeds the REAL page shapes production observes. The login-wall body is the one
that matters: from the production box every Instagram request — real handle or
nonsense — returns that page, and the probe read it as proof of absence.
"""
from __future__ import annotations

import asyncio

import httpx
from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

PROFILE_BODY = (
    '<html><head>'
    '<meta property="og:type" content="profile" />'
    '<meta property="og:title" content="Tasquinha do Tio (&#064;tasquinhadotio)'
    ' &#x2022; Instagram photos and videos" />'
    '<meta property="og:description" content="23K Followers, 8 Following, 218 Posts" />'
    '<meta property="og:image" content="https://scontent.cdninstagram.com/pic.jpg" />'
    '</head><body></body></html>'
)

# What production actually gets: 200, no og tags, Instagram's login shell.
LOGIN_WALL_BODY = (
    '<html><head><title>Login &#x2022; Instagram</title></head>'
    '<body><div id="loginForm">Log in to Instagram</div>'
    '<script>window.__initialData = {"challenge": null}</script></body></html>'
)

CHALLENGE_BODY = (
    '<html><head><title>Instagram</title></head>'
    '<body>Please wait a few minutes before you try again.</body></html>'
)

# Instagram's own response for a handle with no profile: its page shell, no
# profile og tags, and NOT redirected away.
MISSING_PROFILE_BODY = (
    '<html><head>'
    '<meta property="og:site_name" content="Instagram" />'
    '<meta property="al:ios:app_name" content="Instagram" />'
    '</head><body>'
    "<div>Sorry, this page isn't available.</div>"
    '</body></html>'
)

PROFILE_URL = "https://www.instagram.com/somehandle/"
LOGIN_URL = "https://www.instagram.com/accounts/login/"


def _probe_module():
    from app.api import instagram_profile_probe as m

    return m


def _counted(result):
    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != "instagram_profile_probe":
            continue
        for s in metric.samples:
            if s.name.endswith("_total") and s.labels.get("result") == result:
                total += s.value
    return total


def _build_probe(context, body, final_url=PROFILE_URL, status=200):
    m = _probe_module()

    def handler(request):
        return httpx.Response(status, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://www.instagram.com")
    # The final URL is what distinguishes a served profile page from a redirect
    # to the login wall, so the probe must be able to see it.
    context.final_url = final_url
    context.probe = m.InstagramProfileProbe(client=client)
    context.probe_body = body


# ── Given: page shapes ────────────────────────────────────────────────────────
@given("Instagram serves a genuine profile page")
def step_real_profile(context):
    _build_probe(context, PROFILE_BODY)


@given("Instagram serves its login wall instead of the profile")
def step_login_wall(context):
    _build_probe(context, LOGIN_WALL_BODY, final_url=LOGIN_URL)


@given("Instagram serves a challenge page")
def step_challenge(context):
    _build_probe(context, CHALLENGE_BODY)


@given("Instagram redirects the request to its login page")
def step_redirected(context):
    _build_probe(context, LOGIN_WALL_BODY, final_url=LOGIN_URL)


@given("Instagram serves an empty response")
def step_empty(context):
    _build_probe(context, "")


@given("Instagram serves its page for a handle that has no profile")
def step_missing_profile(context):
    _build_probe(context, MISSING_PROFILE_BODY)


@when("the profile is probed")
def step_probe(context):
    import inspect

    m = _probe_module()
    context.probe_before = {
        r: _counted(r) for r in ("present", "absent", "unknown", "blocked")
    }
    # The final URL is the block signal, so the parser needs it — but pass it
    # only if the parser takes it, so this step asserts BEHAVIOR rather than
    # dying on a signature that has not been widened yet.
    if "final_url" in inspect.signature(m.parse_profile_body).parameters:
        context.result = m.parse_profile_body(context.probe_body, final_url=context.final_url)
    else:
        context.result = m.parse_profile_body(context.probe_body)


# ── Then: probe outcomes ──────────────────────────────────────────────────────
@then("the probe reports the profile as present")
def step_present(context):
    m = _probe_module()
    assert context.result.existence == m.EXIST_PRESENT, context.result


@then("the probe reports the profile existence as unknown")
def step_unknown(context):
    m = _probe_module()
    assert context.result.existence == m.EXIST_UNKNOWN, (
        f"got {context.result.existence!r} — a page that never answered the "
        "question must not be read as proof the profile is gone"
    )


@then("the probe reports that Instagram would not answer")
def step_blocked(context):
    m = _probe_module()
    assert context.result.existence == m.EXIST_BLOCKED, (
        f"got {context.result.existence!r} — a refusal to answer must be "
        "recorded as such, so a blocked egress is visible instead of looking "
        "like a catalogue of deleted accounts"
    )


@then("the probe does not report the profile as absent")
def step_not_absent(context):
    m = _probe_module()
    assert context.result.existence != m.EXIST_ABSENT, context.result


@then("the probe reports the profile as absent")
def step_absent(context):
    m = _probe_module()
    assert context.result.existence == m.EXIST_ABSENT, context.result


@then("the probe reports the display name from the page")
def step_display_name(context):
    assert context.result.display_name == "Tasquinha do Tio", context.result


@then("a blocked probe is counted")
def step_blocked_counted(context):
    """Counted under its OWN label — that is what makes an egress block visible
    instead of looking like a catalogue of deleted accounts."""
    from app.metrics import INSTAGRAM_PROFILE_PROBE_TOTAL

    m = _probe_module()
    before = _counted(m.EXIST_BLOCKED)
    INSTAGRAM_PROFILE_PROBE_TOTAL.labels(result=context.result.existence).inc()
    assert _counted(m.EXIST_BLOCKED) == before + 1, (
        f"a blocked probe was counted as {context.result.existence!r}"
    )


# ── Cascade interaction ───────────────────────────────────────────────────────
class _Venue:
    venue_name = "Tasquinha do Tio"


class _Dao:
    def __init__(self):
        self.saved = None

    def list_servable_venue_ids(self):
        return ["ven_1"]

    def get_venue(self, venue_id):
        return _Venue()

    def get_venue_instagram(self, venue_id):
        return None

    def set_venue_instagram(self, record):
        self.saved = record


class _Listing:
    async def website_for(self, venue_id, venue=None):
        return "https://www.instagram.com/tasquinhadotio/"


class _Probe:
    def __init__(self, existence):
        self.existence = existence

    async def fetch(self, handle):
        m = _probe_module()
        return m.ProfileProbeResult(existence=self.existence, display_name="Tasquinha do Tio")


@given("the venue's Google listing links an Instagram profile")
def step_listing_links(context):
    context.dao = _Dao()
    context.listing = _Listing()


@given("the probe cannot verify whether that profile exists")
def step_probe_unknown(context):
    m = _probe_module()
    context.cascade_probe = _Probe(m.EXIST_UNKNOWN)


@given("the probe confirms that profile does not exist")
def step_probe_absent(context):
    m = _probe_module()
    context.cascade_probe = _Probe(m.EXIST_ABSENT)


@when("the cascade discovers the venue's handle")
def step_cascade_discover(context):
    from app.services.instagram_cascade_service import InstagramCascadeService

    service = InstagramCascadeService(
        venue_dao=context.dao,
        google_listing=context.listing,
        probe=context.cascade_probe,
    )
    context.cascade_result = asyncio.run(service.discover("ven_1", {"force_refresh": True}))


@then("the cascade accepts the handle")
def step_cascade_accepts(context):
    assert context.cascade_result.accepted, (
        f"candidate rejected at confidence {context.cascade_result.confidence} — "
        "an unverifiable probe must not veto a handle the venue's own Google "
        "listing points at"
    )


@then("the stored record carries the handle")
def step_stored(context):
    saved = context.dao.saved
    assert saved is not None and saved.instagram_handle, saved


@then("the cascade does not accept a handle")
def step_cascade_rejects(context):
    assert not context.cascade_result.accepted, context.cascade_result
