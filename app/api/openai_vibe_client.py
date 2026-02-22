"""OpenAI Vision client for venue vibe classification.

2-stage hybrid architecture:
- Stage A (gpt-4o-mini, detail="low"): photo scoring + fixed-taxonomy classification
- Stage B (gpt-4o, detail="high"): refinement for uncertain categories + blurb generation

v2: Fixed-taxonomy system with 8 categories and strict label vocabulary.
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

# Stage A prompt: fixed-taxonomy classification + photo scoring
STAGE_A_PROMPT = """You are VibeSense Venue Vibe Classifier for bars and nightlife in Recife, Brazil. Analyze ALL available evidence (photos + text signals) and return ONLY a JSON object. You must be precise, conservative, and return ONLY valid labels from the provided taxonomy. Do not invent new labels. If evidence is weak, return an empty list for that category and reduce confidence.

Context: Venue name: "{venue_name}" | Type: "{venue_type}"
{text_context}
## Instructions
1. For each photo, score its relevance and vibe appeal, and classify its type.
   - "relevance": how useful this photo is for classifying the venue (0-10).
   - "vibe_appeal": how well this photo communicates the venue's atmosphere to a potential visitor browsing the app (0-10). Prefer photos showing the interior ambiance, crowd, lighting, and decor over menus, logos, or blurry selfies.
2. Classify the venue into 8 fixed taxonomy categories. Use ONLY the labels listed below — never invent new ones.
3. Use BOTH modalities:
   - PHOTOS are primary for: Estética, Estilo do Lugar, Dress Code, Clima Social (barulho/ambiente), "Pra dançar"
   - REVIEWS/TEXT are primary for: Música (gênero), Formato Musical (ao vivo/DJ/karaokê), Público, Intenção, Clima Social
4. Be CONSERVATIVE: if you cannot confidently assign a label, leave the category empty rather than guess. Max 4 labels per category.
5. Do not infer sensitive traits beyond what is clearly indicated (e.g., LGBTQ+ only if explicitly stated in reviews or strongly signaled by venue positioning/branding visible in text).
6. For evidence, cite photo indices (0-based) and brief review quotes that support each category.
7. Generate top_vibes: the 6 most defining tags across all categories — what makes someone choose THIS place on a night out.
   Prioritize: Estilo do Lugar, Intenção, Música, Público, Estética.
8. Generate short blurbs in Portuguese and English describing the venue's atmosphere.
9. Return ONLY valid JSON, no explanations.

## Fixed Taxonomy Labels (ONLY THESE ARE ALLOWED)

### Público (crowd types — who goes there)
Turistas, Alternativo, Gótico, LGBTQ+, Casais, Galera 50+, Galera 30+, Galera jovem, Família, Artistas / criativos, Público misto

### Música (genre — what you'll hear)
Pagode, Samba, Sertanejo, Funk, Eletrônica, Techno, House, Pop, Rock, Indie, Rap / Trap, MPB, Reggaeton, Forró, Jazz, Música ambiente, Brega, Frevo

### Formato Musical (how music is delivered)
DJ, Som ao vivo, Banda ao vivo, Roda de samba, Karaokê, Playlist ambiente, Open mic, Instrumental

### Estilo do Lugar (venue style)
Boteco raiz, Gastrobar, Bar tradicional, Lounge, Balada, Club, Pub, Rooftop, Pé na areia, Beach club, Wine bar, Coquetelaria, Bar com jogos, Speakeasy, Cultural / alternativo, Inferninho

### Estética (look and feel)
Instagramável, Minimalista, Retrô, Underground, Neon, Intimista, Sofisticado, Moderno, Rústico, Ao ar livre, Vista bonita, Beira-mar, Nature vibe

### Intenção (why you'd go)
Pra dançar, Clima de date, Sentar com a galera, Aniversário, Comemoração, Jantar tranquilo, Virar a noite, Conhecer gente nova, Beber de leve, Happy hour, After

### Dress Code (what to wear)
Casual, Arrumadinho, Esporte fino, Praia, Alternativo, Sem dress code

### Clima Social (social energy)
Intimista, Social, Animado, Agitado, Fervendo, Tranquilo

