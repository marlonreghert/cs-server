"""Resolve every archived image behind one event's announcing posts, for
GET /admin/events/{event_id}/media. See plans/260813_event-source-media.md.

Every image resolved here is addressed from the event's OWN
`post_item_source` rows — specifically each source's stored
`cover_photo_key`, which already encodes the run/venue (or run/promoter)
partition it was archived under. Nothing here ever accepts a key from a
caller (the plan's own pinned security property: "Never sign a key supplied
by the caller") and nothing here calls `MediaArchiveStore.list_run_prefixes`
— that is the crawl-time batch scan
(`event_extraction_service.EventPostSource._manifests_since`); driving it
from this interactive endpoint would read every archived run to serve one
event, exactly what the plan forbids.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.metrics import EVENT_MEDIA_IMAGE_SIGN_TOTAL

logger = logging.getLogger(__name__)

# The run prefix + venue/promoter partition segment of an archived key, e.g.
#   retrieved/source=instagram_posts/year=2026/month=08/day=07/
#   run_id=01J.../venue_id=abc123/media/flyer/Dbs1FdsEWr7_1.jpg
# `MediaArchiveStore`'s own module docstring documents this layout; this
# regex runs it in reverse, recovering the manifest's own address (`prefix`
# + `venue_id`/`promoter`) from a key already stored on a source row, so no
# listing call is ever needed to find it.
_PARTITION_RE = re.compile(
    r"^(?P<prefix>.*run_id=[^/]+/)(?P<kind>venue_id|promoter)=(?P<value>[^/]+)/"
)

# Sentinel for a source with no recorded publish timestamp — sorts AFTER
# every source that has one (see `sort_sources_by_published`), never before.
_UNKNOWN_PUBLISHED_SENTINEL = datetime.max.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RunPartition:
    prefix: str
    kind: str  # "venue_id" | "promoter"
    value: str


def derive_run_partition(key: Optional[str]) -> Optional[RunPartition]:
    """The run prefix + venue/promoter partition a stored key was archived
    under, or None when `key` is empty or does not match the archive's own
    layout — a legacy or malformed key must degrade to the fallback path,
    never raise.
    """
    if not key:
        return None
    m = _PARTITION_RE.match(key)
    if not m:
        return None
    return RunPartition(prefix=m.group("prefix"), kind=m.group("kind"), value=m.group("value"))


def entries_for_shortcode(manifest: Optional[dict], shortcode: Optional[str]) -> list[dict]:
    """Manifest photo entries belonging to one post.

    Matched by the manifest's own `shortcode` field, which is ALWAYS the
    clean shortcode with no ordinal suffix
    (`instagram_crawl_service._archive_venue_posts` and
    `promoter_crawl_service._archive_post_images` both write it that way,
    reserving the `_1`/`_2` suffix for the ARCHIVED FILENAME alone, to keep
    a carousel's images from overwriting each other in S3). Matching here
    never parses that suffix back off the key — it trusts the manifest's own
    field, exactly like `event_extraction_service.EventPostSource.
    _bucket_entries` already does.
    """
    if not manifest or not shortcode:
        return []
    return [
        entry for entry in (manifest.get("photos") or [])
        if entry.get("shortcode") == shortcode and entry.get("key")
    ]


def images_from_entries(entries: list[dict], read_key: Optional[str]) -> list[dict]:
    """One `{key, category, confidence, read_by_extractor}` dict per manifest
    entry. `read_key` is the source's own stored `cover_photo_key` — exactly
    `flyer_photo_key or any_photo_key` as recorded at extraction time
    (`event_extraction_service.py:809`), so the flag is a recorded fact
    rather than a re-derivation that could drift from what the model
    actually saw. Exactly one entry matches when `read_key` is present among
    them; none match when `read_key` is None (nothing was ever extracted
    from this post) or points at a key the manifest no longer lists.
    """
    return [
        {
            "key": entry["key"],
            "category": entry.get("category"),
            "confidence": entry.get("classification_confidence"),
            "read_by_extractor": entry["key"] == read_key,
        }
        for entry in entries
    ]


def sort_sources_by_published(sources: list[dict]) -> list[dict]:
    """Oldest post first, by the post's own `source_uploaded_at` (when it
    was actually published) — not `first_seen_at` (when the crawler first
    saw it), which can lag or reorder relative to publish time. A source
    with no recorded upload timestamp (archives that predate this field)
    sorts after every source that has one; the sort is stable, so those ties
    keep `list_event_sources`'s own first_seen_at-ascending order rather
    than being given an invented position.
    """
    return sorted(
        sources, key=lambda s: s.get("source_uploaded_at") or _UNKNOWN_PUBLISHED_SENTINEL,
    )


async def _read_partition_manifest(media_store, partition: RunPartition, cache: dict) -> Optional[dict]:
    """One manifest read per distinct (kind, prefix, value) per request —
    plan §B: "a merged item's sources usually share one run." Never raises:
    a read failure is reported as None, exactly like
    `MediaArchiveStore.read_manifest`'s own contract, so the caller's
    fallback is the only path that has to reason about failure.
    """
    cache_key = (partition.kind, partition.prefix, partition.value)
    if cache_key in cache:
        return cache[cache_key]
    try:
        if partition.kind == "venue_id":
            manifest = await media_store.read_manifest(partition.prefix, partition.value)
        else:
            manifest = await media_store.read_promoter_manifest(partition.prefix, partition.value)
    except Exception as e:
        logger.warning(f"[EventSourceMedia] manifest read failed for {partition.prefix}: {e}")
        manifest = None
    cache[cache_key] = manifest
    return manifest


def _fallback_to_cover(cover_key: Optional[str]) -> list[dict]:
    if not cover_key:
        return []
    return [{"key": cover_key, "category": None, "confidence": None, "read_by_extractor": True}]


async def _resolve_source_images(media_store, source: dict, cache: dict) -> tuple[list[dict], bool]:
    """`(images, used_fallback)` for ONE source. `images` carry plain
    (unsigned) keys — presigning happens once, across every source, in
    `resolve_event_media` below.
    """
    cover_key = source.get("cover_photo_key")
    shortcode = source.get("source_shortcode")
    partition = derive_run_partition(cover_key)

    if partition is not None:
        manifest = await _read_partition_manifest(media_store, partition, cache)
        if manifest is not None:
            # The manifest itself was read fine — whatever it does or does
            # not say about this shortcode is the real answer, not a
            # "cannot be read" case. A manifest that happens to have
            # nothing for this shortcode legitimately yields zero images
            # here (images_from_entries([]) == []), rather than inventing a
            # cover-key fallback no scenario asks for.
            entries = entries_for_shortcode(manifest, shortcode)
            return images_from_entries(entries, cover_key), False

    # Either the key didn't match the archive's own layout, or the manifest
    # genuinely could not be read — sign the source's stored cover key
    # alone (plan §Implementation Approach: "Fallback when the manifest is
    # missing or unreadable ... Count this fallback").
    return _fallback_to_cover(cover_key), True


async def resolve_event_media(media_store, sources: list[dict], *, presign_expires_in: int) -> list[dict]:
    """Every archived image for `sources` (one event's `post_item_source`
    rows), oldest-published first, each image signed — ready for the router
    to wrap directly into `EventSourceMediaOut` models.

    Each returned dict also carries `used_fallback` (whether THIS source's
    manifest could not be read), which is not part of the response shape —
    the router reads it to classify the overall response outcome for
    `EVENT_MEDIA_TOTAL`, and it is silently dropped when the router builds
    the Pydantic model from the rest of the dict (the same "extra keys
    ignored" convention `admin_events_router._to_out` already relies on for
    `EventSourceOut(**s)`).
    """
    ordered = sort_sources_by_published(sources)
    manifest_cache: dict[tuple[str, str, str], Optional[dict]] = {}
    out: list[dict] = []
    for source in ordered:
        images, used_fallback = await _resolve_source_images(media_store, source, manifest_cache)
        signed_images = []
        for image in images:
            url = await media_store.presign(image["key"], expires_in=presign_expires_in)
            if not url:
                # MediaArchiveStore.presign() returns None rather than
                # raising on failure — omit the image, count it, and keep
                # going: "it must not fail the whole response and lose the
                # images that did sign" (plan Error Handling).
                EVENT_MEDIA_IMAGE_SIGN_TOTAL.labels(result="failed").inc()
                continue
            EVENT_MEDIA_IMAGE_SIGN_TOTAL.labels(result="signed").inc()
            signed_images.append({
                "url": url, "category": image["category"], "confidence": image["confidence"],
                "read_by_extractor": image["read_by_extractor"],
            })
        out.append({
            "source_shortcode": source.get("source_shortcode"),
            "source_handle": source.get("source_handle"),
            "source_permalink": source.get("source_permalink"),
            "source_media_type": source.get("source_media_type"),
            "source_uploaded_at": source.get("source_uploaded_at"),
            "used_fallback": used_fallback,
            "images": signed_images,
        })
    return out
