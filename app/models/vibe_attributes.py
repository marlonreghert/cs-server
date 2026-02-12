"""Vibe attributes models for venue atmosphere data from Google Places API."""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class VibeAttribute(BaseModel):
    """Single vibe attribute with name and optional boolean value."""
    name: str
    value: bool = True
    source: str = "google_places"  # Could be "google_places", "user_submitted", etc.


class VibeAttributes(BaseModel):
    """Collection of vibe attributes for a venue.

    These attributes come from Google Places API (New) and describe
    the atmosphere and crowd characteristics of a venue.
    """
    venue_id: str

    # LGBTQ+ Related
    lgbtq_friendly: Optional[bool] = None
    transgender_safespace: Optional[bool] = None

    # Crowd & Social
    good_for_groups: Optional[bool] = None
    good_for_kids: Optional[bool] = None
    good_for_working: Optional[bool] = None  # Laptop-friendly

    # Pet Related
    allows_dogs: Optional[bool] = None

    # Accessibility
    wheelchair_accessible_entrance: Optional[bool] = None
    wheelchair_accessible_seating: Optional[bool] = None
    wheelchair_accessible_restroom: Optional[bool] = None

    # Atmosphere
    live_music: Optional[bool] = None
    outdoor_seating: Optional[bool] = None
    rooftop: Optional[bool] = None

    # Service Style
    reservable: Optional[bool] = None
    serves_breakfast: Optional[bool] = None
    serves_brunch: Optional[bool] = None
    serves_lunch: Optional[bool] = None
    serves_dinner: Optional[bool] = None
    serves_vegetarian_food: Optional[bool] = None
    serves_beer: Optional[bool] = None
    serves_wine: Optional[bool] = None
    serves_cocktails: Optional[bool] = None

    # AI-Generated Summary (from Google's Generative AI)
    generative_summary: Optional[str] = None

    # Metadata
    last_updated: Optional[datetime] = Field(default_factory=datetime.utcnow)

    def get_vibe_labels(self) -> list[str]:
        """Return a list of human-readable vibe labels that are True."""
        labels = []

        if self.lgbtq_friendly:
            labels.append("LGBTQ+ Friendly")
        if self.transgender_safespace:
            labels.append("Transgender Safespace")
        if self.good_for_groups:
            labels.append("Good for Groups")
        if self.good_for_kids:
            labels.append("Family Friendly")
        if self.good_for_working:
            labels.append("Work Friendly")
        if self.allows_dogs:
            labels.append("Pet Friendly")
        if self.live_music:
            labels.append("Live Music")
        if self.outdoor_seating:
            labels.append("Outdoor Seating")
        if self.rooftop:
            labels.append("Rooftop")
        if self.serves_vegetarian_food:
            labels.append("Vegetarian Options")
        if self.serves_cocktails:
            labels.append("Cocktails")

        return labels


class GooglePlacesDetailsResponse(BaseModel):
    """Parsed response from Google Places API (New) for vibe-related fields."""
    place_id: str
    display_name: Optional[str] = None

    # Business status from Google Places API
    # Values: OPERATIONAL, CLOSED_TEMPORARILY, CLOSED_PERMANENTLY
    business_status: Optional[str] = None

    # Boolean attributes from the API
    allows_dogs: Optional[bool] = None
    good_for_children: Optional[bool] = None
    good_for_groups: Optional[bool] = None
    good_for_watching_sports: Optional[bool] = None
    live_music: Optional[bool] = None
    outdoor_seating: Optional[bool] = None
    reservable: Optional[bool] = None
    restroom: Optional[bool] = None
    serves_beer: Optional[bool] = None
    serves_breakfast: Optional[bool] = None
    serves_brunch: Optional[bool] = None
    serves_cocktails: Optional[bool] = None
    serves_coffee: Optional[bool] = None
    serves_dinner: Optional[bool] = None
    serves_lunch: Optional[bool] = None
    serves_vegetarian_food: Optional[bool] = None
    serves_wine: Optional[bool] = None

    # Accessibility
    wheelchair_accessible_entrance: Optional[bool] = None
    wheelchair_accessible_parking: Optional[bool] = None
    wheelchair_accessible_restroom: Optional[bool] = None
    wheelchair_accessible_seating: Optional[bool] = None

    # Generative AI summary
    generative_summary: Optional[str] = None

    # Editorial summary (human-written)
    editorial_summary: Optional[str] = None

    # Opening hours (from Google Places API)
    # weekday_descriptions: Pre-formatted strings like "Segunda-feira: 20:00 – 03:00"
    weekday_descriptions: Optional[list[str]] = None
    # open_now: Current status from currentOpeningHours
    open_now: Optional[bool] = None
    # special_days: Secondary opening hours for holidays (pre-formatted)
    special_days: Optional[list[str]] = None

    def is_permanently_closed(self) -> bool:
        """Check if the place is permanently closed."""
        return self.business_status == "CLOSED_PERMANENTLY"

    def is_temporarily_closed(self) -> bool:
        """Check if the place is temporarily closed."""
        return self.business_status == "CLOSED_TEMPORARILY"

    def is_operational(self) -> bool:
        """Check if the place is operational (open)."""
        return self.business_status == "OPERATIONAL" or self.business_status is None
