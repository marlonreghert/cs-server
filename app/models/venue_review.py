"""Venue review models for Google Places API review data."""
from typing import Optional
from pydantic import BaseModel


class VenueReview(BaseModel):
    """A single user review from Google Places."""
    author_name: str
    rating: int
    text: str
    relative_time: str
    language: Optional[str] = None
    publish_time: Optional[str] = None


class VenueReviews(BaseModel):
    """Collection of reviews for a venue."""
    venue_id: str
    reviews: list[VenueReview] = []
