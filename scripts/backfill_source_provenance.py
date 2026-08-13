"""Operator CLI: recover `source_uploaded_at`/`source_media_type` for every
pre-existing `events.post_item_source` row from the archive manifest that was
already written for it.

See plans/260813_backfill-source-provenance.md.

`archive_sources.py` and its callers (`VenuePhotoArchiveService`,
`InstagramCrawlChainer`, `PromoterCrawlService`) write one manifest record per
archived photo, carrying the post's own `uploaded_at` and `post_type` (the
MEDIA type — "Video"/"Image"/"Sidecar" — not `events.post_item.post_type`,
which is event/promotion/menu/food/other; see migration 0036's own
docstring for the collision this deliberately does not touch). Both forward
writers (plans/260812_crawl-error-visibility.md §C, plans/
260813_promoter-source-provenance-parity.md) now populate the two columns on
every NEW row; this script recovers them for every row written before that,
by reading the SAME manifest record back rather than re-deriving anything.

### The join (plan §A)
Every stored source row that has a `cover_photo_key` was archived under an
exact, self-describing S3 prefix — the key itself names the run, the entity
(`venue_id=<id>` or `promoter=<handle>`), and therefore the manifest that
was written alongside it (`<same prefix><entity>/info/_manifest.json`).
`manifest_key_for_cover_photo_key` derives that ONE manifest key directly
from the row's own `cover_photo_key` — no bucket listing, no scanning other
runs, no "which of several candidates" ambiguity: the row's own cover photo
key IS the record of which run archived it. `find_manifest_entry` then
matches the row's `source_shortcode` against that ONE manifest's `photos`
list (a manifest can hold several posts' worth of entries per run). Several
entries can share a shortcode within one manifest — a carousel's several
child images — and always carry identical post-level facts (the same
`uploaded_at`/`post_type`) by construction, so taking the first match is
deterministic and never loses information.

A row with no `cover_photo_key` (no image was ever archived for it) has no
key to derive a manifest from and is reported unmatched, never guessed.

### Never invent, never overwrite (plan §B)
A column already holding a value is left alone — checked BEFORE any S3 read
(`decide_one`'s first branch) so an already-complete row costs nothing.
`update_event_source_provenance` enforces the same rule again, at the SQL
layer, with `COALESCE(existing, new)` — a second, independent guarantee that
does not rely on this script's own bookkeeping being correct.

An absent, empty, or unparseable `uploaded_at` in the manifest leaves the
column NULL — parsed through the SAME helper
(`app.services.event_venue_targeting._parse_timestamp`) the promoter and
venue forward-writers already use, never a second parser that could drift
from theirs. Never `now()`, never `first_seen_at` — see the plan's own
"Never substitute the crawl time" scenario.

### Read-only against S3 (plan §C)
`ArchiveManifestReader` exposes exactly one method, `get`, which calls
`s3_client.get_object` and nothing else. There is no write method on this
class and none is added anywhere in this module — a script built on this
port cannot write to the archive bucket by construction.

Dry-run by default, `--apply` to write. Idempotent (a second `--apply`
changes nothing — every row it would have touched is now `already_present`)
and resumable (`--since-id`). Exits non-zero, after writing, if the outcome
arithmetic does not balance or a write ever reports the row gone.

Usage:
    python -m scripts.backfill_source_provenance                        # dry-run: report only
    python -m scripts.backfill_source_provenance --apply                # write the recovered values
    python -m scripts.backfill_source_provenance --apply --since-id X   # resume after source id X

Capture the dry-run report BEFORE running --apply. Review the disposition
counts first: a high `unmatched` count means the join key is wrong, not that
the archive is missing (plan §A) — abandon the run rather than apply it, and
re-derive the join instead of forcing it through.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.services.event_venue_targeting import _parse_timestamp

logger = logging.getLogger("backfill_source_provenance")

# ── dispositions ─────────────────────────────────────────────────────────────
# The plan's own four ("Report counts by disposition: filled, already-present,
# unmatched, unparseable") plus one honest fifth: a manifest record WAS found
# (the join succeeded) but it simply never recorded either fact — expected and
# normal for `promoter_post` rows' media type (see the module docstring's
# "several entries" paragraph and the PR notes for why the promoter archive
# path never wrote a media type at all), not a join failure and not malformed
# data, so folding it into `unmatched` or `unparseable` would misdescribe it.
DISPOSITION_ALREADY_PRESENT = "already_present"
DISPOSITION_FILLED = "filled"
DISPOSITION_UNMATCHED = "unmatched"
DISPOSITION_UNPARSEABLE = "unparseable"
DISPOSITION_MATCHED_NO_DATA = "matched_no_new_data"

UNMATCHED_NO_SHORTCODE = "no_shortcode"
UNMATCHED_NO_COVER_PHOTO_KEY = "no_cover_photo_key"
UNMATCHED_UNRECOGNIZED_KEY_SHAPE = "unrecognized_key_shape"
UNMATCHED_MANIFEST_NOT_FOUND = "manifest_not_found"
UNMATCHED_MANIFEST_UNREADABLE = "manifest_unreadable"
UNMATCHED_NO_ENTRY_FOR_SHORTCODE = "no_manifest_entry_for_shortcode"


class BackfillError(Exception):
    """Base for every hard-stop this script can raise. `report`, when set,
    is whatever partial `Report` had been built before the failure, so the
    CLI can still print it before exiting non-zero."""

    def __init__(self, message: str, *, report: "Optional[Report]" = None):
        super().__init__(message)
        self.report = report


class ArithmeticImbalance(BackfillError):
    """`selected != sum(report.dispositions.values())`."""


class WriteAffectedNoRows(BackfillError):
    def __init__(self, source_id: str, *, report: "Optional[Report]" = None):
        super().__init__(f"update affected no row for source_id={source_id}", report=report)
        self.source_id = source_id


class ManifestNotFound(Exception):
    """The manifest object genuinely does not exist at this key — a normal,
    expected outcome (a row from before the archive existed, or a run whose
    manifest write itself failed at the time), never treated as an error."""


# ── S3 (read-only) ────────────────────────────────────────────────────────────
class ArchiveManifestReader:
    """Read-only S3 access for manifest lookups.

    The ONLY boto3 call this class can make is `get_object` — there is no
    `put_object` (or any other write) method here, and none is ever added.
    docs/venue-retrieval-storage.md's IAM section describes a writer role
    that cannot even read; this is the deliberate mirror image for a repair
    script that must never write, enforced by the shape of the class rather
    than by convention.
    """

    def __init__(self, *, bucket: str, s3_client: Any):
        self.bucket = bucket
        self._s3 = s3_client

    def get(self, key: str) -> dict:
        """The parsed manifest at `key`.

        Raises `ManifestNotFound` for a genuinely absent object (S3's
        `NoSuchKey`/404) — the caller counts that as an ordinary unmatched
        row. Raises whatever the client raises for anything else (a
        transport error, an access error, malformed JSON) so the caller can
        count THAT as a distinct "manifest unreadable" outcome instead of
        silently folding a real problem into an ordinary miss.
        """
        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=key)
        except Exception as e:
            code = None
            response_meta = getattr(e, "response", None)
            if isinstance(response_meta, dict):
                code = (response_meta.get("Error") or {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise ManifestNotFound(key) from e
            raise
        return json.loads(response["Body"].read())


def build_manifest_reader(settings_obj) -> Optional[ArchiveManifestReader]:
    """The production reader, or None when no archive bucket is configured
    (mirrors `app.container`'s own `archive_bucket = media_archive_bucket or
    datalake_bucket` fallback, since this script reads the SAME archive that
    fallback names)."""
    bucket = settings_obj.media_archive_bucket or settings_obj.datalake_bucket
    if not bucket:
        return None
    from app.dao.datalake_writer import _build_s3_client

    s3_client = _build_s3_client(
        region=settings_obj.datalake_region,
        access_key_id=settings_obj.datalake_access_key_id or None,
        secret_access_key=settings_obj.datalake_secret_access_key or None,
    )
    return ArchiveManifestReader(bucket=bucket, s3_client=s3_client)


# ── the join (plan §A) ────────────────────────────────────────────────────────
def manifest_key_for_cover_photo_key(cover_photo_key: Optional[str]) -> Optional[str]:
    """`retrieved/.../venue_id=<id>/media/<cat>/<photo>.<ext>` (or
    `.../promoter=<handle>/...`) -> the manifest key for that SAME run and
    entity: `retrieved/.../venue_id=<id>/info/_manifest.json`.

    Returns None when the key does not contain a recognisable
    `venue_id=`/`promoter=` segment — an unrecognised shape is reported
    unmatched, never guessed at.
    """
    if not cover_photo_key:
        return None
    parts = cover_photo_key.split("/")
    for i, part in enumerate(parts):
        if part.startswith("venue_id=") or part.startswith("promoter="):
            return "/".join(parts[: i + 1]) + "/info/_manifest.json"
    return None


def find_manifest_entry(manifest: Optional[dict], shortcode: Optional[str]) -> Optional[dict]:
    """The manifest's own `photos[]` entry for `shortcode`, or None.

    A manifest aggregates every post archived in one run, so this filters
    down to the one this row's own post produced. Several entries can share
    a shortcode (a carousel's child images) — they carry identical
    post-level facts by construction, so the first match is taken
    deterministically; see the module docstring.
    """
    if not shortcode:
        return None
    for entry in (manifest or {}).get("photos") or []:
        if entry.get("shortcode") == shortcode:
            return entry
    return None


# ── per-row decision ──────────────────────────────────────────────────────────
@dataclass
class Decision:
    source_id: str
    event_id: str
    action: str
    unmatched_reason: Optional[str] = None
    old_uploaded_at: Any = None
    new_uploaded_at: Any = None
    old_media_type: Optional[str] = None
    new_media_type: Optional[str] = None
    manifest_key: Optional[str] = None

    def write_fields(self) -> dict:
        """Only ever used for action == filled."""
        return {"source_uploaded_at": self.new_uploaded_at, "source_media_type": self.new_media_type}


def decide_one(source_row: dict, reader: "ArchiveManifestReader") -> Decision:
    """The pure per-row verdict — computed without writing, so it is
    identical in dry-run and --apply and unit-testable on a bare dict."""
    source_id = source_row["id"]
    event_id = source_row["event_id"]
    old_uploaded_at = source_row.get("source_uploaded_at")
    old_media_type = source_row.get("source_media_type")

    def _unmatched(reason: str, manifest_key: Optional[str] = None) -> Decision:
        return Decision(
            source_id=source_id, event_id=event_id, action=DISPOSITION_UNMATCHED,
            unmatched_reason=reason, old_uploaded_at=old_uploaded_at,
            old_media_type=old_media_type, manifest_key=manifest_key,
        )

    # ── plan §B: never overwrite -- checked BEFORE any S3 read ────────────
    if old_uploaded_at is not None and old_media_type is not None:
        return Decision(
            source_id=source_id, event_id=event_id, action=DISPOSITION_ALREADY_PRESENT,
            old_uploaded_at=old_uploaded_at, new_uploaded_at=old_uploaded_at,
            old_media_type=old_media_type, new_media_type=old_media_type,
        )

    shortcode = source_row.get("source_shortcode")
    if not shortcode:
        return _unmatched(UNMATCHED_NO_SHORTCODE)

    cover_photo_key = source_row.get("cover_photo_key")
    if not cover_photo_key:
        return _unmatched(UNMATCHED_NO_COVER_PHOTO_KEY)

    manifest_key = manifest_key_for_cover_photo_key(cover_photo_key)
    if manifest_key is None:
        return _unmatched(UNMATCHED_UNRECOGNIZED_KEY_SHAPE)

    try:
        manifest = reader.get(manifest_key)
    except ManifestNotFound:
        return _unmatched(UNMATCHED_MANIFEST_NOT_FOUND, manifest_key)
    except Exception as e:  # noqa: BLE001 - a single bad manifest must not abort the run
        logger.warning(
            "backfill_source_provenance: manifest read failed for source_id=%s key=%s: %s",
            source_id, manifest_key, e,
        )
        return _unmatched(UNMATCHED_MANIFEST_UNREADABLE, manifest_key)

    entry = find_manifest_entry(manifest, shortcode)
    if entry is None:
        return _unmatched(UNMATCHED_NO_ENTRY_FOR_SHORTCODE, manifest_key)

    # ── plan §B: write only what the manifest states ───────────────────────
    new_uploaded_at = old_uploaded_at
    unparseable = False
    if old_uploaded_at is None:
        raw = entry.get("uploaded_at")
        parsed = _parse_timestamp(raw)
        if parsed is not None:
            new_uploaded_at = parsed
        elif raw:
            # Non-empty but failed to parse — a real anomaly (Apify changed
            # its format), distinct from a manifest that simply never
            # recorded a timestamp at all.
            unparseable = True
            logger.warning(
                "backfill_source_provenance: unparseable uploaded_at for "
                "source_id=%s: %r", source_id, raw,
            )
        # else: absent/empty -- leave NULL, never invented.

    new_media_type = old_media_type
    if old_media_type is None:
        media_type = entry.get("post_type")
        if media_type:
            new_media_type = media_type

    filled_upload = old_uploaded_at is None and new_uploaded_at is not None
    filled_media = old_media_type is None and new_media_type is not None
    if filled_upload or filled_media:
        action = DISPOSITION_FILLED
    elif unparseable:
        action = DISPOSITION_UNPARSEABLE
    else:
        action = DISPOSITION_MATCHED_NO_DATA

    return Decision(
        source_id=source_id, event_id=event_id, action=action,
        old_uploaded_at=old_uploaded_at, new_uploaded_at=new_uploaded_at,
        old_media_type=old_media_type, new_media_type=new_media_type,
        manifest_key=manifest_key,
    )


# ── report ────────────────────────────────────────────────────────────────────
@dataclass
class Report:
    selected: int = 0
    dispositions: Counter = field(default_factory=Counter)
    unmatched_by_reason: Counter = field(default_factory=Counter)
    rows: list = field(default_factory=list)  # every non-already_present Decision
    failed_writes: list = field(default_factory=list)
    applied: bool = False
    balanced: bool = False

    @property
    def filled_count(self) -> int:
        return self.dispositions.get(DISPOSITION_FILLED, 0)

    @property
    def changed_count(self) -> int:
        return self.filled_count


def check_balance(report: Report) -> None:
    total = sum(report.dispositions.values())
    if total != report.selected:
        report.balanced = False
        raise ArithmeticImbalance(
            f"totals did not balance: selected={report.selected} but "
            f"dispositions sum to {total}: {dict(report.dispositions)}",
            report=report,
        )
    report.balanced = True


def run_backfill(
    venue_dao, reader: "ArchiveManifestReader", *, apply: bool, since_id: Optional[str] = None,
) -> Report:
    """The whole backfill in one pass: selection, per-row decisions, writes
    (when `apply`), then the balance check.

    `since_id` resumes: only source rows with `id > since_id` (plain string
    comparison — real source ids are `evsrc_<ULID>`, so this IS
    chronological order) are considered this run.
    """
    all_sources = venue_dao.list_all_event_sources()
    candidates = sorted(
        (s for s in all_sources if since_id is None or s["id"] > since_id),
        key=lambda s: s["id"],
    )

    report = Report(applied=apply)
    report.selected = len(candidates)

    for source in candidates:
        decision = decide_one(source, reader)
        report.dispositions[decision.action] += 1
        if decision.unmatched_reason:
            report.unmatched_by_reason[decision.unmatched_reason] += 1
        if decision.action != DISPOSITION_ALREADY_PRESENT:
            report.rows.append(decision)

        if apply and decision.action == DISPOSITION_FILLED:
            ok = venue_dao.update_event_source_provenance(
                decision.source_id, **decision.write_fields(),
            )
            if not ok:
                report.failed_writes.append(decision.source_id)
                break

    if report.failed_writes:
        raise WriteAffectedNoRows(report.failed_writes[0], report=report)
    check_balance(report)
    return report


def _print_report(report: Report) -> None:
    mode = "APPLY" if report.applied else "DRY RUN"
    logger.info("=== backfill_source_provenance (%s) ===", mode)
    logger.info("selected: %d", report.selected)
    logger.info("dispositions: %s", dict(report.dispositions))
    if report.unmatched_by_reason:
        logger.info("unmatched by reason: %s", dict(report.unmatched_by_reason))
    for d in report.rows:
        logger.info(
            "  %s source_id=%s event_id=%s | uploaded_at: %r -> %r | media_type: %r -> %r "
            "| manifest_key=%s | unmatched_reason=%s",
            d.action, d.source_id, d.event_id, d.old_uploaded_at, d.new_uploaded_at,
            d.old_media_type, d.new_media_type, d.manifest_key, d.unmatched_reason,
        )
    logger.info("balanced: %s", report.balanced)
    unmatched = report.dispositions.get(DISPOSITION_UNMATCHED, 0)
    if report.selected:
        pct = 100.0 * unmatched / report.selected
        logger.info(
            "unmatched: %d/%d (%.1f%%) -- a high value means the join key is wrong, "
            "not that the archive is missing; abandon this run rather than --apply it "
            "if this looks high",
            unmatched, report.selected, pct,
        )


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Recover events.post_item_source.source_uploaded_at/"
        ".source_media_type from the archived S3 manifest for every "
        "pre-existing row. Dry-run by default.",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="write the recovered values (default: dry-run report only)",
    )
    ap.add_argument(
        "--since-id", default=None,
        help="resume: only process candidates with source id greater than this id",
    )
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))
    reader = build_manifest_reader(settings)
    if reader is None:
        logger.error(
            "no archive bucket configured (media_archive_bucket/datalake_bucket) "
            "-- cannot read manifests, refusing to run"
        )
        return 2

    try:
        report = run_backfill(venue_dao, reader, apply=args.apply, since_id=args.since_id)
    except WriteAffectedNoRows as exc:
        if exc.report is not None:
            _print_report(exc.report)
        logger.error(str(exc))
        return 3
    except ArithmeticImbalance as exc:
        if exc.report is not None:
            _print_report(exc.report)
        logger.error(str(exc))
        return 4

    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
