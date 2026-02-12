"""Opening hours model for venue business hours from Google Places API."""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class OpeningHours(BaseModel):
    """Opening hours for a venue.

    Uses Google Places API pre-formatted weekday descriptions which:
    - Handle crossing midnight correctly (e.g., "Domingo: 22:00 – 04:00")
    - Are already localized to Portuguese (pt-BR)
    - Require no manual formatting
    """

    venue_id: str

    # Primary display data (human-readable, handles crossing midnight)
    # Example: ["Domingo: Fechado", "Segunda-feira: 20:00 – 03:00", ...]
    weekday_descriptions: list[str] = Field(default_factory=list)

    # Current status from Google (already timezone-aware)
    open_now: Optional[bool] = None

    # Special hours for holidays (optional)
    # Example: ["25 de dezembro: Fechado", "31 de dezembro: 20:00 – 02:00"]
    special_days: Optional[list[str]] = None

    # Metadata
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    def get_today_hours(self, day_index: int) -> Optional[str]:
        """Get hours for a specific day.

        Args:
            day_index: Day of week (0=Sunday, 1=Monday, ..., 6=Saturday)
                       This matches Google's day format.

        Returns:
            Hours string for that day, or None if not available
        """
        if self.weekday_descriptions and 0 <= day_index < len(self.weekday_descriptions):
            return self.weekday_descriptions[day_index]
        return None

    def has_hours(self) -> bool:
        """Check if opening hours data is available."""
        return bool(self.weekday_descriptions)