## Heuristics
A) Evidence rules:
- "DJ", "Karaokê", "Som ao vivo", "Roda de samba" should be supported by a review mention OR a clear visual cue (stage, mic, instruments, DJ booth).
- "Pé na areia" / "Beira-mar" must be supported by photo evidence (sand, shoreline) or strong review mention.
- "Rooftop" must be supported by photo evidence (open skyline/terrace) or explicit mention.
- "LGBTQ+" only if explicit text in reviews/branding or strong textual cues like "gay friendly" / "LGBT" / "drag night". Don't infer from crowd appearance.
B) Clima social and barulho:
- If photos show small tables, warm lighting, and reviews mention "conversa", set "Tranquilo" or "Intimista".
C) Dress code:
- Infer from attire in photos + venue style: Beach/sand => "Praia", Club/lux => "Esporte fino", Boteco => "Casual" / "Sem dress code".
D) Limits:
- Up to 4 labels per category per venue.
- Prefer specificity: "Coquetelaria" over "Bar tradicional" if clear cocktail bar cues exist.
- If venue_type suggests but evidence is missing, keep confidence low or empty.

## Output Schema
{{
  "photos": [
    {{
      "index": 0,
      "relevance": 7.5,
      "vibe_appeal": 8.0,
      "type": "interior|exterior|crowd|food|drink|event|menu|selfie|other",
      "tags": ["dim_lighting", "crowded", "neon_signs"]
    }}
  ],
  "publico": {{
    "labels": ["Galera jovem"],
    "confidence": 0.75,
    "evidence": {{
      "photo_indices": [2, 5],
      "review_quotes": ["Público jovem e diverso"]
    }}
  }},
  "musica": {{
    "labels": ["Eletrônica", "House"],
    "confidence": 0.80,
    "evidence": {{
      "photo_indices": [],
      "review_quotes": ["DJ tocando house music toda sexta"]
    }}
  }},
  "music_format": {{
    "labels": ["DJ"],
    "confidence": 0.85,
    "evidence": {{
      "photo_indices": [3],
      "review_quotes": []
    }}
  }},
  "estilo_do_lugar": {{
    "labels": ["Balada"],
    "confidence": 0.90,
    "evidence": {{
      "photo_indices": [0, 1, 4],
      "review_quotes": []
    }}
  }},
  "estetica": {{
    "labels": ["Neon", "Underground"],
    "confidence": 0.70,
    "evidence": {{
      "photo_indices": [0, 1],
      "review_quotes": []
    }}
  }},
  "intencao": {{
    "labels": ["Pra dançar", "Virar a noite"],
    "confidence": 0.80,
    "evidence": {{
      "photo_indices": [],
      "review_quotes": ["O melhor lugar pra dançar"]
    }}
  }},
  "dress_code": {{
    "labels": ["Casual"],
    "confidence": 0.60,
    "evidence": {{
      "photo_indices": [2],
      "review_quotes": []
    }}
  }},
  "clima_social": {{
    "labels": ["Agitado", "Fervendo"],
    "confidence": 0.85,
    "evidence": {{
      "photo_indices": [2, 5],
      "review_quotes": ["Lotado toda sexta"]
    }}
  }},
  "top_vibes": ["Pra dançar", "Eletrônica", "Balada", "Neon", "Galera jovem", "Virar a noite"],
  "overall_confidence": 0.78,
  "notes": "Strong visual evidence for club atmosphere; music genres confirmed by reviews.",
  "vibe_short_pt": "Balada eletrônica com vibe underground e neon.",
  "vibe_short_en": "Underground electronic club with neon vibes and non-stop dancing.",
  "vibe_long_pt": "Uma balada eletrônica com estética underground e iluminação neon. O público é jovem e diverso, com DJ tocando house e eletrônica. O clima é agitado — ideal pra quem quer dançar e virar a noite.",
  "vibe_long_en": "An underground electronic club with neon aesthetics. The crowd is young and diverse, with DJs spinning house and electronic music. The energy is high — perfect for dancing the night away."
}}

