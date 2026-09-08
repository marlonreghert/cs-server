"""Operator CLI: read the RAW ARCHIVED CAPTION behind every live event and,
optionally, ask the deployed extraction prompt what `kind` each one is.

See plans/260907_events-non-event-and-recurrence-normalisation.md — §4
Path B0 (the production proof) and the Test Plan's fixture builder. It is
ONE script with two modes because they need exactly the same caption
resolution, and a second copy of that resolution is how the proof and the
fixture would come to disagree about what the model actually saw.

## It has no write path

This script constructs NO DAO for writing, calls NO `reconcile_post_events`,
and issues no `UPDATE`/`INSERT` of any kind. It reads RDS (`events.post_item`
and `events.post_item_source`), reads S3 under `retrieved/` and — only in
`--ask-model` mode — calls OpenAI. That is deliberate and load-bearing: the
proof this script performs used to be a write-path re-extraction that had
authority over the very rows it was measuring, at handles that also host
genuine recurring nights (`Sambinha Downtown`). A misfire would have
deprojected them within one 2-minute cycle. It cannot misfire through a write
path it does not have.

## How a caption is found without a listing scan

Captions are NOT in RDS: `post_item_source` has no caption column, and
`raw_extraction` holds the model's parsed ANSWER, not its input. They ARE in
the archive:

    post_item_source.cover_photo_key
      -> event_source_media.derive_run_partition  (run prefix + partition)
        -> retrieved/.../info/_manifest.json
          -> the entry whose `shortcode` matches, and its `caption`

That `caption` field is byte-for-byte what
`event_extraction_service.EventPostSource._bucket_entries` reads at
ingestion, so the eval sees exactly what the model saw.

## The fixture's expected label is NOT filled in here

`--emit-fixture` writes `expected_kind: null` on purpose, alongside
`census_kind` (what the row's `post_type` says today). **A human reads each
caption and fills `expected_kind` in from the CAPTION TEXT alone before the
fixture is checked in.** Taking the label from the census would be circular:
the census is post-extraction prose the previous run itself produced, and the
`menu`/`promotion` labels on the two known offenders are the entire
prediction this round rests on. A caption that does not support its
census-derived label is a FINDING that re-opens the acceptance table, not a
fixture bug to be quietly relabelled — which is why both values are stored,
so any divergence shows up in the diff.

## Usage

    # 1. Build the caption fixture for the live corpus (no model call, no spend)
    python -m scripts.eval_kind_on_captions --emit-fixture out.json

    # 2. Path B0: the production proof, at the two handles under correction
    python -m scripts.eval_kind_on_captions \
        --handles ctradicao downtownbeergarden_ --ask-model

Run it inside the production container over SSM (ship it to /app, never
/tmp — see docs' prod verification playbook). `--ask-model` spends one
OpenAI call per post and nothing else.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Optional

from app.api.openai_event_extraction_client import (
    OpenAIEventExtractionClient,
    parse_extraction_response,
)
from app.config import settings
from app.dao.media_archive_store import MediaArchiveStore
from app.dao.rds_venue_store import RdsVenueStore
from app.models.event_kind import normalize_kind
from app.services.event_source_media import derive_run_partition


async def _manifest_for(archive: MediaArchiveStore, cover_photo_key: Optional[str]):
    """The manifest a stored `cover_photo_key` was archived alongside, or
    None. Addressed straight from the key — never via
    `MediaArchiveStore.list_run_prefixes`, which would read every archived
    run to serve one event."""
    partition = derive_run_partition(cover_photo_key)
    if partition is None:
        return None
    if partition.kind == "promoter":
        return await archive.read_promoter_manifest(partition.prefix, partition.value)
    return await archive.read_manifest(partition.prefix, partition.value)


def _caption_from(manifest: Optional[dict], shortcode: Optional[str]) -> Optional[str]:
    """The post's own caption out of the manifest, matched on the manifest's
    own `shortcode` field — the SAME field, and the same matching,
    `EventPostSource._bucket_entries` uses (never parsing the `_1`/`_2`
    ordinal suffix back off the archived filename)."""
    if not manifest or not shortcode:
        return None
    for entry in manifest.get("photos") or []:
        if entry.get("shortcode") == shortcode and entry.get("caption"):
            return entry.get("caption")
    return None


async def collect_rows(
    store: RdsVenueStore, archive: MediaArchiveStore, handles, *, all_statuses=False,
) -> list[dict]:
    """One record per live event, carrying its raw caption. Read-only —
    every branch below is a SELECT.

    The DEFAULT source is `list_events_for_projection`, i.e. exactly the rows
    the serving projection selects right now. That is the corpus the plan's
    census counted and the corpus the acceptance table is written against —
    `list_events(status=None)` would drag in every `pending_review`,
    `rejected` and superseded row ever written, none of which is in the feed.
    `--all-statuses` asks for that wider read explicitly.
    """
    if handles:
        events: list[dict] = []
        seen: set[str] = set()
        for handle in handles:
            for row in store.list_events_by_handle(handle):
                if row["event_id"] not in seen:
                    seen.add(row["event_id"])
                    events.append(row)
    elif all_statuses:
        events = store.list_events(status=None)
    else:
        events = store.list_events_for_projection(now=datetime.now(timezone.utc))

    out: list[dict] = []
    for row in events:
        caption = None
        shortcode = None
        for source in store.list_event_sources(row["event_id"]):
            shortcode = source.get("source_shortcode")
            manifest = await _manifest_for(archive, source.get("cover_photo_key"))
            caption = _caption_from(manifest, shortcode)
            if caption:
                break
        out.append({
            "event_id": row["event_id"],
            "handle": row.get("source_handle"),
            "shortcode": shortcode,
            "title": row.get("title"),
            "caption": caption,
            # What the row says TODAY — context for the human labeller, and
            # never the answer. See the module docstring.
            "census_kind": row.get("post_type"),
            # Filled in by a human, from the caption text alone.
            "expected_kind": None,
            "synthetic": False,
        })
    return out


async def ask_model(rows: list[dict]) -> list[dict]:
    """Send each caption (and nothing else) through the DEPLOYED prompt.

    The flyer IMAGE is deliberately not sent: a post whose only event
    evidence is on the flyer is judged more harshly here than in production,
    which biases the eval toward FINDING false positives — the safe
    direction — and the standing-offer posts this round is about are
    caption-only anyway.
    """
    if not settings.openai_api_key:
        raise SystemExit("openai_api_key is not configured; cannot --ask-model")
    client = OpenAIEventExtractionClient(api_key=settings.openai_api_key)
    try:
        for row in rows:
            if not row.get("caption"):
                row["model_kind"] = None
                row["model_error"] = "no caption archived"
                continue
            try:
                raw = await client.extract(caption=row["caption"])
                row["model_kind"] = normalize_kind(
                    parse_extraction_response(raw).get("kind")
                )
                row["model_error"] = None
            except Exception as e:  # noqa: BLE001 - reported, never fatal
                row["model_kind"] = None
                row["model_error"] = str(e)
    finally:
        await client.close()
    return rows


def _report(rows: list[dict]) -> int:
    """Prints `shortcode -> kind` and returns a process exit code: non-zero
    when a row carrying a human-assigned `expected_kind` disagrees with the
    model. A row with no `expected_kind` is REPORTED, never gated — that is
    what makes `Oktoberfest BeerDock` observable without re-erecting the
    boundary §0 removed."""
    failures = 0
    for row in sorted(rows, key=lambda r: (r.get("handle") or "", r.get("shortcode") or "")):
        expected = row.get("expected_kind")
        actual = row.get("model_kind")
        verdict = "-"
        if expected is not None:
            if actual == expected:
                verdict = "ok"
            else:
                verdict = "MISMATCH"
                failures += 1
        print(
            f"{row.get('handle') or '?':<24} {row.get('shortcode') or '?':<14} "
            f"census={row.get('census_kind') or '?':<10} "
            f"expected={expected or '-':<10} model={actual or '-':<10} {verdict}"
        )
        if row.get("model_error"):
            print(f"    error: {row['model_error']}")
    print(f"\n{len(rows)} rows, {failures} mismatch(es) against a human-assigned label")
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--handles", nargs="*", default=None,
        help="restrict to these source handles (Path B0). Omit for the whole corpus.",
    )
    parser.add_argument(
        "--emit-fixture", metavar="PATH",
        help="write the caption fixture (expected_kind left null) and stop.",
    )
    parser.add_argument(
        "--ask-model", action="store_true",
        help="call the DEPLOYED prompt once per caption. Spends OpenAI budget.",
    )
    parser.add_argument(
        "--all-statuses", action="store_true",
        help="read EVERY event row rather than only the projection-selected "
             "corpus. Still read-only; just much wider.",
    )
    parser.add_argument(
        "--fixture", metavar="PATH",
        help="score an already-labelled fixture instead of reading the archive.",
    )
    args = parser.parse_args(argv)

    if args.fixture:
        rows = json.loads(open(args.fixture, encoding="utf-8").read())
    else:
        # Exactly how app.container builds both, so the script reads the
        # same database and the same bucket the running service does.
        store = RdsVenueStore(settings.rds_sqlalchemy_url)
        archive = MediaArchiveStore(
            bucket=settings.media_archive_bucket or settings.datalake_bucket,
            region=settings.datalake_region,
            access_key_id=settings.datalake_access_key_id or None,
            secret_access_key=settings.datalake_secret_access_key or None,
        )
        rows = asyncio.run(collect_rows(
            store, archive, args.handles, all_statuses=args.all_statuses,
        ))

    if args.emit_fixture:
        with open(args.emit_fixture, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        missing = sum(1 for r in rows if not r.get("caption"))
        print(
            f"wrote {len(rows)} rows to {args.emit_fixture} "
            f"({missing} with no archived caption). "
            "Every expected_kind is null: fill each one in FROM THE CAPTION "
            "before checking the fixture in."
        )
        return 0

    if args.ask_model:
        rows = asyncio.run(ask_model(rows))
    return _report(rows)


if __name__ == "__main__":
    sys.exit(main())
