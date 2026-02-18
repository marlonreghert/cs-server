"""OpenAI Vision client for venue vibe classification.

2-stage hybrid architecture:
- Stage A (gpt-4o-mini, detail="low"): photo scoring + vibe extraction in one call
- Stage B (gpt-4o, detail="high"): refinement for uncertain facets + blurb generation
"""
import json
import logging
import re
import time

from openai import AsyncOpenAI

from app.metrics import (
    OPENAI_API_CALLS_TOTAL,
    OPENAI_API_CALL_DURATION_SECONDS,
)

logger = logging.getLogger(__name__)

# Stage A prompt: compact JSON output, no prose
STAGE_A_PROMPT = """You are VibeSense's venue vibe classifier. Analyze all photos from this venue and return ONLY a JSON object.

Context: Venue name: "{venue_name}" | Type: "{venue_type}"

## Instructions
1. For each photo, assess its relevance and extract observable vibe signals.
2. Aggregate across all photos into a single venue vibe profile.
3. All numeric scores must be 0-10 scale.
4. Return ONLY valid JSON, no explanations.

## Output Schema
{{
  "photos": [
    {{
      "index": 0,
      "relevance": 7.5,
      "type": "interior|exterior|crowd|food_drink|event|menu|selfie|other",
      "tags": ["dim_lighting", "crowded", "neon_signs"]
    }}
  ],
  "core_venue_modes": [
    {{"label": "bar_social|dining_restaurant|club_party|lounge_cocktail|boteco_raiz|cafe_brunch|outdoor_beer_garden|live_music_venue|cultural_space|mixed_use", "confidence": 0.9}}
  ],
  "crowd_types": [
    {{"label": "alternativo_indie|mainstream_social|corporate_professional|university_student|tourist|family_friendly|upscale_affluent|local_regulars|fitness_wellness|lgbtq_friendly", "confidence": 0.8}}
  ],
  "energy": {{
    "energy_level": 7, "party_intensity": 5, "conversation_focus": 6,
    "date_friendly": 7, "group_friendly": 8, "networking_friendly": 4, "dance_likelihood": 3
  }},
  "price": {{
    "price_score": 5, "price_tier": "budget|mid_range|upscale|premium",
    "predictability_score": 3
  }},
  "environment": {{
    "aesthetic_score": 7, "instagrammable_score": 6, "cleanliness_score": 7, "comfort_score": 6,
    "indoor_outdoor": "indoor|outdoor|mixed",
    "lighting": "bright|dim_ambient|dark|natural|neon",
    "decor_styles": [{{"label": "modern|rustic|industrial|tropical|classic|eclectic|vintage|minimalist", "confidence": 0.8}}]
  }},
  "crowd_density": {{
    "crowd_density_visible": 6, "seating_vs_standing_ratio": 4
  }},
  "music": {{
    "music_prominence": 7,
    "genres": [{{"label": "rock|sertanejo|pagode|funk|electronic|mpb|jazz|pop|hip_hop|reggae|forro", "confidence": 0.7}}]
  }},
  "safety": {{
    "perceived_safety": 8, "accessibility_score": 6, "crowd_diversity_signal": 7
  }},
  "vibe_keywords": ["energetic", "casual", "music"],
  "overall_confidence": 0.82,
  "uncertainty_reasons": []
}}

## Rules
- Only include labels you are confident about (confidence >= 0.5).
- If a facet cannot be determined from photos, set to null.
- For multi-label fields (core_venue_modes, crowd_types, decor_styles, genres), include only relevant labels.
- Be concise: no explanations, only the JSON object."""

# Stage B prompt: focused refinement + blurb generation
STAGE_B_PROMPT = """You are VibeSense's venue vibe classifier performing a REFINEMENT pass.

Context: Venue name: "{venue_name}"

## Previous Stage A Results (partial)
{stage_a_json}

## Uncertain Facets to Refine
{uncertain_facets}

## Instructions
1. Look at these HIGH-RESOLUTION photos carefully.
2. ONLY provide refined values for the uncertain facets listed above.
3. Also generate bilingual venue description blurbs.
4. Return ONLY valid JSON.

## Output Schema
{{
  "refined_facets": {{
    // Only include facets from the uncertain list above
    // Use same structure as Stage A for each facet
  }},
  "vibe_short_pt": "Descrição curta do ambiente em português (max 100 chars)",
  "vibe_short_en": "Short vibe description in English (max 100 chars)",
  "vibe_long_pt": "Descrição detalhada do ambiente em português (2-3 frases)",
  "vibe_long_en": "Detailed vibe description in English (2-3 sentences)",
  "overall_confidence": 0.88,
  "uncertainty_reasons": []
}}"""