## Rules
- ONLY use labels from the taxonomy above. No free-form labels.
- Max 4 labels per category.
- Confidence 0.0-1.0 per category.
- If you cannot determine a category, set labels to [] and confidence to 0.
- top_vibes: pick up to 6 tags from across all categories that best capture the venue's social identity.
- overall_confidence: average of non-empty category confidences, penalized by 0.05 for each empty category.
- Be Recife/Brazil aware: know local venue types (boteco, inferninho), music (frevo, brega, manguebeat context), and cultural norms.
- For evidence, photo_indices are 0-based matching the photo order sent. review_quotes should be short excerpts (max 50 chars).
- Return ONLY the JSON object, no markdown fences or explanations."""

# Stage B prompt: focused refinement for uncertain categories
STAGE_B_PROMPT = """You are VibeSense Venue Vibe Classifier performing a REFINEMENT pass for a venue in Recife, Brazil.

Context: Venue name: "{venue_name}"
{text_context}
## Previous Stage A Results
{stage_a_json}

## Categories to Refine
{uncertain_categories}

## Instructions
1. Look at these HIGH-RESOLUTION photos carefully.
2. Use text signals (IG bio, IG posts, Google reviews) to help resolve the uncertain categories listed above.
3. ONLY provide refined values for the categories listed above — do NOT repeat already-confident categories.
4. Also regenerate top_vibes (up to 6 tags) and blurbs based on the full picture.
5. Use ONLY labels from the fixed taxonomy (same as Stage A).
6. Return ONLY valid JSON.

## Fixed Taxonomy Labels (same as Stage A)
- Público: Turistas, Alternativo, Gótico, LGBTQ+, Casais, Galera 50+, Galera 30+, Galera jovem, Família, Artistas / criativos, Público misto
- Música: Pagode, Samba, Sertanejo, Funk, Eletrônica, Techno, House, Pop, Rock, Indie, Rap / Trap, MPB, Reggaeton, Forró, Jazz, Música ambiente, Brega, Frevo
- Formato Musical: DJ, Som ao vivo, Banda ao vivo, Roda de samba, Karaokê, Playlist ambiente, Open mic, Instrumental
- Estilo do Lugar: Boteco raiz, Gastrobar, Bar tradicional, Lounge, Balada, Club, Pub, Rooftop, Pé na areia, Beach club, Wine bar, Coquetelaria, Bar com jogos, Speakeasy, Cultural / alternativo, Inferninho
- Estética: Instagramável, Minimalista, Retrô, Underground, Neon, Intimista, Sofisticado, Moderno, Rústico, Ao ar livre, Vista bonita, Beira-mar, Nature vibe
- Intenção: Pra dançar, Clima de date, Sentar com a galera, Aniversário, Comemoração, Jantar tranquilo, Virar a noite, Conhecer gente nova, Beber de leve, Happy hour, After
- Dress Code: Casual, Arrumadinho, Esporte fino, Praia, Alternativo, Sem dress code
- Clima Social: Intimista, Social, Animado, Agitado, Fervendo, Tranquilo

## Output Schema
{{
  "refined_categories": {{
    "estilo_do_lugar": {{
      "labels": ["Boteco raiz"],
      "confidence": 0.90,
      "evidence": {{
        "photo_indices": [0, 2],
        "review_quotes": ["Boteco clássico de esquina"]
      }}
    }}
  }},
  "top_vibes": ["Boteco raiz", "Pagode", "Sentar com a galera", "Galera 30+", "Casual", "Animado"],
  "overall_confidence": 0.85,
  "notes": "Refined estilo_do_lugar from high-res photos showing classic boteco decor.",
  "vibe_short_pt": "Boteco raiz com pagode ao vivo, clima animado e galera de todas as idades.",
  "vibe_short_en": "Classic neighborhood boteco with live pagode, lively atmosphere and mixed crowd.",
  "vibe_long_pt": "Um boteco raiz com mesas na calçada e pagode ao vivo. O público é de todas as idades, com galera de 30+ predominando. Ambiente casual e animado — ideal pra sentar com a galera e beber cerveja.",
  "vibe_long_en": "A classic sidewalk boteco with live pagode music. The crowd spans all ages with 30-somethings predominating. Casual and lively — perfect for hanging with friends over cold beers."
}}

