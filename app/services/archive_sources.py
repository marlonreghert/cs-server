"""Where archived photos come from.

One descriptor per source. A source owns four things the rest of the pipeline
must not hard-code:

  * the extra config fields it needs (rendered by the admin panel from here),
  * how it turns a venue into photos,
  * how much a run costs, because the billing models genuinely differ —
    Google charges per photo request, Apify per place scraped,
  * which dependency has to be wired for it to work at all.

Everything else — the caps, eligibility, the target prefix, the skip rules — is
source-independent and lives on the shared run config, not in here.

Adding a source is an entry in `ARCHIVE_SOURCES`. The admin panel renders the
catalog this module publishes, so a new source needs no UI change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.api.apify_gmaps_extractor_client import ApifyPollTimeoutError
from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.config import settings
from app.metrics import INSTAGRAM_ARCHIVE_IMAGES_TOTAL

logger = logging.getLogger(__name__)

SOURCE_GOOGLE_PHOTOS = "google_photos"
SOURCE_APIFY_GMAPS = "apify_gmaps_extractor"
SOURCE_SEARCHAPI_PHOTOS = "searchapi_gmaps_photos"
SOURCE_INSTAGRAM_POSTS = "instagram_posts"


class ArchiveCreditExhausted(Exception):
    """The source's own budget ran out mid-run.

    Distinct from a fetch failure: the run must stop rather than keep trying,
    and the operator needs to be told which budget, not just "it failed".
    """


class ArchiveFetchTimeout(Exception):
    """The source was still working on this venue when we stopped waiting.

    Distinct from "not found": the venue exists and the request was billed, we
    simply have no result yet. Reporting it as a no-match claimed 35 mid-scrape
    venues were absent from Google Maps.

    Distinct from a fetch failure too — it must NOT be retried by the caller's
    ladder, because the source-level retry starts a fresh billable request while
    the original is still running. Any waiting happens inside the source.
    """

    def __init__(self, message: str, last_status: str = "UNKNOWN"):
        super().__init__(message)
        self.last_status = last_status


@dataclass(frozen=True)
class ConfigField:
    """One source-specific control, rendered by the admin panel."""

    name: str
    label: str
    type: str  # "number" | "text" | "select" | "multiselect"
    default: Any = None
    help: str = ""
    options: Optional[list[str]] = None

    def to_public(self) -> dict:
        out = {
            "name": self.name, "label": self.label, "type": self.type,
            "default": self.default, "help": self.help,
        }
        if self.options:
            out["options"] = self.options
        return out


@dataclass(frozen=True)
class ArchiveSource:
    label: str
    description: str
    # Container attribute that must be wired, and what to say when it is not.
    requires_attr: str
    unavailable_reason: str
    # async (client, venue_ctx, cfg) -> list[photo dict]
    fetch: Callable[..., Any]
    # (venues, cfg) -> (billable_units, unit_label)
    estimate_units: Callable[[int, dict], tuple[int, str]]
    # (settings, cfg) -> cost in USD for one billable unit
    unit_cost_usd: Callable[..., float]
    config_schema: list[ConfigField] = field(default_factory=list)
    cost_note: str = ""
    # True when the source returns Google's own tab for a photo. That label is
    # authoritative, so the classifier must not overwrite it with a guess — an
    # attribute of the SOURCE, not an if-branch in the pipeline.
    provides_categories: bool = False

    def to_public(self, source_id: str, available: bool) -> dict:
        return {
            "id": source_id,
            "label": self.label,
            "description": self.description,
            "available": available,
            "unavailable_reason": None if available else self.unavailable_reason,
            "config_schema": [f.to_public() for f in self.config_schema],
            "cost_note": self.cost_note,
        }


# ── google_photos ────────────────────────────────────────────────────────────
async def _fetch_google(client, venue, cfg):
    """Place Details for the photo names, then one media call per photo.

    That is 1 + N billed calls per venue, which is what the cost model below
    counts — the Place Details call was previously omitted from the estimate.
    """
    place_id = venue.get("google_place_id")
    if not place_id:
        return None  # caller records `no_place_id`; costs nothing
    photos = await client.get_place_photos(
        place_id, max_photos=cfg["max_photos_per_venue"], include_ref=True
    )
    # The Places photo endpoint returns photos and nothing else, so there is no
    # extra info to keep — an empty info block, not a missing one.
    return {"photos": photos or [], "info": {"google_place_id": place_id}}


def _google_units(venues: int, cfg: dict) -> tuple[int, str]:
    # 1 Place Details + N photo-media requests, per venue.
    return venues * (1 + cfg["max_photos_per_venue"]), "Google API requests"


def _google_unit_cost(settings, cfg) -> float:
    return float(getattr(settings, "google_photo_cost_per_1k_usd", 7.0)) / 1000.0


def _as_bool(value: Any, *, default: bool) -> bool:
    """Coerce an admin-panel config value to a bool.

    A `select` field arrives as the STRING "yes"/"no", and `bool("no")` is True —
    so the off switch would silently be an on switch without this.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("no", "false", "0", "off", "")


