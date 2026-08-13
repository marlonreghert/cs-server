"""Behave steps for tests/bdd/enrichment/backfill-source-provenance.feature.

See plans/260813_backfill-source-provenance.md.

Drives `scripts.backfill_source_provenance.run_backfill` directly against an
in-memory RDS fake (tests/rds_fake.py) wrapped in the SAME `VenueRepository`
production wiring uses, and a deterministic in-memory double for S3
(`FakeArchiveManifestReader`) — CLAUDE.md forbids live S3 (or any other
external call) in BDD. No boto3 client, no network, exists anywhere in this
harness.

Three step texts here are DELIBERATELY NOT defined in this file, because
they are already registered elsewhere with the identical literal text and
Behave allows only one registration per pattern across `tests/bdd/steps/`
(a second one raises AmbiguousStep):

- `When "the backfill runs with apply"` / `"...without apply"` /
  `"...with apply again"` — registered by
  `backfill_misattributed_links_steps.py`, which now dispatches on whether
  `context.bsp_dao` (built here) or `context.bfl_dao` (built there) is
  present — see that module's `_run` docstring.
- `Then 'the stored source records its media type as "{media_type}"'` —
  registered by `crawl_error_visibility_steps.py`, reading `context.ic_dao`/
  `context.ic_handle`/`context.ic_media_type_shortcode`.
- `Then 'the stored source records an upload time of {date} {time}'` —
  registered by `promoter_source_provenance_parity_steps.py`, reading
  `context.ee_dao`/`context.ee_handle`/`context.ee_last_shortcode` (via
  `instagram_event_extraction_steps._stored_event`).

Both borrowed `Then` steps only appear in this feature's first scenario;
`_alias_reused_then_steps` below points all five borrowed names at THIS
scenario's own fixture (same trick `promoter_source_provenance_parity_
steps.py` already uses in the other direction for its own venue-path
regression scenario) right after the row that scenario checks is created.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.dao.venue_repository import VenueRepository
from scripts.backfill_source_provenance import (
    ManifestNotFound,
    manifest_key_for_cover_photo_key,
    run_backfill,
)
from tests.rds_fake import InMemoryRdsVenueStore

_DEFAULT_FIRST_SEEN = datetime(2020, 1, 1, tzinfo=timezone.utc)


class FakeArchiveManifestReader:
    """Deterministic double for `scripts.backfill_source_provenance.
    ArchiveManifestReader`. Exposes exactly one method, `get` — there is no
    `put_*` method anywhere on this class, so the "no object is written"
    scenario is checking a structural fact about the double, not merely an
    unused spy (the static check in `_assert_script_never_writes_to_s3`
    below covers the production class the same way)."""

    def __init__(self) -> None:
        self.manifests: dict[str, dict] = {}
        self.unreadable_keys: set[str] = set()
        self.reads: list[str] = []

    def get(self, key: str) -> dict:
        self.reads.append(key)
        if key in self.unreadable_keys:
            raise RuntimeError(f"simulated unreadable manifest read: {key}")
        if key not in self.manifests:
            raise ManifestNotFound(key)
        return self.manifests[key]


# ── harness setup ────────────────────────────────────────────────────────────
def _ensure_bsp(context) -> None:
    if getattr(context, "bsp_dao", None) is not None:
        return
    context.bsp_store = InMemoryRdsVenueStore()
    context.bsp_dao = VenueRepository(client=None, rds_store=context.bsp_store)
    context.bsp_reader = FakeArchiveManifestReader()
    context.bsp_rows: list[dict] = []
    context.bsp_counter = 0
    context.bsp_report = None
    context.bsp_exception = None


def _auto_cover_photo_key(n: int, shortcode: str) -> str:
    return (
        f"retrieved/source=instagram_posts/year=2026/month=08/day=12/"
        f"run_id=01BSP{n:06d}/venue_id=v_bsp/media/uncategorised/{shortcode}.jpg"
    )


def _insert_source(
    context, *, uploaded_at=None, media_type=None, handle="promonatal",
    shortcode: "str | None" = None, cover_photo_key: "str | None | bool" = False,
) -> dict:
    """Insert one `events.post_item_source` row (plus its parent
    `post_item`) directly, mirroring what a real crawl would have written
    BEFORE the forward fixes — never via a live crawl, since this feature
    repairs already-persisted history. `cover_photo_key=False` (the
    sentinel default, distinct from `None`) means "derive one"; pass
    `cover_photo_key=None` explicitly for a row with no archived cover
    photo at all.
    """
    _ensure_bsp(context)
    context.bsp_counter += 1
    n = context.bsp_counter
    shortcode = shortcode or f"bsp_post_{n}"
    if cover_photo_key is False:
        cover_photo_key = _auto_cover_photo_key(n, shortcode)
    fields = {
        "event_id": f"evt_bsp_{n:04d}", "venue_id": None, "title": f"Evento {n}",
        "status": "accepted", "source_kind": "venue_post",
        "source_handle": handle, "source_shortcode": shortcode,
        "source_permalink": f"https://instagram.com/p/{shortcode}",
        "cover_photo_key": cover_photo_key,
        "source_media_type": media_type, "source_uploaded_at": uploaded_at,
        "raw_extraction": None,
        "first_seen_at": _DEFAULT_FIRST_SEEN, "last_seen_at": _DEFAULT_FIRST_SEEN,
    }
    context.bsp_dao.insert_event(fields)
    entry = {
        "event_id": fields["event_id"], "handle": handle, "shortcode": shortcode,
        "cover_photo_key": cover_photo_key,
        "original_uploaded_at": uploaded_at, "original_media_type": media_type,
    }
    context.bsp_rows.append(entry)
    return entry


def _row(context, entry: "dict | None" = None) -> dict:
    entry = entry or context.bsp_rows[-1]
    row = context.bsp_dao.get_event(entry["event_id"])
    assert row is not None, entry
    return row


def _program_manifest(
    context, cover_photo_key: str, shortcode: str, *,
    uploaded_at: "str | None" = None, media_type: "str | None" = None,
) -> None:
    key = manifest_key_for_cover_photo_key(cover_photo_key)
    assert key is not None, cover_photo_key
    manifest = context.bsp_reader.manifests.setdefault(key, {"photos": []})
    photo_entry = {"shortcode": shortcode}
    if uploaded_at is not None:
        photo_entry["uploaded_at"] = uploaded_at
    if media_type is not None:
        photo_entry["post_type"] = media_type
    manifest["photos"].append(photo_entry)


def _alias_reused_then_steps(context, entry: dict) -> None:
    """Points the borrowed `Then` steps' fixture names (see module
    docstring) at THIS scenario's own store/handle/shortcode."""
    context.ee_dao = context.bsp_dao
    context.ee_handle = entry["handle"]
    context.ee_last_shortcode = entry["shortcode"]
    context.ic_dao = context.bsp_dao
    context.ic_handle = entry["handle"]
    context.ic_media_type_shortcode = entry["shortcode"]


