"""Content-derived identity for one event within a post.

See plans/260806_multi-event-posts.md §A: several events can now share a
post (`0025_multi_event_posts` replaces `UNIQUE (source_handle,
source_shortcode)` with `UNIQUE (source_handle, source_shortcode,
source_event_key)`), so the row key can no longer be positional. The model
does not guarantee list order between runs, so an ordinal ("event #2") would
silently migrate an operator's confirmation onto a DIFFERENT event the
moment the model's list order changed. The key must be CONTENT-derived: a
stable hash over the event's distinguishing content (normalised title plus
resolved date).

Migration `0025_multi_event_posts` imports and calls this SAME function to
back-fill every existing row before the new constraint is added. Using
anything else there — even an equivalent-looking re-implementation — would
let a re-extraction of an old post derive a DIFFERENT key than the one the
migration wrote, turning the very next run into a duplicate-event generator.

`source_event_index` (the per-post ordinal) is a SEPARATE, display-order-only
value computed by the caller (simply the event's position in the model's
list, 1-based) and never participates in this key.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Optional


def _normalize_title(title: Optional[str]) -> str:
    """Case/accent/whitespace-insensitive: "O Homem do Fraque Verde" and a
    later re-extraction reading "o homem do fraque verde " (different case,
    trailing whitespace) must hash identically, or a harmless re-extraction
    quirk would look like a brand-new event and orphan the operator's
    confirmation of the original row."""
    text = (title or "").strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text)


def compute_source_event_key(title: Optional[str], starts_at: Optional[datetime]) -> str:
    """A stable hash over normalised title + resolved CALENDAR DATE — never
    the per-post ordinal (list order is not guaranteed stable between runs)
    and never the clock time (an unstated time defaulting to midnight one
    run and a stated time read on a later run must still resolve to the same
    key, or the operator's confirmation would silently detach).

    Two events in the same post with the same title and no resolvable date
    collapse onto the same key — an acceptable edge case (there is no
    stronger identity signal available), and no worse than today's single-
    event behaviour for an unresolvable date.
    """
    date_part = starts_at.date().isoformat() if starts_at is not None else ""
    payload = f"{_normalize_title(title)}|{date_part}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


__all__ = ["compute_source_event_key"]
