"""Instagram discovery models for venue Instagram profile matching."""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class InstagramSearchResult(BaseModel):
    """A single result from Apify instagram-search-scraper."""
    username: str
    full_name: Optional[str] = None
    biography: Optional[str] = None
    is_verified: Optional[bool] = None
    follower_count: Optional[int] = None


class InstagramProfile(BaseModel):
    """Full profile data from Apify instagram-profile-scraper."""
    username: str
    full_name: Optional[str] = None
    biography: Optional[str] = None
    external_url: Optional[str] = None
    followers_count: Optional[int] = None
    following_count: Optional[int] = None
    is_business_account: Optional[bool] = None
    business_category_name: Optional[str] = None
    is_verified: Optional[bool] = None


class InstagramValidationResult(BaseModel):
    """Result of validating an Instagram profile against a venue."""
    username: str
    confidence_score: float
    signals: dict[str, float] = Field(default_factory=dict)
    is_match: bool = False


class VenueInstagram(BaseModel):
    """Cached Instagram discovery result for a venue.

    Stored in Redis at key: venue_instagram_v1:{venue_id}
    """
    venue_id: str
    instagram_handle: Optional[str] = None
    instagram_url: Optional[str] = None
    confidence_score: float = 0.0
    status: str = "not_found"  # "found" | "not_found" | "low_confidence"
    bio: Optional[str] = None
    followers_count: Optional[int] = None
    is_business_account: Optional[bool] = None
    business_category: Optional[str] = None
    # Which cascade tier produced this handle. Promoted to a real column on
    # instagram.handle because "how many came free vs paid" is the first
    # question this feature has to answer, and jsonb extraction across the
    # catalog is exactly the query that gets written wrong.
    source: Optional[str] = None
    # Evidence behind the decision: signals, probe verdict, name similarity and
    # the judge's mode/reason. Rides in `payload`, no migration needed.
    discovery: Optional[dict] = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)

    def has_instagram(self) -> bool:
        """Check if a valid Instagram handle was found."""
        return self.status in ("found", "low_confidence") and self.instagram_handle is not None


class InstagramPost(BaseModel):
    """A single Instagram post.

    Caption/engagement fields are cached indefinitely in `instagram.posts` —
    they never expire. `image_urls` is the exception: Instagram signs those
    urls with a short-lived signature, so they are only ever meaningful within
    the same scrape run that produced them (the media archive pipeline
    downloads them immediately) and are never read back from this cached
    payload days later.
    """
    caption: Optional[str] = None
    likes_count: int = 0
    comments_count: int = 0
    timestamp: Optional[str] = None
    post_type: str = "image"  # image | video | carousel
    # Additive — existing cached rows simply lack these, and every reader must
    # tolerate that (no migration; these ride in the existing payload jsonb).
    shortcode: Optional[str] = None
    permalink: Optional[str] = None
    # The main image plus every carousel child, in display order. Empty for a
    # video post or a payload that predates this field.
    image_urls: list[str] = Field(default_factory=list)


class VenueInstagramPosts(BaseModel):
    """Cached Instagram posts for a venue.

    Stored in Redis at key: venue_ig_posts_v1:{venue_id}
    """
    venue_id: str
    instagram_handle: str
    posts: list[InstagramPost] = []
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
