"""Normalise an extracted `ticket_url` into something a phone can open.

plans/260906_events-venue-bairro-and-ticket-url.md §3 (defect C2). Pure: no
I/O, no config, no clock.

## Why this exists and why it lives at PROJECTION time

`openai_event_extraction_client` records exactly what the model said — that
module's explicit contract — so `events.post_item.ticket_url` holds whatever
the caption offered. The single production value is the bare host
`"evenyx.com"`, which `Linking.openURL` refuses because it carries no
scheme. RDS is deliberately NOT rewritten: the extraction stays auditable,
and because the events projection rebuilds every payload on every cycle the
correction re-asserts itself on deploy with no migration and no
re-extraction.

The rules below are the CONTRACT vibes_bot and the mobile app plan against,
so they are pinned here in the order they are applied. `ticket_url` is
therefore either `null` or an absolute `http`/`https` URL **carrying a real
host** — never a bare host, never a bare scheme (`https://`), never a
`host:port` whose port is not a real TCP port, never
`mailto:`/`tel:`/`javascript:`/`data:`, never prose.
"""
from __future__ import annotations

import re
from typing import Optional

# The longest value worth keeping. Anything past this is not a link a human
# typed into a caption.
MAX_TICKET_URL_LENGTH = 2048

# Sentence punctuation a caption-scraped link routinely ends in.
_TRAILING_PUNCTUATION = ".,;!"

# RFC 3986 scheme grammar. A leading token only counts as a SCHEME when it
# matches this AND carries no `.` — without the dot exclusion, `evenyx.com`
# in `evenyx.com:8080/lote2` reads as a scheme and a legitimate host:port
# ticket link is thrown away (review finding F30).
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*$")
_PORT_DIGITS_RE = re.compile(r"^[0-9]{1,5}$")
_ALPHA_RE = re.compile(r"^[A-Za-z]+$")

# The highest TCP port. The digit COUNT alone is not the rule: `:80808` and
# `:99999` are five digits and are not ports, so a URL built around them is
# one no client can open. vibes_bot's serve-time twin
# (`vibes_bot/app/services/event_ticket_url.py`) applies the same numeric
# bound, so the two modules agree input-for-input.
_MAX_PORT = 65535

# Outcome labels for `events_projection_ticket_url_total{outcome}`.
OUTCOME_ABSENT = "absent"
OUTCOME_PASSTHROUGH = "passthrough"
OUTCOME_SCHEME_ADDED = "scheme_added"
OUTCOME_REJECTED = "rejected"


def _valid_port(port: str) -> bool:
    """A `host:<port>` suffix counts as a port only when it is 1-5 digits AND
    a real TCP port (<= 65535). The digit-count half alone accepted
    `evenyx.com:80808`, which was then qualified to
    `https://evenyx.com:80808/...` and counted `scheme_added` — a link the
    phone cannot open, recorded as a success. Leading zeros are tolerated
    (`:0080` is port 80); an empty suffix (`host:`) is not a port."""
    if not _PORT_DIGITS_RE.match(port):
        return False
    return int(port) <= _MAX_PORT


def _looks_like_a_host(authority: str) -> bool:
    """`authority` with any port already stripped. A host is acceptable when
    it carries no userinfo (`@`), has at least one dot, and its last
    dot-separated label is a plausible TLD (>= 2 characters, all letters) —
    which is what separates `evenyx.com` from `R$50` and from an email
    address."""
    if not authority or "@" in authority:
        return False
    labels = authority.split(".")
    if len(labels) < 2:
        return False
    if any(not label for label in labels):
        return False
    tld = labels[-1]
    return len(tld) >= 2 and bool(_ALPHA_RE.match(tld))