# ── apify_gmaps_extractor ────────────────────────────────────────────────────
async def _fetch_apify(client, venue, cfg):
    """One actor run per venue; the actor bills per place, not per photo.

    The actor returns images in the order Google Maps displays them — the "All"
    tab, which is Google's own ranking — so asking for N gives the TOP N, not an
    arbitrary N. There is no category filter on either compass actor:
    `imageCategories` is output metadata listing which tabs exist, not a
    per-image label and not an input, so "only the Menu photos" is not
    expressible here.

    Requests exactly the operator's cap. An earlier version over-fetched into a
    "pool" borrowed from the menu-photo path, where a bigger pool feeds a
    downstream classifier; the archive has no such selection step, so the pool
    only meant storing more photos than the operator asked for.
    """
    query = venue.get("search_query")
    if not query:
        return None
    source_cfg = cfg.get("source_config") or {}
    try:
        result = await client.fetch_venue_photos(
            query,
            max_photos=cfg["max_photos_per_venue"],
            language=str(source_cfg.get("language") or "pt-BR"),
            scrape_image_authors=_as_bool(
                source_cfg.get("scrape_image_authors"), default=True
            ),
        )
    except ApifyPollTimeoutError as e:
        # Translated to the source-neutral type so the service stays ignorant of
        # which vendor it is talking to.
        raise ArchiveFetchTimeout(str(e), last_status=e.last_status) from e
    except ApifyCreditExhaustedError as e:
        # The client's own contract says this must stop the run, but the service
        # catches ArchiveCreditExhausted — an unrelated class. Without this
        # translation the exception fell through to the generic handler, was
        # counted as one venue's failure, and the run carried on calling into an
        # exhausted balance for every venue that remained.
        raise ArchiveCreditExhausted(str(e)) from e
    if result is None:
        return None
    # Tolerate a client that still returns a bare photo list.
    if isinstance(result, list):
        return {"photos": result, "info": {}}
    return result


def _apify_units(venues: int, cfg: dict) -> tuple[int, str]:
    # Billed per place scraped, independent of how many photos come back.
    return venues, "places scraped"


def _apify_unit_cost(settings, cfg) -> float:
    # place-scraped + the additional-details add-on that carries the images.
    return (
        float(getattr(settings, "apify_place_scraped_cost_usd", 0.004))
        + float(getattr(settings, "apify_place_details_cost_usd", 0.002))
    )


# ── searchapi_gmaps_photos ───────────────────────────────────────────────────
async def _fetch_searchapi(client, venue, cfg):
    """One request PER CATEGORY, which is exactly what it bills.

    The only source that can say which Google tab a photo came from. The engine
    returns no categories array, so the ids come from the table on the client;
    a tab a venue lacks simply yields nothing and is skipped.
    """
    place_id = venue.get("google_place_id")
    if not place_id:
        return None
    source_cfg = cfg.get("source_config") or {}
    categories = source_cfg.get("categories") or ["menu"]
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(",") if c.strip()]
    # Per CATEGORY, so the per-category cap governs when set; otherwise the
    # venue cap is the only bound and the service trims the total afterwards.
    per_category = cfg.get("max_photos_per_category") or cfg["max_photos_per_venue"]
    return await client.fetch_venue_photos(
        place_id,
        categories=categories,
        max_photos=per_category,
        hl=str(source_cfg.get("language") or "pt-BR"),
    )