# ── running the backfill (called by backfill_misattributed_links_steps._run) ──
def run_bsp_backfill(context, *, apply: bool, since_id=None) -> None:
    _ensure_bsp(context)
    # Snapshot every known row's two provenance columns immediately before
    # this run, so "no source is changed" can assert against what was true
    # a moment ago rather than what was true at fixture-creation time — the
    # two are the same for the dry-run scenario but NOT for the second-apply
    # scenario, where round 1 already legitimately changed the values and
    # round 2 must merely not change them AGAIN.
    context.bsp_pre_run_snapshot = {
        entry["event_id"]: (
            _row(context, entry).get("source_uploaded_at"),
            _row(context, entry).get("source_media_type"),
        )
        for entry in context.bsp_rows
    }
    context.bsp_exception = None
    try:
        context.bsp_report = run_backfill(
            context.bsp_dao, context.bsp_reader, apply=apply, since_id=since_id,
        )
    except Exception as exc:  # noqa: BLE001 - captured for Then steps to inspect
        context.bsp_exception = exc
        context.bsp_report = getattr(exc, "report", None)


# ── Background ────────────────────────────────────────────────────────────────
@given("the provenance backfill script is available")
def step_given_script_available(context):
    _ensure_bsp(context)


# ── row fixtures ──────────────────────────────────────────────────────────────
@given("a stored source with no upload time and no media type")
def step_given_source_no_upload_no_media(context):
    _insert_source(context, uploaded_at=None, media_type=None)


