"""Behave steps for
tests/bdd/enrichment/promoter-source-provenance-parity.feature.

See plans/260813_promoter-source-provenance-parity.md.

Reuses two ALREADY-registered steps rather than redefining them — a second
identical (type, text) registration raises Behave's AmbiguousStep, the same
collision crawl_error_visibility_steps.py's own docstring documents and
works around:

- `When "the promoter crawl runs"` — registered by
  `instagram_promoter_events_steps.py`, reading `context.pe_service`/
  `context.pe_run_config`. This file's promoter-path scenarios build
  directly on that module's `context.pe_*` fixture (`_reset_context`,
  `_active_promoter`) rather than a second, parallel one, specifically so
  that existing registration can drive them unmodified.
- `When "the post is extracted"` — registered by
  `event_ticket_info_and_attractions_steps.py`, reading `context.ee_service`.
  This file's one venue-path scenario builds on
  `instagram_event_extraction_steps.py`'s `context.ee_*` fixture the same
  way.
- `Then 'the stored source records its media type as "{media_type}"'` —
  registered by `crawl_error_visibility_steps.py`, reading `context.ic_dao`/
  `context.ic_handle`/`context.ic_media_type_shortcode`. Two scenarios here
  need that exact assertion (one promoter-path, one venue-path); rather than
  reimplementing it, each Given step below points those three names at
  whichever fixture (`pe_*` or `ee_*`) it just built — same underlying
  objects, borrowed names, so the existing Then step finds the right data.

Every OTHER step here is new text, verified against the full
`tests/bdd/steps/` directory before being written to avoid a second,
silent collision.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from behave import given, then  # type: ignore[import-untyped]

from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post as _ee_add_post,
    _extraction_json as _ee_extraction_json,
    _stored_event as _ee_stored_event,
    step_given_an_event_candidate_venue_with_an_instagram_handle as _seed_venue,
)
from tests.bdd.steps.instagram_promoter_events_steps import (
    _active_promoter,
    _extraction_json as _pe_extraction_json,
    _reset_context as _pe_reset_context,
)
from tests.bdd.steps.multi_event_posts_steps import _FakeMultiEventOpenAIClient, _events_json

_PCS_LOGGER_NAME = "app.services.promoter_crawl_service"
_GENERIC_CAPTION = "Ingressos abertos! Vem pro role de hoje."


class _ListLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_date_time(date: str, time: str) -> datetime:
    return datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def _seed_provenance_post(
    context, shortcode: str, *,
    timestamp_text: str = "", media_type: str | None = None,
    caption: str = _GENERIC_CAPTION,
) -> dict:
    handle = context.pe_last_handle
    post = {
        "shortcode": shortcode,
        "caption": caption,
        "permalink": f"https://instagram.com/p/{shortcode}",
        "timestamp": timestamp_text,
        "image_urls": ["https://cdn.example.com/img.jpg"],
        "location_tag": None,
    }
    if media_type is not None:
        post["post_type"] = media_type
    context.pe_posts_client.posts_by_handle.setdefault(handle, []).append(post)
    context.pe_last_shortcode = shortcode
    return post


def _swap_in_multi_event_client(context) -> None:
    """The `pe_*` fixture's own fake OpenAI client (instagram_promoter_
    events_steps._FakeOpenAIClient) always wraps ONE programmed response
    into a single-element events list — it cannot produce several events
    from one call. Swapped, once, for the multi-event scenarios below;
    single-event scenarios keep the original fake untouched."""
    if not isinstance(context.pe_openai, _FakeMultiEventOpenAIClient):
        context.pe_openai = _FakeMultiEventOpenAIClient()
        context.pe_service.openai_client = context.pe_openai


def _multi_events(n: int) -> list[dict]:
    return [
        {"title": f"Roteiro Evento {i + 1}", "date_text": f"{(i % 28) + 1:02d}/08"}
        for i in range(n)
    ]


def _pe_rows(context) -> list[dict]:
    rows = context.pe_dao.list_events_by_source(context.pe_last_handle, context.pe_last_shortcode)
    assert rows, "expected at least one stored source for this post"
    return rows


# ── Background ────────────────────────────────────────────────────────────
@given('a promoter crawl target "{handle}"')
def step_given_a_promoter_crawl_target(context, handle):
    _pe_reset_context(context)
    _active_promoter(context, handle)


# ── upload time on every event a post yields ────────────────────────────────
@given('a promoter post uploaded at {date} {time} that yields {n:d} events')
def step_given_a_promoter_post_uploaded_at_that_yields_n_events(context, date, time, n):
    ts_text = f"{date}T{time}:00.000Z"
    _seed_provenance_post(context, "prov_multi_ts_1", timestamp_text=ts_text)
    _swap_in_multi_event_client(context)
    context.pe_openai.program(_events_json(_multi_events(n)))


@then('all {n:d} stored sources record an upload time of {date} {time}')
def step_then_all_n_stored_sources_record_upload_time(context, n, date, time):
    expected = _parse_date_time(date, time)
    rows = _pe_rows(context)
    assert len(rows) == n, rows
    for row in rows:
        assert row["source_uploaded_at"] == expected, row


# ── media type on every event a post yields ──────────────────────────────────
@given('a promoter post whose media type is "{media_type}" that yields {n:d} events')
def step_given_a_promoter_post_whose_media_type_is_that_yields_n_events(context, media_type, n):
    _seed_provenance_post(
        context, "prov_multi_mt_1",
        timestamp_text=_iso_z(context.pe_now), media_type=media_type,
    )
    _swap_in_multi_event_client(context)
    context.pe_openai.program(_events_json(_multi_events(n)))


@then('all {n:d} stored sources record their media type as "{media_type}"')
def step_then_all_n_stored_sources_record_media_type(context, n, media_type):
    rows = _pe_rows(context)
    assert len(rows) == n, rows
    for row in rows:
        assert row["source_media_type"] == media_type, row


# ── same provenance on every event of one post ──────────────────────────────
@given('a promoter post uploaded at {date} {time} whose media type is "{media_type}"')
def step_given_a_promoter_post_uploaded_at_whose_media_type_is(context, date, time, media_type):
    ts_text = f"{date}T{time}:00.000Z"
    _seed_provenance_post(
        context, "prov_same_1", timestamp_text=ts_text, media_type=media_type,
    )
    _swap_in_multi_event_client(context)
    context.pe_openai.program(_events_json([
        {"title": "Roteiro Evento 1", "date_text": "12/08"},
        {"title": "Roteiro Evento 2", "date_text": "13/08"},
    ]))


@then("every stored source of that post carries the same upload time")
def step_then_every_stored_source_carries_the_same_upload_time(context):
    rows = _pe_rows(context)
    values = {row["source_uploaded_at"] for row in rows}
    assert len(values) == 1, rows
    assert next(iter(values)) is not None, rows


@then("every stored source of that post carries the same media type")
def step_then_every_stored_source_carries_the_same_media_type(context):
    rows = _pe_rows(context)
    values = {row["source_media_type"] for row in rows}
    assert len(values) == 1, rows
    assert next(iter(values)) is not None, rows


# ── missing / unparseable timestamp -> NULL, never invented ─────────────────
@given("a promoter post with no timestamp")
def step_given_a_promoter_post_with_no_timestamp(context):
    _seed_provenance_post(context, "prov_no_ts_1", timestamp_text="")
    context.pe_openai.program(_pe_extraction_json())


@given('a promoter post whose timestamp is "{ts_text}"')
def step_given_a_promoter_post_whose_timestamp_is(context, ts_text):
    _seed_provenance_post(context, "prov_bad_ts_1", timestamp_text=ts_text)
    context.pe_openai.program(_pe_extraction_json())
    context.psp_log_handler = _ListLogHandler()
    context.psp_log_handler.setLevel(logging.WARNING)
    logging.getLogger(_PCS_LOGGER_NAME).addHandler(context.psp_log_handler)


@then("the stored sources record no upload time")
def step_then_the_stored_sources_record_no_upload_time(context):
    rows = _pe_rows(context)
    assert all(row["source_uploaded_at"] is None for row in rows), rows


@then("a warning is logged that the timestamp could not be read")
def step_then_a_warning_is_logged_that_the_timestamp_could_not_be_read(context):
    handler = context.psp_log_handler
    logging.getLogger(_PCS_LOGGER_NAME).removeHandler(handler)
    messages = " | ".join(r.getMessage() for r in handler.records)
    assert "timestamp" in messages.lower(), messages


@then("the stored sources' upload time does not equal their first seen time")
def step_then_upload_time_does_not_equal_first_seen_time(context):
    rows = _pe_rows(context)
    for row in rows:
        assert row["source_uploaded_at"] is None, row
        assert row.get("first_seen_at") is not None, row
        assert row["source_uploaded_at"] != row["first_seen_at"], row


# ── the naming-collision regression: media type vs item type ────────────────
@given('a promoter post whose media type is "{media_type}" announcing one event')
def step_given_a_promoter_post_whose_media_type_is_announcing_one_event(context, media_type):
    shortcode = "prov_kind_1"
    _seed_provenance_post(
        context, shortcode, timestamp_text=_iso_z(context.pe_now), media_type=media_type,
    )
    context.pe_openai.program(_pe_extraction_json())
    # Point the borrowed step's names (see module docstring) at this
    # scenario's own fixture.
    context.ic_dao = context.pe_dao
    context.ic_handle = context.pe_last_handle
    context.ic_media_type_shortcode = shortcode


@then('the stored item records its post type as "{post_type}"')
def step_then_the_stored_item_records_its_post_type_as(context, post_type):
    rows = _pe_rows(context)
    assert rows[0]["post_type"] == post_type, rows[0]


# ── venue path regression: unchanged, still records both fields ─────────────
@given('an archived venue post uploaded at {date} {time} whose media type is "{media_type}"')
def step_given_an_archived_venue_post_uploaded_at_whose_media_type_is(context, date, time, media_type):
    _seed_venue(context)
    ts = _parse_date_time(date, time)
    shortcode = "prov_venue_1"
    _ee_add_post(context, shortcode, timestamp=ts, media_type=media_type)
    context.ee_openai.program(_ee_extraction_json())
    # Point the borrowed step's names (see module docstring) at this
    # scenario's own fixture.
    context.ic_dao = context.ee_dao
    context.ic_handle = context.ee_handle
    context.ic_media_type_shortcode = shortcode


@then('the stored source records an upload time of {date} {time}')
def step_then_the_stored_source_records_an_upload_time_of(context, date, time):
    expected = _parse_date_time(date, time)
    row = _ee_stored_event(context)
    assert row["source_uploaded_at"] == expected, row
