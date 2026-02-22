"""Service for AI-powered venue vibe classification from photos.

2-stage hybrid pipeline:
- Stage A (gpt-4o-mini): Cheap photo scoring + fixed-taxonomy classification
- Stage B (gpt-4o): Expensive refinement for uncertain categories

v2: Fixed-taxonomy system with 8 categories and strict label vocabulary.
Reuses photos already cached in Redis by PhotoEnrichmentService.
"""
import asyncio
import logging
from typing import Optional

from app.api.openai_vibe_client import OpenAIVibeClient
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.vibe_profile import (
    VenueVibeProfile,
    TaxonomyCategory,
    CategoryEvidence,
    EvidencePhoto,
)
from app.models.taxonomy import (
    TAXONOMY_CATEGORIES,
    validate_category_labels,
    validate_top_vibes,
)
from app.metrics import (
    VIBE_CLASSIFIER_RESULTS,
    VIBE_CLASSIFIER_STAGE_B_TRIGGERS,
    VENUES_WITH_VIBE_PROFILE,
    VIBE_CLASSIFIER_CONFIDENCE,
)

logger = logging.getLogger(__name__)

# Rate limiting for OpenAI API
REQUEST_DELAY = 1.0

# Categories primarily determined by photos (benefit most from high-res Stage B)
PHOTO_PRIMARY_CATEGORIES = {"estetica", "estilo_do_lugar", "dress_code", "clima_social"}