def _searchapi_units(venues: int, cfg: dict) -> tuple[int, str]:
    source_cfg = cfg.get("source_config") or {}
    categories = source_cfg.get("categories") or ["menu"]
    if isinstance(categories, str):
        categories = [c for c in categories.split(",") if c.strip()]
    # One billed search per category per venue — no discovery call, because the
    # category ids are constants.
    return venues * max(1, len(categories)), "searches"


def _searchapi_unit_cost(settings, cfg) -> float:
    return float(getattr(settings, "searchapi_cost_per_1k_usd", 4.0)) / 1000.0


# ── instagram_posts ──────────────────────────────────────────────────────────
def _parse_post_timestamp(value: Any) -> Optional[datetime]:
    """A post's `timestamp` field as an aware datetime, or None when absent or
    unparseable. Used only to ORDER posts (never to gate them), so a bad value
    must sort last, never raise."""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _sort_posts_newest_first(posts: list[dict]) -> list[dict]:
    """Newest first, so a per-venue image cap always spends on the most recent
    posts. The actor's own array order is not a contract — plans/
    260806_instagram-post-recency-and-unknown-time.md observed two accounts
    scraped by the same client return their posts in a different relative
    order on the same day.

    A post with a missing or unparseable timestamp sorts LAST, stable among
    ties, so a single payload oddity can never displace a dated post out of a
    size-bounded cap — and it never raises, just falls to the back.
    """
    dated: list[tuple[datetime, dict]] = []
    undated: list[dict] = []
    for post in posts:
        ts = _parse_post_timestamp(post.get("timestamp"))
        if ts is None:
            logger.debug(
                "instagram_posts: post %s has no usable timestamp; sorted last",
                post.get("shortcode"),
            )
            undated.append(post)
        else:
            dated.append((ts, post))
    # list.sort is stable, and Python's docs guarantee reverse=True preserves
    # that stability (ties keep their original relative order) rather than
    # literally reversing the list.
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [post for _, post in dated] + undated


def _instagram_photo(
    post: dict, url: str, shortcode: Optional[str], child_index: Optional[int]
) -> dict:
    """One archivable image, carrying the post that produced it.

    The photo id is derived from the POST's identity — `<shortcode>` for a
    single image, `<shortcode>_<n>` for carousel child n — never from the url:
    Instagram signs the url with a signature that rotates on every scrape, so
    an id built from it would change every run and defeat the skip-before-spend
    gate, which depends on a photo having the same id across runs.
    """
    photo_id = (
        f"{shortcode}_{child_index}" if shortcode and child_index is not None
        else shortcode
    )
    return {
        "url": url,
        "instagram_photo_id": photo_id,
        "caption": post.get("caption"),
        "permalink": post.get("permalink"),
        "shortcode": shortcode,
        "carousel_index": child_index,
        "uploaded_at": post.get("timestamp") or None,
        "likes_count": post.get("likes_count"),
        "comments_count": post.get("comments_count"),
        "post_type": post.get("post_type"),
        # Instagram has no Google resource name; explicit None keeps
        # `photo_id_for`'s Google fallback path from tripping on a stray key.
        "photo_name": None,
    }