@given("a stored source that already records an upload time and a media type")
def step_given_source_already_has_values(context):
    _insert_source(
        context, uploaded_at=datetime(2025, 1, 1, tzinfo=timezone.utc), media_type="Image",
    )
    # Program a DISAGREEING manifest record at the same key/shortcode -- the
    # scenario proves the never-overwrite rule holds even when the archive
    # would say something different, not merely when there is nothing to say.
    entry = context.bsp_rows[-1]
    _program_manifest(
        context, entry["cover_photo_key"], entry["shortcode"],
        uploaded_at="2026-08-12T15:26:00.000Z", media_type="Video",
    )


@given("a stored source with no upload time")
def step_given_source_no_upload_time(context):
    _insert_source(context, uploaded_at=None, media_type=None)


@given("several stored sources with no upload time")
def step_given_several_sources_no_upload_time(context):
    for _ in range(3):
        entry = _insert_source(context, uploaded_at=None, media_type=None)
        _program_manifest(
            context, entry["cover_photo_key"], entry["shortcode"],
            uploaded_at="2026-08-10T09:00:00.000Z", media_type="Image",
        )


@given("two stored sources and the first one's manifest cannot be read")
def step_given_two_sources_first_unreadable(context):
    first = _insert_source(context, uploaded_at=None, media_type=None)
    unreadable_key = manifest_key_for_cover_photo_key(first["cover_photo_key"])
    context.bsp_reader.unreadable_keys.add(unreadable_key)
    context.bsp_first_row = first

    second = _insert_source(context, uploaded_at=None, media_type=None)
    _program_manifest(
        context, second["cover_photo_key"], second["shortcode"],
        uploaded_at="2026-08-11T18:00:00.000Z", media_type="Image",
    )
    context.bsp_second_row = second


# ── manifest fixtures ─────────────────────────────────────────────────────────
@given('its archived manifest records an upload time of {date} {time} and media type "{media_type}"')
def step_given_manifest_records_upload_and_media(context, date, time, media_type):
    entry = context.bsp_rows[-1]
    _program_manifest(
        context, entry["cover_photo_key"], entry["shortcode"],
        uploaded_at=f"{date}T{time}:00.000Z", media_type=media_type,
    )
    _alias_reused_then_steps(context, entry)


@given("its archived manifest records no upload time")
def step_given_manifest_records_no_upload_time(context):
    entry = context.bsp_rows[-1]
    _program_manifest(context, entry["cover_photo_key"], entry["shortcode"])


@given("no archived manifest record matches it")
def step_given_no_manifest_matches(context):
    entry = context.bsp_rows[-1]
    key = manifest_key_for_cover_photo_key(entry["cover_photo_key"])
    assert key not in context.bsp_reader.manifests, "fixture bug: a manifest already exists here"


# ── idempotency fixture ────────────────────────────────────────────────────────
@given("the backfill has already been applied")
def step_given_backfill_already_applied(context):
    _ensure_bsp(context)
    if not context.bsp_rows:
        entry = _insert_source(context, uploaded_at=None, media_type=None)
        _program_manifest(
            context, entry["cover_photo_key"], entry["shortcode"],
            uploaded_at="2026-08-01T10:00:00.000Z", media_type="Image",
        )
    run_bsp_backfill(context, apply=True)
    assert context.bsp_exception is None, context.bsp_exception