## Rules
- refined_categories: only include the categories listed in "Categories to Refine" above.
- ONLY use labels from the fixed taxonomy. Max 4 per category.
- top_vibes: up to 6 tags from all categories (including the unrefined ones from Stage A).
- Return ONLY the JSON object, no markdown fences or explanations."""


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
        instagram_bio: str = "",
        instagram_posts: list[str] | None = None,
        google_reviews: list[dict] | None = None,
    ) -> dict:
        """Stage A: Score photos + extract vibe facets in one call.

        Uses detail="low" for cheap thumbnails.

        Args:
            photo_urls: List of photo URLs (Google Places API)
            venue_name: Venue name for context
            venue_type: Venue type for context (e.g., "bar", "restaurant")
            model: Model to use (default: gpt-4o-mini)
            instagram_bio: Instagram bio text
            instagram_posts: List of recent IG post captions
            google_reviews: List of review dicts with "rating" and "text" keys

        Returns:
            Parsed JSON dict with vibe profile, or empty dict on error.
        """
        if not photo_urls:
            return {}

        text_context = self._build_text_context(
            instagram_bio, instagram_posts, google_reviews,
        )

        prompt = STAGE_A_PROMPT.format(
            venue_name=venue_name or "Unknown",
            venue_type=venue_type or "Unknown",
            text_context=text_context,
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
                max_tokens=3072,
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
        instagram_bio: str = "",
        instagram_posts: list[str] | None = None,
        google_reviews: list[dict] | None = None,
    ) -> dict:
        """Stage B: Refine uncertain facets using high-resolution photos.

        Uses detail="high" for nuanced analysis.

        Args:
            photo_urls: Top relevant photo URLs (subset)
            stage_a_result: Stage A raw result dict
            uncertain_facets: List of facet names to refine
            venue_name: Venue name for context
            model: Model to use (default: gpt-4o)
            instagram_bio: Instagram bio text
            instagram_posts: List of recent IG post captions
            google_reviews: List of review dicts with "rating" and "text" keys

        Returns:
            Parsed JSON dict with refined facets + blurbs, or empty dict on error.
        """
        if not photo_urls or not uncertain_facets:
            return {}

        # Build a compact version of Stage A results for context
        stage_a_compact = {
            k: v for k, v in stage_a_result.items()
            if k in ("publico", "musica", "music_format", "estilo_do_lugar",
                      "estetica", "intencao", "dress_code", "clima_social",
                      "top_vibes", "overall_confidence", "notes")
        }

        text_context = self._build_text_context(
            instagram_bio, instagram_posts, google_reviews,
        )

        prompt = STAGE_B_PROMPT.format(
            venue_name=venue_name or "Unknown",
            stage_a_json=json.dumps(stage_a_compact, ensure_ascii=False, indent=None),
            uncertain_categories=", ".join(uncertain_facets),
            text_context=text_context,
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

    @staticmethod
    def _build_text_context(
        instagram_bio: str = "",
        instagram_posts: list[str] | None = None,
        google_reviews: list[dict] | None = None,
    ) -> str:
        """Format IG bio + post captions + reviews as a text block for the prompt.

        Returns empty string if no text signals are available.
        """
        sections: list[str] = []

        # Instagram Bio
        if instagram_bio:
            sections.append(
                f"### Instagram Bio\n{instagram_bio.strip()}"
            )

        # Recent Instagram Posts (captions only)
        if instagram_posts:
            lines = []
            for i, caption in enumerate(instagram_posts[:10], 1):
                truncated = caption[:300].strip()
                if len(caption) > 300:
                    truncated += "..."
                lines.append(f"{i}. {truncated}")
            sections.append(
                "### Recent Instagram Posts (captions)\n" + "\n".join(lines)
            )

        # Google Reviews
        if google_reviews:
            lines = []
            for review in google_reviews[:5]:
                rating = review.get("rating", "?")
                text = (review.get("text") or "")[:200].strip()
                if len(review.get("text") or "") > 200:
                    text += "..."
                lines.append(f"- [{rating}/5] {text}")
            sections.append(
                "### Google Reviews (top 5)\n" + "\n".join(lines)
            )

        if not sections:
            return ""

        return (
            "\n## Additional Context (text signals — use alongside photos)\n\n"
            + "\n\n".join(sections)
            + "\n"
        )

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
