"""Shared photo-category derivation from vibe-profile evidence photos.

The vibe classifier's per-photo evidence (`VenueVibeProfile.evidence_photos`)
tags each analyzed photo with an AI `photo_type` (interior/exterior/crowd/
food/drink/event/menu/selfie/other). Both photo surfaces map that to the same
user-friendly category label:
  - The legacy embedded photo list (app/handlers/venue_handler.py).
  - The on-demand fresh-photos resolution (app/services/photo_enrichment_service.py)
    and its API surface (ResolvePhotosResponse, app/routers/internal_router.py).

Centralized here so the two surfaces cannot drift on the mapping.
"""
from typing import Optional

TYPE_TO_CATEGORY: dict[str, str] = {
    "interior": "Ambiente", "exterior": "Ambiente", "crowd": "Ambiente",
    "food": "Comida", "drink": "Bebida",
    "event": "Evento",
    "menu": "Outro", "selfie": "Outro", "other": "Outro",
    # Backward compat for the old food_drink type.
    "food_drink": "Comida",
}


def category_for_url(vibe_profile, photo_url: Optional[str]) -> Optional[str]:
    """Return the friendly category label for `photo_url` per the vibe
    profile's evidence-photo mapping.

    Returns None when there is no vibe profile, no evidence photos, or no
    evidence entry matching this exact URL — a resolved photo that predates
    (or postdates, if Google rotated tokens) classification simply carries no
    category, never an error.
    """
    if not vibe_profile or not getattr(vibe_profile, "evidence_photos", None) or not photo_url:
        return None
    for ep in vibe_profile.evidence_photos:
        if ep.photo_url == photo_url:
            return TYPE_TO_CATEGORY.get(ep.photo_type, "Outro")
    return None
