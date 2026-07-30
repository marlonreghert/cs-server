"""Does this Instagram handle belong to a real profile, and what is it called?

Measured behavior, not assumption (see plans/260728_instagram-handle-cascade.md):

  * A plain GET to instagram.com/<handle> returns HTTP 200 and ~602KB for EVERY
    handle, including nonsense ones. Status codes carry no information here.
  * The anonymous body is a JS shell: <title>Instagram</title>, 8 meta tags, no
    og: tags, no profile data. Nothing to compare against a venue.
  * With a CRAWLER user-agent, Instagram serves Open Graph — og:title with the
    display name, og:description with follower/post counts, og:image with the
    profile picture — and OMITS them entirely for a handle that does not exist.

So the probe is: fetch as a crawler and read og tags. Their PRESENCE proves the
profile exists. Their absence proves nothing on its own — a login wall, a
challenge page and an empty body look identical — so absence of the profile must
be stated by Instagram itself before it is believed.

This is undocumented behavior Instagram can withdraw at any time. It therefore
fails to `unknown`, never to `absent`: a timeout or a changed page must not
start silently marking real handles as fake. `unknown` withholds the existence
bonus and pushes the candidate to the ambiguous path — it never rejects.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# A social crawler UA is what unlocks the og: tags; a normal browser UA gets the
# JS shell. This is the load-bearing detail of the whole probe.
DEFAULT_CRAWLER_UA = (
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
)

EXIST_PRESENT = "present"
EXIST_ABSENT = "absent"
EXIST_UNKNOWN = "unknown"
# Instagram answered, but refused to tell us anything: a login wall, a challenge,
# a redirect away from the profile. Scored exactly like `unknown` — it withholds
# the existence bonus and never rejects a candidate — but kept as its own value
# so an egress block is a visible spike instead of a silent flood of false
# absences. Datacenter IPs get this for EVERY handle, real or not.
EXIST_BLOCKED = "blocked"

_OG = r'<meta property="{}" content="([^"]*)"'
_FOLLOWERS = re.compile(r"([\d.,KMkm]+)\s+Followers", re.I)

# A URL path that is not the handle's own page means Instagram sent us somewhere
# else — it never answered the question that was asked.
_DIVERTED_PATHS = ("/accounts/login", "/challenge", "/accounts/suspended")

# Positive evidence that Instagram itself served the page.
_INSTAGRAM_MARKERS = (
    'property="og:site_name" content="Instagram"',
    'content="Instagram"',
    "instagram.com",
)

# Positive evidence that the handle has no profile. ABSENCE MUST BE PROVEN:
# without one of these, a page missing its og tags is unknown, not empty.
_NOT_FOUND_MARKERS = (
    "sorry, this page isn't available",
    "sorry, this page isn&#039;t available",
    "page isn't available",
    "page not found",
    "the link you followed may be broken",
)

_BLOCK_MARKERS = (
    "please wait a few minutes",
    "log in to instagram",
    "loginform",
    "/accounts/login",
)


@dataclass(frozen=True)
class ProfileProbeResult:
    existence: str = EXIST_UNKNOWN
    display_name: Optional[str] = None
    followers_count: Optional[str] = None
    image_url: Optional[str] = None
    raw_title: Optional[str] = None

    @property
    def exists(self) -> bool:
        return self.existence == EXIST_PRESENT


def _meta(body: str, prop: str) -> Optional[str]:
    m = re.search(_OG.format(re.escape(prop)), body)
    return html.unescape(m.group(1)) if m else None


def _no_profile(body: str, final_url: Optional[str]) -> ProfileProbeResult:
    """Classify a page that carries no profile metadata.

    Three different things look identical at the og-tag level, and only one of
    them is evidence about the handle.
    """
    lowered = (body or "").lower()

    # 1. Sent somewhere else entirely — the question was never answered.
    if final_url and any(p in str(final_url).lower() for p in _DIVERTED_PATHS):
        return ProfileProbeResult(existence=EXIST_BLOCKED)
    if any(m in lowered for m in _BLOCK_MARKERS):
        return ProfileProbeResult(existence=EXIST_BLOCKED)

    # 2. Nothing to read at all.
    if not lowered.strip():
        return ProfileProbeResult(existence=EXIST_UNKNOWN)

    # 3. Instagram's own "no such profile" page — the ONLY proof of absence.
    served_by_instagram = any(m.lower() in lowered for m in _INSTAGRAM_MARKERS)
    if served_by_instagram and any(m in lowered for m in _NOT_FOUND_MARKERS):
        return ProfileProbeResult(existence=EXIST_ABSENT)

    return ProfileProbeResult(existence=EXIST_UNKNOWN)


def parse_profile_body(body: str, final_url: Optional[str] = None) -> ProfileProbeResult:
    """Parse an Instagram profile page body into a probe result.

    Split out from the fetch so the parsing is testable against a captured body
    without touching the network.

    ABSENCE IS PROVEN, NEVER INFERRED. Missing og tags used to mean "no such
    profile", but that is also what a login wall, a challenge page, an empty
    body and a redirect look like — and from a datacenter IP Instagram serves
    the login wall for EVERY handle. That read every real profile as deleted,
    and because a venue finalizing `not_found` has its stored handle
    overwritten with NULL, a blocked probe did not merely fail: it destroyed
    data. So a page only proves absence when Instagram plainly says so.
    """
    og_type = _meta(body, "og:type")
    title = _meta(body, "og:title")
    if not title or (og_type and og_type != "profile"):
        return _no_profile(body, final_url)

    # "Tasquinha do Tio (@tasquinhadotio) • Instagram photos and videos"
    display_name = title.split("(@")[0].strip() or None
    description = _meta(body, "og:description") or ""
    followers = None
    m = _FOLLOWERS.search(description)
    if m:
        followers = m.group(1)

    return ProfileProbeResult(
        existence=EXIST_PRESENT,
        display_name=display_name,
        followers_count=followers,
        image_url=_meta(body, "og:image"),
        raw_title=title,
    )


class InstagramProfileProbe:
    """Fetches Open Graph metadata for a handle, with a per-run cache."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_CRAWLER_UA,
        timeout_seconds: float = 10.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        # One warning per run, not per handle: a blocked egress affects every
        # probe, and 1,400 identical lines would bury the run's real output.
        self._logged_block = False
        self._client = client or httpx.AsyncClient(follow_redirects=True)
        self._cache: dict[str, ProfileProbeResult] = {}

    async def fetch(self, handle: str) -> ProfileProbeResult:
        if not handle:
            return ProfileProbeResult(existence=EXIST_ABSENT)
        if handle in self._cache:
            return self._cache[handle]

        url = f"https://www.instagram.com/{handle}/"
        try:
            response = await self._client.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
            )
            # The status is NOT a signal (200 for everything). The body says what
            # was served, and the FINAL url says whether we were served the
            # profile at all or quietly diverted to the login wall.
            result = parse_profile_body(
                response.text or "", final_url=str(response.url)
            )
            if result.existence == EXIST_BLOCKED and not self._logged_block:
                self._logged_block = True
                logger.warning(
                    f"[InstagramProbe] Instagram is not serving profile metadata "
                    f"to this host (@{handle} -> {response.url}). Existence checks "
                    "are unavailable for this run; candidates fall back to "
                    "provenance and name similarity."
                )
        except Exception as e:
            # Fail to unknown, never to absent: a transient failure must not be
            # recorded as proof that a real profile does not exist.
            logger.warning(f"[InstagramProbe] probe failed for @{handle}: {e}")
            result = ProfileProbeResult(existence=EXIST_UNKNOWN)

        self._cache[handle] = result
        return result

    async def close(self) -> None:
        await self._client.aclose()
