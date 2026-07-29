"""Where an Instagram handle can come from, and how a URL becomes one.

Sources are ordered cheapest-first. The first two cost nothing — they read data
the platform already holds — and only the third spends money, so the ordering is
the feature, not an implementation detail.

Extraction is deliberately strict. Instagram URLs found in the wild are not all
profiles: `l.instagram.com/?u=…` is an OUTBOUND REDIRECT WRAPPER (real example
from our own archived data pointed at ifood.com.br), and /p/, /reel/ and
/explore/ are posts and listings. Accepting any of them would mint a handle that
looks plausible, fails verification, and burns the venue's not_found window.
"""
from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

SOURCE_GOOGLE_WEBSITE = "google_website"
SOURCE_ARCHIVED_GMAPS = "archived_gmaps_website"
SOURCE_APIFY_SEARCH = "apify_search"

# Cheapest first. The paid source is last on purpose.
SOURCE_ORDER = (SOURCE_GOOGLE_WEBSITE, SOURCE_ARCHIVED_GMAPS, SOURCE_APIFY_SEARCH)

PAID_SOURCES = frozenset({SOURCE_APIFY_SEARCH})

# Instagram's own outbound link wrapper. Anything behind it is a THIRD PARTY
# site, not the profile — treating it as a handle yields `?u=https%3A%2F%2F…`.
_SHIM_HOSTS = frozenset({"l.instagram.com", "lm.instagram.com"})

# First path segments that are Instagram features, not usernames.
_NON_PROFILE_PATHS = frozenset(
    {"p", "reel", "reels", "explore", "stories", "tv", "s", "accounts", "direct"}
)

REJECT_LINK_SHIM = "link_shim"
REJECT_NON_PROFILE_PATH = "non_profile_path"
REJECT_EMPTY = "empty"
REJECT_MALFORMED = "malformed"
REJECT_NOT_INSTAGRAM = "not_instagram"


@dataclass(frozen=True)
class HandleCandidate:
    handle: str
    source: str
    display_name: Optional[str] = None
    raw_url: Optional[str] = None


def extract_handle(url: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return `(handle, rejection_reason)` for a URL. Exactly one is set.

    A rejection is never silent — the caller counts the reason, so a new shim
    host appearing in the wild surfaces as a metric rather than as an
    unexplained drop in yield.
    """
    text = (url or "").strip()
    if not text:
        return None, REJECT_EMPTY
    try:
        parsed = urllib.parse.urlparse(text if "//" in text else f"https://{text}")
    except Exception:
        return None, REJECT_MALFORMED

    host = (parsed.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host in _SHIM_HOSTS:
        return None, REJECT_LINK_SHIM
    if "instagram.com" not in host:
        return None, REJECT_NOT_INSTAGRAM

    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    if not segments:
        return None, REJECT_EMPTY
    first = segments[0]
    if first.lower() in _NON_PROFILE_PATHS:
        return None, REJECT_NON_PROFILE_PATH

    handle = normalize_handle(first)
    if not handle:
        return None, REJECT_MALFORMED
    return handle, None


def normalize_handle(raw: Optional[str]) -> Optional[str]:
    """Lowercase, strip a leading @, and drop any query/fragment leftovers
    (`?igshid=…` rides along on shared profile links)."""
    text = (raw or "").strip().lstrip("@")
    text = text.split("?")[0].split("#")[0].strip("/")
    text = text.lower()
    if not text or any(c.isspace() for c in text):
        return None
    return text