async def _fetch_instagram(client, venue, cfg):
    """Scrape a venue's recent posts and expand every carousel into its images.

    The handle is looked up and checked BEFORE this is ever reached — in the
    service's `_archive_venue`, mirroring the Apify Maps `no_query` check — so
    a venue without one costs nothing. This function can assume a handle is
    present; the guard below is defensive, not the cost gate itself.

    `max_photos_per_venue` bounds IMAGES here, not posts: a ten-post scrape
    where every post is a ten-image carousel is a hundred images from one
    venue if the cap only bounded posts. Expansion stops the moment the cap is
    reached rather than fetching everything and trimming, so nothing past the
    cap is ever downloaded — the loop below breaks before appending, not after.

    The scrape and the download happen in the same run: Instagram signs each
    image url with a short-lived signature, so returning urls for the caller to
    download immediately (exactly how the shared pipeline already behaves) is a
    constraint this function must preserve, not a mechanism it needs to build.
    """
    handle = venue.get("instagram_handle")
    if not handle:
        return None  # caller records `no_handle` before this is ever called

    source_cfg = cfg.get("source_config") or {}
    posts_per_venue = int(
        source_cfg.get("posts_per_venue")
        or settings.instagram_archive_posts_per_venue
    )
    include_children = _as_bool(
        source_cfg.get("include_carousel_children"), default=True
    )
    cap = cfg["max_photos_per_venue"]

    try:
        posts = await client.fetch_recent_posts(handle, results_limit=posts_per_venue)
    except ApifyCreditExhaustedError as e:
        # Same translation the Maps source needed: without it this exception
        # falls through as one venue's failure and the run keeps calling into
        # an exhausted balance for every venue that remains.
        raise ArchiveCreditExhausted(str(e)) from e

    # The actor makes no ordering promise (see the module-level docstring
    # above `_sort_posts_newest_first`), so the cap below must not be left to
    # spend on whichever posts happened to lead the array.
    posts = _sort_posts_newest_first(posts or [])

    photos: list[dict] = []
    skipped_cap = 0
    for post in posts:
        if len(photos) >= cap:
            break
        shortcode = post.get("shortcode")
        urls = post.get("image_urls") or []
        if not urls:
            continue  # a video post, or a payload with nothing to archive
        selected = urls if include_children else urls[:1]
        is_carousel = len(selected) > 1
        for idx, url in enumerate(selected, start=1):
            if len(photos) >= cap:
                skipped_cap += 1
                continue
            child_index = idx if is_carousel else None
            photos.append(_instagram_photo(post, url, shortcode, child_index))

    if skipped_cap:
        INSTAGRAM_ARCHIVE_IMAGES_TOTAL.labels(result="skipped_cap").inc(skipped_cap)

    return {"photos": photos, "info": {}}


def _instagram_units(venues: int, cfg: dict) -> tuple[int, str]:
    # Billed per RESULT ITEM (post) scraped — the Apify Instagram actor's own
    # unit, unlike the Maps extractor, which bills per PLACE regardless of how
    # many photos come back.
    source_cfg = cfg.get("source_config") or {}
    posts = int(
        source_cfg.get("posts_per_venue")
        or settings.instagram_archive_posts_per_venue
    )
    return venues * posts, "posts scraped"


def _instagram_unit_cost(settings_obj, cfg) -> float:
    return float(getattr(settings_obj, "apify_instagram_post_cost_usd", 0.003))


