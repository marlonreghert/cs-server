"""Reusable Instagram caption event-marker matcher.

Detects whether a caption plausibly announces an event: an explicit pt-BR
date, a weekday paired with a time, or a ticketing/lineup term. This is a pure,
free classifier over text already cached in RDS (`instagram.posts`) — no model
call, no external request.

Deliberately its own module rather than inlined in the event venue targeting
evidence gate: plan `260804_instagram-event-extraction.md` (event extraction)
consumes the exact same matcher, and a caption either smells like an event or
it doesn't — that judgment must have one definition, not two that can drift.
"""
from __future__ import annotations

import re
from typing import Optional

_MONTHS = (
    "janeiro", "fevereiro", "março", "marco", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)
_WEEKDAYS = (
    "segunda", "terça", "terca", "quarta", "quinta", "sexta",
    "sábado", "sabado", "domingo",
)

# "25/12", "25.12.2026", "25-12" — a day/month numeral pair, the shape of an
# explicit event date. Deliberately loose on separators/year since captions are
# free-form; the ticketing/date-word markers below catch what this misses.
_DATE_NUMERIC_RE = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?\b")
# "25 de dezembro", "3 agosto"
_DATE_TEXTUAL_RE = re.compile(
    r"\b\d{1,2}\s*(?:de)?\s*(?:" + "|".join(_MONTHS) + r")\b", re.IGNORECASE
)
# A weekday within a short window of a clock time ("sexta ... 22h", "sábado às
# 20:00") — the classic Instagram event-flyer line, distinct from a plain
# mention of the weekday alone.
_WEEKDAY_TIME_RE = re.compile(
    r"\b(?:" + "|".join(_WEEKDAYS) + r")\b[\w\-,.!\s]{0,40}?\d{1,2}[:h]\d{0,2}\b",
    re.IGNORECASE,
)
# Ticketing/lineup vocabulary: a caption using any of these is talking about an
# event whether or not it also carries a date.
_TICKETING_TERMS: tuple[str, ...] = (
    "ingressos", "ingresso", "line-up", "lineup", "open bar", "sympla",
    "shotgun", "pré-venda", "pre-venda", "pista", "camarote",
)

MARKER_DATE = "date"
MARKER_WEEKDAY_TIME = "weekday_time"


def find_event_markers(caption: Optional[str]) -> list[str]:
    """Return every marker label matched in `caption`.

    Empty list for a falsy caption or one that matches nothing — a menu
    announcement or a holiday greeting with no date/time/ticketing language is
    the intended non-match.
    """
    if not caption:
        return []
    text = caption.lower()
    markers: list[str] = []
    if _DATE_NUMERIC_RE.search(caption) or _DATE_TEXTUAL_RE.search(text):
        markers.append(MARKER_DATE)
    if _WEEKDAY_TIME_RE.search(text):
        markers.append(MARKER_WEEKDAY_TIME)
    for term in _TICKETING_TERMS:
        if term in text:
            markers.append(f"term:{term}")
    return markers


def matches_event_marker(caption: Optional[str]) -> bool:
    """True when `caption` carries at least one event marker."""
    return bool(find_event_markers(caption))
