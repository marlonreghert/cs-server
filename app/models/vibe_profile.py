"""Venue Vibe Profile models for AI-powered venue atmosphere classification.

v2 Fixed-Taxonomy system:
- 8 categories with fixed-vocabulary labels (Portuguese)
- Per-category confidence (0-1) and evidence
- top_vibes for quick UI chips
- 2-stage hybrid pipeline preserved (Stage A: gpt-4o-mini, Stage B: gpt-4o)
"""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class EvidencePhoto(BaseModel):
    """Per-photo evidence for explainability and photo sorting."""
    photo_url: str
    relevance_score: float = 0.0   # 0-10
    vibe_appeal: float = 0.0       # 0-10: how well photo communicates venue atmosphere
    photo_type: str = ""           # interior|exterior|crowd|food|drink|event|menu|selfie|other
    evidence_tags: list[str] = []  # e.g., ["crowded", "neon_lighting", "standing_dancefloor"]


class CategoryEvidence(BaseModel):
    """Evidence supporting a category classification."""
    photo_indices: list[int] = []       # 0-based indices of photos that support this
    review_quotes: list[str] = []       # Short review excerpts (max 50 chars each)


class TaxonomyCategory(BaseModel):
    """A single taxonomy category with fixed-vocabulary labels."""
    labels: list[str] = []              # Max 4, from fixed vocabulary only
    confidence: float = 0.0             # 0.0 to 1.0
    evidence: CategoryEvidence = Field(default_factory=CategoryEvidence)


class VenueVibeProfile(BaseModel):
    """Full AI-generated vibe profile for a venue.

    Stored in Redis at key: venue_vibe_profile_v2:{venue_id}
    """
    venue_id: str
    schema_version: str = "v2"

    # ── 8 Taxonomy Categories ──
    publico: TaxonomyCategory = Field(default_factory=TaxonomyCategory)
    musica: TaxonomyCategory = Field(default_factory=TaxonomyCategory)
    music_format: TaxonomyCategory = Field(default_factory=TaxonomyCategory)
    estilo_do_lugar: TaxonomyCategory = Field(default_factory=TaxonomyCategory)
    estetica: TaxonomyCategory = Field(default_factory=TaxonomyCategory)
    intencao: TaxonomyCategory = Field(default_factory=TaxonomyCategory)
    dress_code: TaxonomyCategory = Field(default_factory=TaxonomyCategory)
    clima_social: TaxonomyCategory = Field(default_factory=TaxonomyCategory)

    # ── Global Output ──
    top_vibes: list[str] = []           # Up to 6 tags across all categories for UI chips
    overall_confidence: float = 0.0     # Average of category confidences, penalized if empty
    notes: Optional[str] = None         # Brief classifier note

    # ── Blurbs ──
    vibe_short_pt: Optional[str] = None
    vibe_short_en: Optional[str] = None
    vibe_long_pt: Optional[str] = None
    vibe_long_en: Optional[str] = None

    # ── Data Sources & Evidence ──
    data_sources: list[str] = []        # e.g. ["photos", "ig_bio", "ig_posts", "google_reviews"]
    evidence_photos: list[EvidencePhoto] = []
    photos_analyzed: int = 0
    photos_available: int = 0

    # ── Pipeline Metadata ──
    stage_b_triggered: bool = False
    uncertainty_reasons: list[str] = []
    classification_trace: list[str] = []  # e.g. ["gpt-4o-mini:stage_a", "gpt-4o:stage_b"]
    classified_at: datetime = Field(default_factory=datetime.utcnow)

    def has_profile(self) -> bool:
        """Check if this profile has meaningful data."""
        return len(self.top_vibes) > 0 or self.overall_confidence > 0

    def get_all_labels(self) -> dict[str, list[str]]:
        """Return all category labels as a dict for similarity computation."""
        return {
            "publico": self.publico.labels,
            "musica": self.musica.labels,
            "music_format": self.music_format.labels,
            "estilo_do_lugar": self.estilo_do_lugar.labels,
            "estetica": self.estetica.labels,
            "intencao": self.intencao.labels,
            "dress_code": self.dress_code.labels,
            "clima_social": self.clima_social.labels,
        }