ARCHIVE_SOURCES: dict[str, ArchiveSource] = {
    SOURCE_GOOGLE_PHOTOS: ArchiveSource(
        label="Google Places API",
        description=(
            "Official API. Licensed and attributed, and the most expensive: "
            "one Place Details call plus one request per photo, per venue."
        ),
        requires_attr="google_places_client",
        unavailable_reason="Google Places API key not configured",
        fetch=_fetch_google,
        estimate_units=_google_units,
        unit_cost_usd=_google_unit_cost,
        config_schema=[],
        cost_note="Billed per request: 1 Place Details + 1 per photo, per venue.",
    ),
    SOURCE_APIFY_GMAPS: ArchiveSource(
        label="Apify Google Maps extractor",
        description=(
            "Scrapes Google Maps through the compass extractor. Roughly an "
            "order of magnitude cheaper because it bills per place, not per "
            "photo — but it is a scrape, not a licensed API."
        ),
        requires_attr="apify_gmaps_extractor_client",
        unavailable_reason="Apify API token not configured (APIFY_API_TOKEN)",
        fetch=_fetch_apify,
        estimate_units=_apify_units,
        unit_cost_usd=_apify_unit_cost,
        config_schema=[
            ConfigField(
                name="language", label="Result language", type="text",
                default="pt-BR",
                help="Passed to the actor as the Google Maps locale.",
            ),
            ConfigField(
                name="scrape_image_authors", label="Fetch photo authors",
                type="select", default="yes", options=["yes", "no"],
                help=(
                    "The actor looks up an author for EVERY image, which "
                    "dominates the run time on photo-heavy venues (1,941 images: "
                    "1,729s on vs 135s off). Turn OFF for venues that time out — "
                    "same photos, but they are all filed as by_visitor with "
                    "unknown authorship."
                ),
            ),
        ],
        cost_note=(
            "Billed per place scraped, not per photo. Photos come back in "
            "Google Maps' own display order, so the cap takes the TOP N. The "
            "per-image charge is not published by Apify, so the estimate is a "
            "floor."
        ),
    ),
    SOURCE_SEARCHAPI_PHOTOS: ArchiveSource(
        label="SearchApi Google Maps photos (by category)",
        description=(
            "The only source that knows which Google tab a photo came from — "
            "Menu, Food & drink, Vibe. Costs one search per category per venue."
        ),
        requires_attr="searchapi_photos_client",
        unavailable_reason="SearchApi key not configured (SEARCHAPI_API_KEY)",
        fetch=_fetch_searchapi,
        estimate_units=_searchapi_units,
        unit_cost_usd=_searchapi_unit_cost,
        config_schema=[
            ConfigField(
                name="categories", label="Photo categories", type="multiselect",
                default=["menu"],
                options=["menu", "food_drink", "vibe", "latest", "all"],
                help="Each category is a separate billed search per venue. A "
                     "venue that lacks a tab is skipped, not failed. `all` is "
                     "the unfiltered view — it catches photos the named tabs "
                     "miss, including place-specific ones like \"Risotto\" "
                     "whose ids cannot be enumerated.",
            ),
            ConfigField(
                name="language", label="Result language", type="text",
                default="pt-BR", help="Passed to the engine as `hl`.",
            ),
        ],
        cost_note=(
            "Billed per search: one per category, per venue. No discovery call "
            "— the category ids are constants."
        ),
        provides_categories=True,
    ),
    SOURCE_INSTAGRAM_POSTS: ArchiveSource(
        label="Instagram posts (Apify)",
        description=(
            "Scrapes a venue's recent Instagram feed posts and archives every "
            "image, expanding carousels into one object per child. A venue "
            "needs a confirmed Instagram handle; without one it costs nothing."
        ),
        requires_attr="apify_instagram_client",
        unavailable_reason="Apify API token not configured (APIFY_API_TOKEN)",
        fetch=_fetch_instagram,
        estimate_units=_instagram_units,
        unit_cost_usd=_instagram_unit_cost,
        config_schema=[
            ConfigField(
                name="posts_per_venue", label="Recent posts per venue",
                type="number", default=settings.instagram_archive_posts_per_venue,
                help="How many of the venue's most recent posts to scrape. "
                     "Each post scraped is the Apify actor's billed unit, "
                     "independent of how many images it carries.",
            ),
            ConfigField(
                name="include_carousel_children",
                label="Archive carousel children", type="select",
                default="yes", options=["yes", "no"],
                help="Turn OFF to archive only a carousel's cover image. "
                     "Either way, the per-venue photo cap bounds IMAGES, "
                     "including carousel children — a run never downloads "
                     "past it.",
            ),
        ],
        cost_note=(
            "Billed per post scraped, not per image — a ten-image carousel "
            "costs the same as a single-image post. The per-post price is not "
            "published, so the estimate is a floor, like the Maps extractor's."
        ),
        # Instagram returns no per-image category; every photo goes through
        # the classifier, which is what lets a poster be filed as `flyer`.
        provides_categories=False,
    ),
}

SUPPORTED_SOURCES = tuple(ARCHIVE_SOURCES)


def get_source(source_id: str) -> ArchiveSource:
    try:
        return ARCHIVE_SOURCES[source_id]
    except KeyError:
        raise KeyError(source_id)


def public_catalog(container) -> list[dict]:
    """The catalog the admin panel renders.

    Availability is resolved from the SERVICE that actually dispatches, not
    from the container. They name the same client differently — the container
    calls it `google_places_api`, the service `google_places_client` — so
    asking the container reported Google as unavailable while runs against it
    worked fine. Whatever answers the fetch must answer the availability
    question too.
    """
    holder = getattr(container, "venue_photo_archive_service", None) or container
    out = []
    for source_id, source in ARCHIVE_SOURCES.items():
        available = getattr(holder, source.requires_attr, None) is not None
        out.append(source.to_public(source_id, available))
    return out