class OpenAIVibeClient:
    """Async client for OpenAI Vision-based venue vibe classification."""

    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

    async def close(self):
        """Close the OpenAI client."""
        await self.client.close()

    async def classify_venue_vibes_stage_a(
        self,
        photo_urls: list[str],
        venue_name: str = "",
        venue_type: str = "",
        model: str = "gpt-4o-mini",
    ) -> dict:
        """Stage A: Score photos + extract vibe facets in one call.

        Uses detail="low" for cheap thumbnails.

        Args:
            photo_urls: List of photo URLs (Google Places API)
            venue_name: Venue name for context
            venue_type: Venue type for context (e.g., "bar", "restaurant")
            model: Model to use (default: gpt-4o-mini)

        Returns:
            Parsed JSON dict with vibe profile, or empty dict on error.
        """
        if not photo_urls:
            return {}

        prompt = STAGE_A_PROMPT.format(
            venue_name=venue_name or "Unknown",
            venue_type=venue_type or "Unknown",
        )

        content = [{"type": "text", "text": prompt}]
        for url in photo_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url, "detail": "low"},
            })

        start_time = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                temperature=0.2,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )

            duration = time.perf_counter() - start_time
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint="vibe_stage_a").observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint="vibe_stage_a", status="success").inc()

            raw_text = response.choices[0].message.content or ""
            tokens = response.usage.total_tokens if response.usage else "?"
            logger.info(
                f"[VibeClient] Stage A complete in {duration:.1f}s, "
                f"tokens: {tokens}, photos: {len(photo_urls)}"
            )

            return self._parse_json_response(raw_text)

        except Exception as e:
            duration = time.perf_counter() - start_time
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint="vibe_stage_a").observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint="vibe_stage_a", status="error").inc()
            logger.error(f"[VibeClient] Stage A failed: {e}")
            return {}

    async def classify_venue_vibes_stage_b(
        self,
        photo_urls: list[str],
        stage_a_result: dict,
        uncertain_facets: list[str],
        venue_name: str = "",
        model: str = "gpt-4o",
    ) -> dict:
        """Stage B: Refine uncertain facets using high-resolution photos.

        Uses detail="high" for nuanced analysis.

        Args:
            photo_urls: Top relevant photo URLs (subset)
            stage_a_result: Stage A raw result dict
            uncertain_facets: List of facet names to refine
            venue_name: Venue name for context
            model: Model to use (default: gpt-4o)

        Returns:
            Parsed JSON dict with refined facets + blurbs, or empty dict on error.
        """
        if not photo_urls or not uncertain_facets:
            return {}

        # Build a compact version of Stage A results for context
        stage_a_compact = {
            k: v for k, v in stage_a_result.items()
            if k in ("core_venue_modes", "crowd_types", "energy", "price",
                      "environment", "crowd_density", "music", "safety",
                      "overall_confidence", "uncertainty_reasons")
        }

        prompt = STAGE_B_PROMPT.format(
            venue_name=venue_name or "Unknown",
            stage_a_json=json.dumps(stage_a_compact, ensure_ascii=False, indent=None),
            uncertain_facets=", ".join(uncertain_facets),
        )

        content = [{"type": "text", "text": prompt}]
        for url in photo_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url, "detail": "high"},
            })

        start_time = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
                max_tokens=3072,
                response_format={"type": "json_object"},
            )

            duration = time.perf_counter() - start_time
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint="vibe_stage_b").observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint="vibe_stage_b", status="success").inc()

            raw_text = response.choices[0].message.content or ""
            tokens = response.usage.total_tokens if response.usage else "?"
            logger.info(
                f"[VibeClient] Stage B complete in {duration:.1f}s, "
                f"tokens: {tokens}, photos: {len(photo_urls)}, "
                f"facets: {uncertain_facets}"
            )

            return self._parse_json_response(raw_text)

        except Exception as e:
            duration = time.perf_counter() - start_time
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint="vibe_stage_b").observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint="vibe_stage_b", status="error").inc()
            logger.error(f"[VibeClient] Stage B failed: {e}")
            return {}

    def _parse_json_response(self, raw_text: str) -> dict:
        """Parse JSON response, stripping markdown fences if present.

        Args:
            raw_text: Raw text from OpenAI response

        Returns:
            Parsed dict, or empty dict on error.
        """
        cleaned = raw_text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"[VibeClient] Failed to parse JSON response: {e}")
            return {}
