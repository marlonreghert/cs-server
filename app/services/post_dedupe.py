"""Generic shortcode dedupe, shared by every caller that has to merge more
than one candidate list of "the same post" into one.

Extracted from `instagram_crawl_service.dedupe_posts_by_shortcode`
(plans/260810_stream-dedupe-and-venue-attribution.md §A: the posts and reels
streams overlap — a reel is also a grid post — so the same shortcode can
arrive from two streams and must be processed once). plans/260811_extract-
by-handle.md §A needs the identical mechanism for a different overlap: the
SAME archived post can be found under two archive prefixes (`promoter=
<handle>` and a mapped venue's `venue_id=`), and re-extracting it twice would
re-spend an OpenAI call for nothing.

Homed in its own module (not in `instagram_crawl_service.py`, which imports
FROM `event_extraction_service.py`) so `EventPostSource.posts_for_handle`
can reuse it too without a circular import — see that module's own note.
"""
from __future__ import annotations

from typing import Callable, Optional

# The default tie-break (§A's original rule): more images first, then a
# longer caption. Only reached when two DIFFERING copies of the same
# shortcode collide — §Evidence found every duplicated shortcode resolves to
# one byte-identical S3 key in practice, so this rarely has to decide
# anything.
def post_richness(post: dict) -> tuple:
    return (len(post.get("image_urls") or []), len(post.get("caption") or ""))


def dedupe_by_shortcode(
    post_groups: list[list[dict]],
    *,
    prefer: Optional[Callable[[dict, dict], bool]] = None,
) -> list[dict]:
    """Merge `post_groups` (given lowest-priority first) into one list keyed
    by `shortcode`, keeping the first-seen entry unless a later group's copy
    is PREFERRED per `prefer(candidate, current) -> bool` (defaults to
    `post_richness`, §A's original rule — a later group's copy wins only
    when it is strictly richer).

    Deterministic regardless of a single group's own internal order: groups
    are always considered in the CALLER-SUPPLIED order, never re-sorted here.

    A post with no shortcode cannot be deduplicated (nothing to key it on)
    and is always kept, from every group.
    """
    prefer_fn = prefer or (lambda candidate, current: post_richness(candidate) > post_richness(current))
    best_by_shortcode: dict[str, dict] = {}
    order: list[str] = []
    unkeyable: list[dict] = []
    for group in post_groups:
        for post in group:
            shortcode = post.get("shortcode")
            if not shortcode:
                unkeyable.append(post)
                continue
            if shortcode not in best_by_shortcode:
                best_by_shortcode[shortcode] = post
                order.append(shortcode)
            elif prefer_fn(post, best_by_shortcode[shortcode]):
                best_by_shortcode[shortcode] = post
    return [best_by_shortcode[sc] for sc in order] + unkeyable


__all__ = ["dedupe_by_shortcode", "post_richness"]
