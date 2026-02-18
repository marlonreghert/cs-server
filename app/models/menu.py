"""Menu photo and extraction models for venue menu enrichment."""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class MenuPhoto(BaseModel):
    """A single menu photo stored in S3."""
    photo_id: str                        # UUID
    s3_url: str                          # https://<bucket>.s3.<region>.amazonaws.com/places/<vid>/photos/menu/<pid>.jpg
    s3_key: str                          # places/<vid>/photos/menu/<pid>.jpg
    category: str = ""                   # Photo category (e.g. "all", "Cardápio")
    source_url: Optional[str] = None     # Original URL from Apify/Google Maps
    author_name: Optional[str] = None    # Photo author from Google Maps
    uploaded_at: Optional[datetime] = None  # When photo was uploaded to Google Maps
    downloaded_at: datetime = Field(default_factory=datetime.utcnow)


class VenueMenuPhotos(BaseModel):
    """Cached menu photos result for a venue.

    Stored in Redis at key: venue_menu_photos_v1:{venue_id}
    """
    venue_id: str
    photos: list[MenuPhoto] = []
    available_categories: list[str] = []  # Photo categories reported by Google Maps
    has_menu_category: bool = False        # Whether Google Maps lists a menu/cardápio category
    total_images_on_maps: int = 0          # Total photos reported by Google Maps
    enriched_at: datetime = Field(default_factory=datetime.utcnow)

    def has_photos(self) -> bool:
        return len(self.photos) > 0


class MenuItem(BaseModel):
    """A single menu item extracted from photos."""
    name: str
    description: Optional[str] = None
    prices: list[dict] = []              # [{"label": "Individual", "price": 22.00}]
    dietary_tags: list[str] = []
    modifiers: list[dict] = []           # [{"name": "Extra cheese", "price": 5.00}]


class MenuSection(BaseModel):
    """A section/category of menu items."""
    name: str                            # "Hamburgueres Artesanais", "Bebidas", etc.
    items: list[MenuItem] = []


class VenueMenuData(BaseModel):
    """Cached extracted menu data for a venue.

    Stored in Redis at key: venue_menu_raw_data_v1:{venue_id}
    """
    venue_id: str
    sections: list[MenuSection] = []
    currency_detected: Optional[str] = None
    source_photo_ids: list[str] = []
    extraction_model: str = "gpt-4o"
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    raw_response: Optional[str] = None   # Raw GPT output for debugging
