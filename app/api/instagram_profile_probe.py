"""Does this Instagram handle belong to a real profile, and what is it called?

Measured behavior, not assumption (see plans/260728_instagram-handle-cascade.md):

  * A plain GET to instagram.com/<handle> returns HTTP 200 and ~602KB for EVERY
    handle, including nonsense ones. Status codes carry no information here.
  * The anonymous body is a JS shell: <title>Instagram</title>, 8 meta tags, no
    og: tags, no profile data. Nothing to compare against a venue.
  * With a CRAWLER user-agent, Instagram serves Open Graph — og:title with the
    display name, og:description with follower/post counts, og:image with the
    profile picture — and OMITS them entirely for a handle that does not exist.

So the probe is: fetch as a crawler, read og tags, and treat their absence as
absence of the profile.

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

_OG = r'<meta property="{}" content="([^"]*)"'
_FOLLOWERS = re.compile(r"([\d.,KMkm]+)\s+Followers", re.I)


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


def parse_profile_body(body: str) -> ProfileProbeResult:
    """Parse an Instagram profile page body into a probe result.

    Split out from the fetch so the parsing is testable against a captured body
    without touching the network.
    """
    og_type = _meta(body, "og:type")
    title = _meta(body, "og:title")
    if not title or (og_type and og_type != "profile"):
        return ProfileProbeResult(existence=EXIST_ABSENT)

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
            # The status is NOT a signal (200 for everything) — only the body is.
            result = parse_profile_body(response.text or "")
        except Exception as e:
            # Fail to unknown, never to absent: a transient failure must not be
            # recorded as proof that a real profile does not exist.
            logger.warning(f"[InstagramProbe] probe failed for @{handle}: {e}")
            result = ProfileProbeResult(existence=EXIST_UNKNOWN)

        self._cache[handle] = result
        return result

    async def close(self) -> None:
        await self._client.aclose()
