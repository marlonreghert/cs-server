"""Venue Vibe Profile models for AI-powered venue atmosphere classification.

2-stage hybrid pipeline:
- Stage A (gpt-4o-mini): photo scoring + vibe extraction
- Stage B (gpt-4o): refinement for uncertain venues

All numeric scores normalized to 0-10 for consistency.
"""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class FacetScore(BaseModel):
    """A labeled facet with confidence score."""
    label: str
    confidence: float = 0.0


class EvidencePhoto(BaseModel):
    """Per-photo evidence for explainability and debugging."""
    photo_url: str
    relevance_score: float = 0.0   # 0-10
    photo_type: str = ""           # crowd|interior|exterior|food_drink|event|menu|selfie|other
    evidence_tags: list[str] = []  # e.g., ["crowded", "neon_lighting", "standing_dancefloor"]


class EnergyDynamics(BaseModel):
    """Energy & Social Dynamics facet group. All scores 0-10."""
    energy_level: Optional[float] = None
    party_intensity: Optional[float] = None
    conversation_focus: Optional[float] = None
    date_friendly: Optional[float] = None
    group_friendly: Optional[float] = None
    networking_friendly: Optional[float] = None
    dance_likelihood: Optional[float] = None


class PricePositioning(BaseModel):
    """Price & Positioning facet group."""
    price_score: Optional[float] = None            # 0-10
    price_tier: Optional[str] = None               # budget|mid_range|upscale|premium
    predictability_score: Optional[float] = None   # 0-10 (0=unique/underground, 10=chain/franchise)


class EnvironmentAesthetic(BaseModel):
    """Environment / Aesthetic facet group. All scores 0-10."""
    aesthetic_score: Optional[float] = None
    instagrammable_score: Optional[float] = None
    cleanliness_score: Optional[float] = None
    comfort_score: Optional[float] = None
    indoor_outdoor: Optional[str] = None           # indoor|outdoor|mixed
    lighting: Optional[str] = None                 # bright|dim_ambient|dark|natural|neon
    decor_styles: list[FacetScore] = []            # multi-label: [{label: "industrial", confidence: 0.8}]


class CrowdDensity(BaseModel):
    """Busyness / Crowd Density facet group. All scores 0-10."""
    crowd_density_visible: Optional[float] = None       # 0=empty, 10=packed
    seating_vs_standing_ratio: Optional[float] = None   # 0=all standing, 10=all seated


class MusicProfile(BaseModel):
    """Music Likelihood facet group."""
    music_prominence: Optional[float] = None     # 0-10
    genres: list[FacetScore] = []                # [{label: "rock", confidence: 0.7}]


class SafetyComfort(BaseModel):
    """Safety / Comfort Signals facet group. All scores 0-10."""
    perceived_safety: Optional[float] = None
    accessibility_score: Optional[float] = None
    crowd_diversity_signal: Optional[float] = None


class VenueVibeProfile(BaseModel):
    """Full AI-generated vibe profile for a venue.

    Stored in Redis at key: venue_vibe_profile_v1:{venue_id}
    """
    venue_id: str
    schema_version: str = "v1"

    # 1. Core Venue Mode (multi-label)
    core_venue_modes: list[FacetScore] = []
    # 2. Crowd Type (multi-label)
    crowd_types: list[FacetScore] = []
    # 3. Energy & Social Dynamics
    energy: EnergyDynamics = Field(default_factory=EnergyDynamics)
    # 4. Price & Positioning
    price: PricePositioning = Field(default_factory=PricePositioning)
    # 5. Environment / Aesthetic
    environment: EnvironmentAesthetic = Field(default_factory=EnvironmentAesthetic)
    # 6. Crowd Density
    crowd_density: CrowdDensity = Field(default_factory=CrowdDensity)
    # 7. Music
    music: MusicProfile = Field(default_factory=MusicProfile)
    # 8. Safety / Comfort
    safety: SafetyComfort = Field(default_factory=SafetyComfort)

    # 9. Embedding-ready summaries
    vibe_keywords: list[str] = []
    vibe_short_pt: Optional[str] = None
    vibe_short_en: Optional[str] = None
    vibe_long_pt: Optional[str] = None
    vibe_long_en: Optional[str] = None

    # 10. Evidence & Debug
    evidence_photos: list[EvidencePhoto] = []
    per_label_confidence: dict[str, float] = {}
    photos_analyzed: int = 0
    photos_available: int = 0
    overall_confidence: float = 0.0
    stage_b_triggered: bool = False
    uncertainty_reasons: list[str] = []
    classification_trace: list[str] = []   # e.g. ["gpt-4o-mini:stage_a", "gpt-4o:stage_b"]

    # Metadata
    classified_at: datetime = Field(default_factory=datetime.utcnow)

    def has_profile(self) -> bool:
        """Check if this profile has meaningful data."""
        return len(self.core_venue_modes) > 0 or self.overall_confidence > 0