def _authority_is_valid(after_scheme: str) -> bool:
    """`after_scheme` is everything following any `scheme://` (or the whole
    value when there is no scheme). Splits off the authority at the first
    `/`, `?` or `#`, requires any `:`-suffix to be a real TCP port
    (`_valid_port`), and runs the remaining host through
    `_looks_like_a_host`.

    Shared by rule 5 and rule 8 deliberately. Rule 5 originally returned any
    `http(s)://`-prefixed string untouched, which accepted `https://`,
    `https:///` and `https://.` as `passthrough` — a ticket button that
    opens nothing, and INVISIBLE, because `passthrough` never trips the
    `rejected` counter an operator watches. One definition means the two
    rules cannot drift apart again.
    """
    authority = re.split(r"[/?#]", after_scheme, maxsplit=1)[0]
    host = authority
    if ":" in authority:
        host, _, port = authority.rpartition(":")
        if not _valid_port(port):
            return False
    return _looks_like_a_host(host)


def classify_ticket_url(value) -> tuple[Optional[str], str]:
    """`(normalised_url_or_None, outcome_label)`.

    The outcome is what `events_projection_ticket_url_total` counts, and it
    is returned alongside the value rather than re-derived by the caller so
    the two can never disagree:

    - `absent` — nothing was stored at all (None, a non-string, or blank).
    - `passthrough` — already an absolute `http`/`https` URL; returned
      as-is, never rewritten and never upgraded from http to https.
    - `scheme_added` — a scheme-less host was qualified with `https://`.
    - `rejected` — something was stored, but it is not a usable web URL.
      A rising `rejected` rate is the operator's signal that extraction is
      emitting prose.
    """
    # 1. Not a string, or None.
    if not isinstance(value, str):
        return None, OUTCOME_ABSENT

    # 2. Trim surrounding whitespace, then trailing sentence punctuation.
    text = value.strip().rstrip(_TRAILING_PUNCTUATION).strip()
    if not text:
        return None, OUTCOME_ABSENT

    # 3. Absurdly long.
    if len(text) > MAX_TICKET_URL_LENGTH:
        return None, OUTCOME_REJECTED

    # 4. Internal whitespace means prose, not a URL.
    if any(ch.isspace() for ch in text):
        return None, OUTCOME_REJECTED

    # 5. Already absolute and web-schemed: hand it back BYTE-FOR-BYTE
    #    untouched — but only once its authority is a real host. A scheme
    #    with nothing behind it (`https://`, `https:///`, `https://.` after
    #    step 2's punctuation strip) is not "an absolute http/https URL",
    #    and passing it through would put a dead ticket button on the card
    #    while counting it as a success.
    lowered = text.lower()
    for scheme in ("http://", "https://"):
        if lowered.startswith(scheme):
            if not _authority_is_valid(text[len(scheme):]):
                return None, OUTCOME_REJECTED
            return text, OUTCOME_PASSTHROUGH

    # 6. Any OTHER scheme is not something the app should open.
    head, sep, _ = text.partition(":")
    if sep and "." not in head and _SCHEME_RE.match(head):
        return None, OUTCOME_REJECTED

    # 7. A protocol-relative link is just a scheme-less one.
    candidate = text[2:] if text.startswith("//") else text
    if not candidate:
        return None, OUTCOME_REJECTED

    # 8. Scheme-less: validate the authority, keep everything after it.
    if not _authority_is_valid(candidate):
        return None, OUTCOME_REJECTED

    # `https` and not `http`: a purchase page served over plaintext draws a
    # browser interstitial, and every real ticketing host redirects anyway.
    # `candidate` is preserved byte-for-byte, so port, path, query and
    # fragment all survive.
    return f"https://{candidate}", OUTCOME_SCHEME_ADDED


def normalize_ticket_url(value: Optional[str]) -> Optional[str]:
    """The stored `ticket_url` as an absolute `http`/`https` URL, or None
    when it is not a usable web URL. Never raises — every non-conforming
    input is simply None."""
    normalised, _outcome = classify_ticket_url(value)
    return normalised


__all__ = [
    "MAX_TICKET_URL_LENGTH",
    "OUTCOME_ABSENT",
    "OUTCOME_PASSTHROUGH",
    "OUTCOME_REJECTED",
    "OUTCOME_SCHEME_ADDED",
    "classify_ticket_url",
    "normalize_ticket_url",
]
