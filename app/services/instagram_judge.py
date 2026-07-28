"""LLM adjudication for Instagram candidates the cheap signals cannot settle.

Consulted only in the ambiguous band. The measured case it exists for: venue
"Bercy Boa Viagem" vs profile "Bercy Village" scores 0.76 on name similarity —
a TRUE match that a 0.8 threshold rejects and a 0.7 threshold accepts. String
distance cannot resolve that; a model looking at the name, the address and the
pictures can.

DESIGN REQUIREMENT: the judge must return a verdict even with no images at all.
A venue may have nothing archived, and a profile may use a blank or generic
avatar. Missing images lower the confidence CEILING, they never prevent an
answer — a text-only judgement is weaker, not impossible.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MODE_VISION_BOTH = "vision_both"
MODE_VISION_PARTIAL = "vision_partial"
MODE_TEXT_ONLY = "text_only"
MODE_UNAVAILABLE = "unavailable"

# A text-only verdict cannot see the venue, so it may not claim the certainty a
# picture-backed one can. This caps what the cascade will record from it.
TEXT_ONLY_CONFIDENCE_CEILING = 0.80


@dataclass(frozen=True)
class JudgeVerdict:
    mode: str
    is_match: bool
    confidence: float
    reason: Optional[str] = None


def select_mode(*, profile_image, venue_photos) -> str:
    """Pick the strongest mode the available inputs support.

    Both sides -> compare. One side -> still useful (the model sees a real venue
    photo or a real profile picture alongside the text). Neither -> text only.
    """
    has_profile_image = bool(profile_image)
    has_venue_photos = bool(venue_photos)
    if has_profile_image and has_venue_photos:
        return MODE_VISION_BOTH
    if has_profile_image or has_venue_photos:
        return MODE_VISION_PARTIAL
    return MODE_TEXT_ONLY


def cap_for_mode(mode: str, confidence: float) -> float:
    """Text-only verdicts are capped; picture-backed ones are not."""
    if mode == MODE_TEXT_ONLY:
        return min(confidence, TEXT_ONLY_CONFIDENCE_CEILING)
    return confidence


_PROMPT = (
    "You decide whether an Instagram profile belongs to a specific venue.\n"
    "Venue name: {venue_name}\n"
    "Venue address: {venue_address}\n"
    "Profile handle: @{handle}\n"
    "Profile display name: {display_name}\n"
    "Profile followers: {followers}\n"
    "{image_note}\n"
    "Answer ONLY with JSON: "
    '{{"is_match": true|false, "confidence": 0.0-1.0, "reason": "<one short sentence>"}}\n'
    "Judge on the evidence you actually have. If you were given no images, say so "
    "in the reason and lower your confidence accordingly — do not refuse to answer."
)

_IMAGE_NOTES = {
    MODE_VISION_BOTH: "You are given the profile picture and photos of the venue. Compare them.",
    MODE_VISION_PARTIAL: "You are given images for only one side; weigh them with the text.",
    MODE_TEXT_ONLY: "NO images are available. Judge from the text alone.",
}


class InstagramJudge:
    """Wraps an OpenAI-compatible client. Never raises into the cascade."""

    def __init__(self, openai_client, *, model: str = "gpt-4o-mini", max_photos: int = 3):
        self.client = openai_client
        self.model = model
        self.max_photos = max_photos

    async def judge(self, *, venue, candidate, profile, venue_photos) -> Optional[JudgeVerdict]:
        profile = profile or {}
        photos = list(venue_photos or [])[: self.max_photos]
        mode = select_mode(profile_image=profile.get("image_url"), venue_photos=photos)

        prompt = _PROMPT.format(
            venue_name=getattr(venue, "venue_name", "") or "",
            venue_address=getattr(venue, "venue_address", "") or "",
            handle=candidate,
            display_name=profile.get("display_name") or "(none)",
            followers=profile.get("followers_count") or "(unknown)",
            image_note=_IMAGE_NOTES[mode],
        )

        try:
            raw = await self.client.judge_instagram_match(
                prompt=prompt,
                model=self.model,
                profile_image_url=profile.get("image_url"),
                venue_photos=photos,
            )
            data = raw if isinstance(raw, dict) else json.loads(raw)
            confidence = float(data.get("confidence", 0.0))
            return JudgeVerdict(
                mode=mode,
                is_match=bool(data.get("is_match")),
                confidence=cap_for_mode(mode, confidence),
                reason=str(data.get("reason") or "")[:200] or None,
            )
        except Exception as e:
            # An unavailable judge must degrade the decision, not fail the venue.
            logger.warning(f"[InstagramJudge] judge failed for @{candidate}: {e}")
            return None
