"""Service for AI-powered venue vibe classification from photos.

2-stage hybrid pipeline:
- Stage A (gpt-4o-mini): Cheap photo scoring + vibe extraction
- Stage B (gpt-4o): Expensive refinement for uncertain venues

Reuses photos already cached in Redis by PhotoEnrichmentService.
"""
import asyncio
import logging
from typing import Optional

from app.api.openai_vibe_client import OpenAIVibeClient
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.vibe_profile import (
    VenueVibeProfile,
    FacetScore,
    EvidencePhoto,
    EnergyDynamics,
    PricePositioning,
    EnvironmentAesthetic,
    CrowdDensity,
    MusicProfile,
    SafetyComfort,
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

        # 4. Run Stage A
        logger.info(
            f"[VibeClassifier] Stage A for {venue_id} ({venue_name}): "
            f"{len(photo_urls)} photos"
        )
        stage_a_result = await self.openai_client.classify_venue_vibes_stage_a(
            photo_urls=photo_urls,
            venue_name=venue_name,
            venue_type=venue_type or "",
            model=self.stage_a_model,
        )

        if not stage_a_result:
            logger.warning(f"[VibeClassifier] Stage A returned empty for {venue_id}")
            VIBE_CLASSIFIER_RESULTS.labels(result="error").inc()
            return None

        # 5. Check uncertainty gate
        should_escalate, uncertain_facets = self._should_escalate(stage_a_result)
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
                f"uncertain={uncertain_facets}, photos={len(top_urls)}"
            )

            stage_b_result = await self.openai_client.classify_venue_vibes_stage_b(
                photo_urls=top_urls,
                stage_a_result=stage_a_result,
                uncertain_facets=uncertain_facets,
                venue_name=venue_name,
                model=self.stage_b_model,
            )

            if stage_b_result:
                # Merge Stage B refinements into Stage A
                stage_a_result = self._merge_stage_b(
                    stage_a_result, stage_b_result, uncertain_facets
                )

        # 7. Build profile
        profile = self._build_profile(
            venue_id=venue_id,
            result=stage_a_result,
            stage_b_result=stage_b_result,
            photos=photos,
            photo_urls=photo_urls,
            stage_b_triggered=stage_b_triggered,
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
            f"modes={[m.label for m in profile.core_venue_modes]}, "
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
        """Check if Stage B should be triggered.

        Returns:
            (should_escalate, list_of_uncertain_facet_names)
        """
        uncertain = []
        reasons = []

        # 1. Low overall confidence
        confidence = stage_a_result.get("overall_confidence", 0)
        if confidence < self.escalation_threshold:
            reasons.append("low_confidence")
            uncertain.extend(["core_venue_modes", "crowd_types", "energy", "environment"])

        # 2. Explicit contradiction rules
        energy = stage_a_result.get("energy", {})
        energy_level = energy.get("energy_level")
        party_intensity = energy.get("party_intensity")
        dance_likelihood = energy.get("dance_likelihood")

        crowd_density = stage_a_result.get("crowd_density", {})
        density_visible = crowd_density.get("crowd_density_visible")

        price = stage_a_result.get("price", {})
        price_score = price.get("price_score")

        safety = stage_a_result.get("safety", {})
        perceived_safety = safety.get("perceived_safety")

        core_modes = [m.get("label", "") for m in stage_a_result.get("core_venue_modes", [])]
        crowd_types = [c.get("label", "") for c in stage_a_result.get("crowd_types", [])]

        # Rule 1: Low energy but high party
        if (energy_level is not None and party_intensity is not None
                and energy_level < 3 and party_intensity > 7):
            reasons.append("contradictions")
            uncertain.extend(["energy"])

        # Rule 2: Low crowd density but high dance likelihood
        if (density_visible is not None and dance_likelihood is not None
                and density_visible < 3 and dance_likelihood > 7):
            reasons.append("contradictions")
            uncertain.extend(["crowd_density", "energy"])

        # Rule 3: Restaurant with very high party intensity
        if "dining_restaurant" in core_modes and party_intensity is not None and party_intensity > 8:
            reasons.append("contradictions")
            uncertain.extend(["core_venue_modes", "energy"])

        # Rule 4: Expensive + boteco_raiz
        if price_score is not None and price_score > 8 and "boteco_raiz" in core_modes:
            reasons.append("contradictions")
            uncertain.extend(["price", "core_venue_modes"])

        # Rule 5: Low safety + family_friendly
        if (perceived_safety is not None and perceived_safety < 4
                and "family_friendly" in crowd_types):
            reasons.append("contradictions")
            uncertain.extend(["safety", "crowd_types"])

        should_escalate = len(reasons) > 0
        if should_escalate:
            # Log trigger reasons
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
        self, stage_a: dict, stage_b: dict, uncertain_facets: list[str]
    ) -> dict:
        """Merge Stage B refinements into Stage A results.

        Stage B values override Stage A for uncertain facets only.
        Blurbs always come from Stage B if present.
        """
        merged = dict(stage_a)

        refined = stage_b.get("refined_facets", {})
        for facet_name in uncertain_facets:
            if facet_name in refined:
                merged[facet_name] = refined[facet_name]

        # Take blurbs from Stage B
        for key in ("vibe_short_pt", "vibe_short_en", "vibe_long_pt", "vibe_long_en"):
            if key in stage_b and stage_b[key]:
                merged[key] = stage_b[key]

        # Update confidence from Stage B
        if "overall_confidence" in stage_b:
            merged["overall_confidence"] = stage_b["overall_confidence"]

        if "uncertainty_reasons" in stage_b:
            merged["uncertainty_reasons"] = stage_b["uncertainty_reasons"]

        return merged

    def _build_profile(
        self,
        venue_id: str,
        result: dict,
        stage_b_result: dict,
        photos: list,
        photo_urls: list[str],
        stage_b_triggered: bool,
    ) -> VenueVibeProfile:
        """Convert raw API result dict to Pydantic VenueVibeProfile."""

        def parse_facet_scores(items: list) -> list[FacetScore]:
            if not items:
                return []
            return [
                FacetScore(
                    label=item.get("label", ""),
                    confidence=item.get("confidence", 0.0),
                )
                for item in items
                if item.get("label")
            ]

        # Build evidence photos
        evidence_photos = []
        for p in result.get("photos", []):
            idx = p.get("index", -1)
            if 0 <= idx < len(photo_urls):
                evidence_photos.append(EvidencePhoto(
                    photo_url=photo_urls[idx],
                    relevance_score=p.get("relevance", 0.0),
                    photo_type=p.get("type", "other"),
                    evidence_tags=p.get("tags", []),
                ))

        # Parse energy
        energy_data = result.get("energy", {})
        energy = EnergyDynamics(
            energy_level=energy_data.get("energy_level"),
            party_intensity=energy_data.get("party_intensity"),
            conversation_focus=energy_data.get("conversation_focus"),
            date_friendly=energy_data.get("date_friendly"),
            group_friendly=energy_data.get("group_friendly"),
            networking_friendly=energy_data.get("networking_friendly"),
            dance_likelihood=energy_data.get("dance_likelihood"),
        )

        # Parse price
        price_data = result.get("price", {})
        price = PricePositioning(
            price_score=price_data.get("price_score"),
            price_tier=price_data.get("price_tier"),
            predictability_score=price_data.get("predictability_score"),
        )

        # Parse environment
        env_data = result.get("environment", {})
        environment = EnvironmentAesthetic(
            aesthetic_score=env_data.get("aesthetic_score"),
            instagrammable_score=env_data.get("instagrammable_score"),
            cleanliness_score=env_data.get("cleanliness_score"),
            comfort_score=env_data.get("comfort_score"),
            indoor_outdoor=env_data.get("indoor_outdoor"),
            lighting=env_data.get("lighting"),
            decor_styles=parse_facet_scores(env_data.get("decor_styles", [])),
        )

        # Parse crowd density
        cd_data = result.get("crowd_density", {})
        crowd_density = CrowdDensity(
            crowd_density_visible=cd_data.get("crowd_density_visible"),
            seating_vs_standing_ratio=cd_data.get("seating_vs_standing_ratio"),
        )

        # Parse music
        music_data = result.get("music", {})
        music = MusicProfile(
            music_prominence=music_data.get("music_prominence"),
            genres=parse_facet_scores(music_data.get("genres", [])),
        )

        # Parse safety
        safety_data = result.get("safety", {})
        safety = SafetyComfort(
            perceived_safety=safety_data.get("perceived_safety"),
            accessibility_score=safety_data.get("accessibility_score"),
            crowd_diversity_signal=safety_data.get("crowd_diversity_signal"),
        )

        # Build per_label_confidence from multi-label facets
        per_label_confidence = {}
        for facet in result.get("core_venue_modes", []):
            if facet.get("label"):
                per_label_confidence[facet["label"]] = facet.get("confidence", 0.0)
        for facet in result.get("crowd_types", []):
            if facet.get("label"):
                per_label_confidence[facet["label"]] = facet.get("confidence", 0.0)

        # Classification trace
        classification_trace = [f"{self.stage_a_model}:stage_a"]
        if stage_b_triggered:
            classification_trace.append(f"{self.stage_b_model}:stage_b")

        return VenueVibeProfile(
            venue_id=venue_id,
            core_venue_modes=parse_facet_scores(result.get("core_venue_modes", [])),
            crowd_types=parse_facet_scores(result.get("crowd_types", [])),
            energy=energy,
            price=price,
            environment=environment,
            crowd_density=crowd_density,
            music=music,
            safety=safety,
            vibe_keywords=result.get("vibe_keywords", []),
            vibe_short_pt=result.get("vibe_short_pt"),
            vibe_short_en=result.get("vibe_short_en"),
            vibe_long_pt=result.get("vibe_long_pt"),
            vibe_long_en=result.get("vibe_long_en"),
            evidence_photos=evidence_photos,
            per_label_confidence=per_label_confidence,
            photos_analyzed=len(photo_urls),
            photos_available=len(photos),
            overall_confidence=result.get("overall_confidence", 0.0),
            stage_b_triggered=stage_b_triggered,
            uncertainty_reasons=result.get("uncertainty_reasons", []),
            classification_trace=classification_trace,
        )

    def _generate_blurbs_from_facets(self, profile: VenueVibeProfile) -> None:
        """Generate simple template-based blurbs from facets (no API call).

        Used when Stage B is not triggered.
        """
        parts_pt = []
        parts_en = []

        # Core mode
        if profile.core_venue_modes:
            top_mode = profile.core_venue_modes[0].label
            mode_labels_pt = {
                "dining_restaurant": "Restaurante",
                "bar_social": "Bar",
                "club_party": "Casa noturna",
                "lounge_cocktail": "Lounge de coquetéis",
                "boteco_raiz": "Boteco",
                "cafe_brunch": "Café",
                "outdoor_beer_garden": "Espaço ao ar livre",
                "live_music_venue": "Casa de shows",
                "cultural_space": "Espaço cultural",
                "mixed_use": "Espaço multifuncional",
            }
            mode_labels_en = {
                "dining_restaurant": "Restaurant",
                "bar_social": "Bar",
                "club_party": "Nightclub",
                "lounge_cocktail": "Cocktail lounge",
                "boteco_raiz": "Traditional bar",
                "cafe_brunch": "Café",
                "outdoor_beer_garden": "Beer garden",
                "live_music_venue": "Live music venue",
                "cultural_space": "Cultural space",
                "mixed_use": "Multi-use space",
            }
            parts_pt.append(mode_labels_pt.get(top_mode, top_mode))
            parts_en.append(mode_labels_en.get(top_mode, top_mode))

        # Energy descriptor
        if profile.energy.energy_level is not None:
            if profile.energy.energy_level >= 7:
                parts_pt.append("com energia alta")
                parts_en.append("with high energy")
            elif profile.energy.energy_level <= 3:
                parts_pt.append("com ambiente tranquilo")
                parts_en.append("with a calm atmosphere")

        # Music
        if profile.music.music_prominence is not None and profile.music.music_prominence >= 6:
            parts_pt.append("e música ao vivo")
            parts_en.append("and live music")

        # Price tier
        if profile.price.price_tier:
            tier_pt = {
                "budget": "preços acessíveis",
                "mid_range": "preços médios",
                "upscale": "ambiente sofisticado",
                "premium": "alto padrão",
            }
            tier_en = {
                "budget": "affordable prices",
                "mid_range": "mid-range prices",
                "upscale": "upscale atmosphere",
                "premium": "premium venue",
            }
            if profile.price.price_tier in tier_pt:
                parts_pt.append(tier_pt[profile.price.price_tier])
                parts_en.append(tier_en[profile.price.price_tier])

        if parts_pt:
            profile.vibe_short_pt = " ".join(parts_pt)[:100]
        if parts_en:
            profile.vibe_short_en = " ".join(parts_en)[:100]

        # Keywords from labels
        keywords = []
        for mode in profile.core_venue_modes[:2]:
            keywords.append(mode.label.replace("_", " "))
        for ct in profile.crowd_types[:2]:
            keywords.append(ct.label.replace("_", " "))
        if profile.energy.energy_level is not None:
            if profile.energy.energy_level >= 7:
                keywords.append("energetic")
            elif profile.energy.energy_level <= 3:
                keywords.append("calm")
        profile.vibe_keywords = keywords[:8]
