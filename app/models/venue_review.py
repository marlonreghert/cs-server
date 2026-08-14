"""Venue review models: the free Google Places review (VenueReviews, at most
5, untouched by the deep-review-corpus feature) and the paid deep corpus
(VenueReviewsDeep) — see plans/260813_deep-review-corpus.md."""
from typing import Optional
from pydantic import BaseModel


class VenueReview(BaseModel):
    """A single user review, from either Google Places enrichment (the
    original 5) or the deep-review-corpus Apify crawl.

    `review_id` and `source` are both optional and both NEW: every review
    stored before this feature existed has neither, and must keep validating
    unchanged. `review_id` is the actor's own id when it supplies one (the
    preferred dedup key); `source` distinguishes `google_places` (the
    original 5, unset/None for pre-existing rows) from `apify_gmaps` (the
    deep-crawl corpus).
    """
    author_name: str
    rating: int
    text: str
    relative_time: str
    language: Optional[str] = None
    publish_time: Optional[str] = None
    review_id: Optional[str] = None
    source: Optional[str] = None


class VenueReviews(BaseModel):
    """Collection of reviews for a venue — the free Google Places path (at
    most 5). Untouched by the deep-review-corpus feature: byte-identical
    before and after a deep crawl for the same venue."""
    venue_id: str
    reviews: list[VenueReview] = []


class VenueReviewsDeep(BaseModel):
    """The deep review corpus for one venue — a separate enrichment family
    (`venues.reviews_deep`) from `VenueReviews` above, with its own RDS table
    and its own Redis key (`venue_reviews_deep_v1:{venue_id}`).

    RDS (via VenueRepository) always holds the FULL set this model carries;
    the Redis serving projection carries only a bounded, newest-first slice
    (`RedisVenueDAO.set_venue_reviews_deep`) — this model itself is agnostic
    to which slice it holds, so both sides can (de)serialize it identically.

    `truncated` means the PER-VENUE CAP bound the last fetch (there could be
    more in-window reviews than were captured) — distinct from the window
    simply having no more reviews to offer. `oldest_publish_time` /
    `newest_publish_time` are the extremes of `reviews` as stored, kept
    denormalized so a caller (the run summary, an admin panel) never has to
    rescan the list to answer "how fresh is this corpus".
    """
    venue_id: str
    reviews: list[VenueReview] = []
    window_days: int
    fetched_at: str
    oldest_publish_time: Optional[str] = None
    newest_publish_time: Optional[str] = None
    truncated: bool = False