class VibeClassifierService:
    """Orchestrates the 2-stage hybrid vibe classification pipeline."""

    def __init__(
        self,
        openai_vibe_client: OpenAIVibeClient,
        venue_dao: RedisVenueDAO,
        target_photos: int = 10,
        escalation_threshold: float = 0.80,
        stage_b_photo_count: int = 5,
        enrichment_limit: int = 20,
        early_stop_enabled: bool = True,
        early_stop_min_photos: int = 6,
        early_stop_confidence: float = 0.92,
        stage_a_model: str = "gpt-4o-mini",
        stage_b_model: str = "gpt-4o",
        priority_venues: list[str] | None = None,
    ):
        self.openai_client = openai_vibe_client
        self.venue_dao = venue_dao
        self.target_photos = target_photos
        self.escalation_threshold = escalation_threshold
        self.stage_b_photo_count = stage_b_photo_count
        self.enrichment_limit = enrichment_limit
        self.early_stop_enabled = early_stop_enabled
        self.early_stop_min_photos = early_stop_min_photos
        self.early_stop_confidence = early_stop_confidence
        self.stage_a_model = stage_a_model
        self.stage_b_model = stage_b_model
        self.priority_venues = [n.lower() for n in (priority_venues or [])]

    async def classify_venue(
        self, venue_id: str, force: bool = False
    ) -> Optional[VenueVibeProfile]:
        """Classify a single venue's vibe from its cached photos.

        Args:
            venue_id: Venue identifier
            force: If True, re-classify even if cached

        Returns:
            VenueVibeProfile if successful, None on error or no photos.
        """
        # 1. Check cache
        if not force:
            existing = self.venue_dao.get_venue_vibe_profile(venue_id)
            if existing is not None:
                logger.debug(f"[VibeClassifier] Already classified {venue_id}, skipping")
                VIBE_CLASSIFIER_RESULTS.labels(result="cached").inc()
                return existing

        # 2. Read photos from Redis
        photos = self.venue_dao.get_venue_photos(venue_id)
        if not photos:
            logger.debug(f"[VibeClassifier] No photos for {venue_id}")
            VIBE_CLASSIFIER_RESULTS.labels(result="no_photos").inc()
            return None

        # Extract photo URLs (limit to target_photos)
        photo_urls = []
        for p in photos[:self.target_photos]:
            url = p.get("url") if isinstance(p, dict) else p
            if url:
                photo_urls.append(url)

        if not photo_urls:
            VIBE_CLASSIFIER_RESULTS.labels(result="no_photos").inc()
            return None

        # 3. Get venue metadata for context
        venue = self.venue_dao.get_venue(venue_id)
        venue_name = venue.venue_name if venue else ""
        venue_type = venue.venue_type if venue else ""

        # 3b. Gather text context from Redis
        instagram_bio = ""
        instagram_posts_captions: list[str] = []
        google_reviews_dicts: list[dict] = []
        data_sources = ["photos"]

        ig_data = self.venue_dao.get_venue_instagram(venue_id)
        if ig_data and ig_data.bio:
            instagram_bio = ig_data.bio
            data_sources.append("ig_bio")

        ig_posts_data = self.venue_dao.get_venue_ig_posts(venue_id)
        if ig_posts_data and ig_posts_data.posts:
            instagram_posts_captions = [
                p.caption for p in ig_posts_data.posts if p.caption
            ]
            if instagram_posts_captions:
                data_sources.append("ig_posts")

        reviews_data = self.venue_dao.get_venue_reviews(venue_id)
        if reviews_data and reviews_data.reviews:
            google_reviews_dicts = [
                {"author": r.author_name, "rating": r.rating, "text": r.text}
                for r in reviews_data.reviews
            ]
            if google_reviews_dicts:
                data_sources.append("google_reviews")

        # 4. Run Stage A
        logger.info(
            f"[VibeClassifier] Stage A for {venue_id} ({venue_name}): "
            f"{len(photo_urls)} photos, data_sources={data_sources}"
        )
        stage_a_result = await self.openai_client.classify_venue_vibes_stage_a(
            photo_urls=photo_urls,
            venue_name=venue_name,
            venue_type=venue_type or "",
            model=self.stage_a_model,
            instagram_bio=instagram_bio,
            instagram_posts=instagram_posts_captions,
            google_reviews=google_reviews_dicts,
        )

        if not stage_a_result:
            logger.warning(f"[VibeClassifier] Stage A returned empty for {venue_id}")
            VIBE_CLASSIFIER_RESULTS.labels(result="error").inc()
            return None

        # 5. Check uncertainty gate
        should_escalate, uncertain_categories = self._should_escalate(stage_a_result)
        stage_b_triggered = False
        stage_b_result = {}

        if should_escalate:
            # 6. Run Stage B on top relevant photos
            stage_b_triggered = True
            top_urls = self._get_top_relevant_urls(
                photo_urls, stage_a_result, self.stage_b_photo_count
            )
            logger.info(
                f"[VibeClassifier] Stage B triggered for {venue_id}: "
                f"uncertain={uncertain_categories}, photos={len(top_urls)}"
            )

            stage_b_result = await self.openai_client.classify_venue_vibes_stage_b(
                photo_urls=top_urls,
                stage_a_result=stage_a_result,
                uncertain_facets=uncertain_categories,
                venue_name=venue_name,
                model=self.stage_b_model,
                instagram_bio=instagram_bio,
                instagram_posts=instagram_posts_captions,
                google_reviews=google_reviews_dicts,
            )

            if stage_b_result:
                # Merge Stage B refinements into Stage A
                stage_a_result = self._merge_stage_b(
                    stage_a_result, stage_b_result, uncertain_categories
                )

        # 7. Build profile
        profile = self._build_profile(
            venue_id=venue_id,
            result=stage_a_result,
            stage_b_result=stage_b_result,
            photos=photos,
            photo_urls=photo_urls,
            stage_b_triggered=stage_b_triggered,
            data_sources=data_sources,
        )

        # 8. Generate blurbs if not from Stage B
        if not profile.vibe_short_pt:
            self._generate_blurbs_from_facets(profile)

        # 9. Cache in Redis
        self.venue_dao.set_venue_vibe_profile(profile)

        # 10. Update metrics
        VIBE_CLASSIFIER_RESULTS.labels(result="classified").inc()
        VIBE_CLASSIFIER_CONFIDENCE.observe(profile.overall_confidence)

        logger.info(
            f"[VibeClassifier] Classified {venue_id}: "
            f"confidence={profile.overall_confidence:.2f}, "
            f"top_vibes={profile.top_vibes}, "
            f"stage_b={stage_b_triggered}"
        )

        return profile

    async def classify_all_venues(self) -> int:
        """Classify all venues that have photos but no vibe profile.

        Returns:
            Number of venues successfully classified.
        """
        all_venue_ids = self.venue_dao.list_all_venue_ids()
        venues_with_photos = set(self.venue_dao.list_cached_venue_photos_ids())
        venues_with_profiles = set(self.venue_dao.list_cached_vibe_profile_venue_ids())

        # Only process venues that have photos but no profile
        venues_to_process = [
            vid for vid in all_venue_ids
            if vid in venues_with_photos and vid not in venues_with_profiles
        ]

        # Sort priority venues to the front
        if self.priority_venues:
            priority_ids = []
            rest_ids = []
            for vid in venues_to_process:
                venue = self.venue_dao.get_venue(vid)
                name = (venue.venue_name or "").lower() if venue else ""
                if name in self.priority_venues:
                    priority_ids.append(vid)
                else:
                    rest_ids.append(vid)
            if priority_ids:
                logger.info(
                    f"[VibeClassifier] Priority venues: {len(priority_ids)} "
                    f"matched from {len(self.priority_venues)} configured"
                )
            venues_to_process = priority_ids + rest_ids

        # Apply limit
        if self.enrichment_limit > 0:
            venues_to_process = venues_to_process[:self.enrichment_limit]

        logger.info(
            f"[VibeClassifier] Starting classification for "
            f"{len(venues_to_process)} venues "
            f"(total={len(all_venue_ids)}, with_photos={len(venues_with_photos)}, "
            f"already_classified={len(venues_with_profiles)})"
        )

        if not venues_to_process:
            logger.info("[VibeClassifier] No venues need classification")
            return 0

        successful = 0
        for venue_id in venues_to_process:
            try:
                result = await self.classify_venue(venue_id)
                if result is not None:
                    successful += 1
            except Exception as e:
                logger.error(f"[VibeClassifier] Error classifying {venue_id}: {e}")
                VIBE_CLASSIFIER_RESULTS.labels(result="error").inc()

            # Rate limiting
            await asyncio.sleep(REQUEST_DELAY)

        # Update gauge metric
        count = self.venue_dao.count_venues_with_vibe_profile()
        VENUES_WITH_VIBE_PROFILE.set(count)

        logger.info(
            f"[VibeClassifier] Classification complete: "
            f"{successful}/{len(venues_to_process)} venues classified"
        )

        return successful

    def _should_escalate(self, stage_a_result: dict) -> tuple[bool, list[str]]:
        """Check if Stage B should be triggered based on category confidences.

        Returns:
            (should_escalate, list_of_uncertain_category_names)
        """
        uncertain = []
        reasons = []

        # 1. Low overall confidence
        confidence = stage_a_result.get("overall_confidence", 0)
        if confidence < self.escalation_threshold:
            reasons.append("low_confidence")
            # Escalate photo-primary categories (most benefit from high-res)
            uncertain.extend(PHOTO_PRIMARY_CATEGORIES)

        # 2. Per-category low confidence (has labels but confidence < 0.50)
        for cat_key in TAXONOMY_CATEGORIES:
            cat_data = stage_a_result.get(cat_key, {})
            if not isinstance(cat_data, dict):
                continue
            cat_labels = cat_data.get("labels", [])
            cat_conf = cat_data.get("confidence", 0)
            if cat_labels and cat_conf < 0.50:
                reasons.append("low_category_confidence")
                uncertain.append(cat_key)

        # 3. Contradiction: Tranquilo clima_social but Pra dançar/Virar a noite intencao
        clima_labels = set(
            (stage_a_result.get("clima_social", {}) or {}).get("labels", [])
        )
        intencao_labels = set(
            (stage_a_result.get("intencao", {}) or {}).get("labels", [])
        )
        if "Tranquilo" in clima_labels and intencao_labels & {"Pra dançar", "Virar a noite"}:
            reasons.append("contradictions")
            uncertain.extend(["clima_social", "intencao"])

        # 4. Contradiction: Família publico but Balada/Club/Inferninho estilo
        publico_labels = set(
            (stage_a_result.get("publico", {}) or {}).get("labels", [])
        )
        estilo_labels = set(
            (stage_a_result.get("estilo_do_lugar", {}) or {}).get("labels", [])
        )
        if "Família" in publico_labels and estilo_labels & {"Balada", "Club", "Inferninho"}:
            reasons.append("contradictions")
            uncertain.extend(["publico", "estilo_do_lugar"])

        # 5. Contradiction: Esporte fino dress_code but Boteco raiz/Inferninho estilo
        dress_labels = set(
            (stage_a_result.get("dress_code", {}) or {}).get("labels", [])
        )
        if "Esporte fino" in dress_labels and estilo_labels & {"Boteco raiz", "Inferninho"}:
            reasons.append("contradictions")
            uncertain.extend(["dress_code", "estilo_do_lugar"])

        should_escalate = len(reasons) > 0
        if should_escalate:
            for reason in set(reasons):
                VIBE_CLASSIFIER_STAGE_B_TRIGGERS.labels(reason=reason).inc()

        # Deduplicate
        uncertain = list(set(uncertain))
        return should_escalate, uncertain

    def _get_top_relevant_urls(
        self, photo_urls: list[str], stage_a_result: dict, count: int
    ) -> list[str]:
        """Get the top N most relevant photo URLs based on Stage A scoring.

        Args:
            photo_urls: All photo URLs
            stage_a_result: Stage A result with photo scores
            count: Number of top photos to return

        Returns:
            Top N photo URLs sorted by relevance.
        """
        photos_data = stage_a_result.get("photos", [])
        if not photos_data:
            return photo_urls[:count]

        # Sort by relevance score descending
        scored = []
        for p in photos_data:
            idx = p.get("index", -1)
            relevance = p.get("relevance", 0)
            if 0 <= idx < len(photo_urls):
                scored.append((relevance, idx))

        scored.sort(reverse=True)
        top_indices = [idx for _, idx in scored[:count]]

        return [photo_urls[i] for i in top_indices]

    def _merge_stage_b(
        self, stage_a: dict, stage_b: dict, uncertain_categories: list[str]
    ) -> dict:
        """Merge Stage B refinements into Stage A results.

        Stage B values override Stage A for uncertain categories only.
        top_vibes, blurbs, and confidence always come from Stage B if present.
        """
        merged = dict(stage_a)

        refined = stage_b.get("refined_categories", {})
        for cat_key in uncertain_categories:
            if cat_key in refined:
                merged[cat_key] = refined[cat_key]

        # Take top_vibes, blurbs, notes from Stage B
        for key in ("top_vibes", "vibe_short_pt", "vibe_short_en",
                     "vibe_long_pt", "vibe_long_en", "notes"):
            if key in stage_b and stage_b[key]:
                merged[key] = stage_b[key]

        # Update confidence from Stage B
        if "overall_confidence" in stage_b:
            merged["overall_confidence"] = stage_b["overall_confidence"]

        return merged

    def _build_profile(
        self,
        venue_id: str,
        result: dict,
        stage_b_result: dict,
        photos: list,
        photo_urls: list[str],
        stage_b_triggered: bool,
        data_sources: list[str] | None = None,
    ) -> VenueVibeProfile:
        """Convert raw API result dict to v2 VenueVibeProfile."""

        def parse_category(cat_key: str, data) -> TaxonomyCategory:
            if not data or not isinstance(data, dict):
                return TaxonomyCategory()
            raw_labels = data.get("labels", [])[:4]
            validated_labels = validate_category_labels(cat_key, raw_labels)
            evidence_data = data.get("evidence", {})
            return TaxonomyCategory(
                labels=validated_labels,
                confidence=data.get("confidence", 0.0),
                evidence=CategoryEvidence(
                    photo_indices=evidence_data.get("photo_indices", []),
                    review_quotes=evidence_data.get("review_quotes", []),
                ),
            )

        # Build evidence photos (same as v1 — used for photo sorting)
        evidence_photos = []
        for p in result.get("photos", []):
            idx = p.get("index", -1)
            if 0 <= idx < len(photo_urls):
                evidence_photos.append(EvidencePhoto(
                    photo_url=photo_urls[idx],
                    relevance_score=p.get("relevance", 0.0),
                    vibe_appeal=p.get("vibe_appeal", 0.0),
                    photo_type=p.get("type", "other"),
                    evidence_tags=p.get("tags", []),
                ))

        # Classification trace
        classification_trace = [f"{self.stage_a_model}:stage_a"]
        if stage_b_triggered:
            classification_trace.append(f"{self.stage_b_model}:stage_b")

        # Validate top_vibes
        top_vibes = validate_top_vibes(result.get("top_vibes", []))[:6]

        return VenueVibeProfile(
            venue_id=venue_id,
            publico=parse_category("publico", result.get("publico")),
            musica=parse_category("musica", result.get("musica")),
            music_format=parse_category("music_format", result.get("music_format")),
            estilo_do_lugar=parse_category("estilo_do_lugar", result.get("estilo_do_lugar")),
            estetica=parse_category("estetica", result.get("estetica")),
            intencao=parse_category("intencao", result.get("intencao")),
            dress_code=parse_category("dress_code", result.get("dress_code")),
            clima_social=parse_category("clima_social", result.get("clima_social")),
            top_vibes=top_vibes,
            overall_confidence=result.get("overall_confidence", 0.0),
            notes=result.get("notes"),
            vibe_short_pt=result.get("vibe_short_pt"),
            vibe_short_en=result.get("vibe_short_en"),
            vibe_long_pt=result.get("vibe_long_pt"),
            vibe_long_en=result.get("vibe_long_en"),
            data_sources=data_sources or ["photos"],
            evidence_photos=evidence_photos,
            photos_analyzed=len(photo_urls),
            photos_available=len(photos),
            stage_b_triggered=stage_b_triggered,
            uncertainty_reasons=result.get("uncertainty_reasons", []),
            classification_trace=classification_trace,
        )

    def _generate_blurbs_from_facets(self, profile: VenueVibeProfile) -> None:
        """Generate simple template-based blurbs from taxonomy categories.

        Used when Stage B is not triggered or GPT didn't produce blurbs.
        """
        parts_pt = []
        parts_en = []

        # Venue style (most important descriptor)
        ESTILO_EN = {
            "Boteco raiz": "Traditional boteco",
            "Gastrobar": "Gastrobar",
            "Bar tradicional": "Traditional bar",
            "Lounge": "Lounge",
            "Balada": "Nightclub",
            "Club": "Club",
            "Pub": "Pub",
            "Rooftop": "Rooftop bar",
            "Pé na areia": "Beach bar",
            "Beach club": "Beach club",
            "Wine bar": "Wine bar",
            "Coquetelaria": "Cocktail bar",
            "Bar com jogos": "Game bar",
            "Speakeasy": "Speakeasy",
            "Cultural / alternativo": "Cultural space",
            "Inferninho": "Underground dive bar",
        }
        if profile.estilo_do_lugar.labels:
            label = profile.estilo_do_lugar.labels[0]
            parts_pt.append(label)
            parts_en.append(ESTILO_EN.get(label, label))

        # Social climate
        CLIMA_PT_SUFFIX = {
            "Intimista": "com clima intimista",
            "Social": "com ambiente social",
            "Animado": "com clima animado",
            "Agitado": "agitado",
            "Fervendo": "fervendo",
            "Tranquilo": "com clima tranquilo",
        }
        CLIMA_EN_SUFFIX = {
            "Intimista": "with intimate vibes",
            "Social": "with social atmosphere",
            "Animado": "with lively vibes",
            "Agitado": "with high energy",
            "Fervendo": "on fire",
            "Tranquilo": "with chill vibes",
        }
        if profile.clima_social.labels:
            label = profile.clima_social.labels[0]
            if label in CLIMA_PT_SUFFIX:
                parts_pt.append(CLIMA_PT_SUFFIX[label])
                parts_en.append(CLIMA_EN_SUFFIX[label])

        # Music format
        FMT_EN = {
            "DJ": "with DJ",
            "Som ao vivo": "with live music",
            "Banda ao vivo": "with live band",
            "Roda de samba": "with samba circle",
        }
        if profile.music_format.labels:
            fmt = profile.music_format.labels[0]
            if fmt in FMT_EN:
                parts_pt.append(f"com {fmt.lower()}")
                parts_en.append(FMT_EN[fmt])

        if parts_pt:
            profile.vibe_short_pt = " ".join(parts_pt)[:100]
        if parts_en:
            profile.vibe_short_en = " ".join(parts_en)[:100]