# ── assertions ─────────────────────────────────────────────────────────────────
@then("the stored source still records no upload time")
def step_then_source_still_no_upload_time(context):
    row = _row(context)
    assert row.get("source_uploaded_at") is None, row


@then("that source's upload time is unchanged")
def step_then_upload_time_unchanged(context):
    entry = context.bsp_rows[-1]
    row = _row(context, entry)
    assert row.get("source_uploaded_at") == entry["original_uploaded_at"], row


@then("that source's media type is unchanged")
def step_then_media_type_unchanged(context):
    entry = context.bsp_rows[-1]
    row = _row(context, entry)
    assert row.get("source_media_type") == entry["original_media_type"], row


@then("the stored source's upload time does not equal its first seen time")
def step_then_upload_time_not_first_seen(context):
    row = _row(context)
    assert row.get("source_uploaded_at") is None, row
    assert row.get("first_seen_at") is not None, row
    assert row["source_uploaded_at"] != row["first_seen_at"], row


@then("the report counts it as unmatched")
def step_then_report_counts_unmatched(context):
    report = context.bsp_report
    assert report is not None, context.bsp_exception
    assert report.dispositions.get("unmatched", 0) >= 1, report.dispositions


@then("no source is changed")
def step_then_no_source_changed(context):
    """Compares every known row's current two columns against the snapshot
    `run_bsp_backfill` took immediately before the run — correct whether
    this is the FIRST run over freshly-inserted null rows (dry-run scenario)
    or a SECOND run over already-filled rows (idempotency scenario): either
    way, "no source is changed" means "identical to a moment ago", not
    "identical to whatever the fixture originally inserted"."""
    report = context.bsp_report
    assert report is not None, context.bsp_exception
    snapshot = getattr(context, "bsp_pre_run_snapshot", {})
    for entry in context.bsp_rows:
        row = _row(context, entry)
        before = snapshot.get(entry["event_id"])
        after = (row.get("source_uploaded_at"), row.get("source_media_type"))
        assert before == after, (entry["event_id"], before, after)


@then("the report counts what it would have filled")
def step_then_report_counts_would_fill(context):
    report = context.bsp_report
    assert report is not None, context.bsp_exception
    assert report.dispositions.get("filled", 0) >= 3, report.dispositions
    assert report.applied is False, report


@then("the second source is still filled")
def step_then_second_source_filled(context):
    row = _row(context, context.bsp_second_row)
    assert row.get("source_uploaded_at") is not None, row
    assert row.get("source_media_type") == "Image", row


@then("the report counts the unreadable manifest")
def step_then_report_counts_unreadable(context):
    report = context.bsp_report
    assert report is not None, context.bsp_exception
    assert report.unmatched_by_reason.get("manifest_unreadable", 0) >= 1, report.unmatched_by_reason


@then("no object is written to the archive bucket")
def step_then_no_object_written(context):
    # Behavioural: the double has no put method at all, so nothing here
    # COULD have written -- confirmed by construction, not merely observed.
    assert not hasattr(context.bsp_reader, "put_object")
    assert not hasattr(context.bsp_reader, "put")
    _assert_script_never_writes_to_s3()


def _assert_script_never_writes_to_s3() -> None:
    """Static proof, not a mock: parse the script's OWN source and assert no
    call names anything that writes to S3. A script that genuinely never
    calls such a method cannot possibly write at runtime, mirroring
    backfill_misattributed_links_steps.py's identical proof for Apify/
    OpenAI/S3 imports."""
    import ast
    import inspect

    import scripts.backfill_source_provenance as mod

    forbidden = {
        "put_object", "put_manifest", "put_image", "put_promoter_manifest",
        "put_promoter_image", "delete_object", "upload_file", "upload_fileobj",
        "copy_object",
    }
    tree = ast.parse(inspect.getsource(mod))
    called_attrs = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    offending = called_attrs & forbidden
    assert not offending, f"script calls a write method: {offending}"
