"""Label fetched photos with our own categories and attributes.

Sits between the fetch and the store in the archive pipeline, so a photo lands
in the right folder the first time — no second write, no object copy.

Three rules carry the behaviour and must not be relaxed:

1. **Classification is an enhancement, not a dependency.** If the model fails,
   the photo keeps the category its source gave it and is still archived.
   Losing a photo we already paid to fetch because a classifier was unavailable
   is the wrong trade.
2. **A failure and a low confidence are different outcomes.** A failure keeps
   the source's category; a low-confidence verdict is filed as `other`. Filing
   a failure as `other` would erase what the source did know, and guessing on
   low confidence would invent what nobody knows.
3. **`authorship` is never written here.** It is the provider's fact. The
   model's read goes in `likely_authorship`, and only where the provider had no
   answer, so a guess can never be mistaken for the fact.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.metrics import (
    PHOTO_CLASSIFICATION_COST_USD,
    PHOTO_CLASSIFICATION_FALLBACKS_TOTAL,
    PHOTO_CLASSIFICATION_TOTAL,
)
from app.models.photo_taxonomy import (
    CATEGORY_OTHER,
    PHOTO_CATEGORIES,
    validate_attributes,
    validate_authorship_guess,
    validate_people,
    validate_quality,
)

logger = logging.getLogger(__name__)

# gpt-4o-mini at detail="low" is 85 image tokens plus prompt share and the JSON
# it writes back. Used ONLY to report what a run cost — it is an estimate, and
# the metric is what makes it checkable rather than assumed.
DEFAULT_COST_PER_PHOTO_USD = 0.00006

UNKNOWN_AUTHORSHIP = ("", "unknown", None)


class PhotoClassificationService:
    """Annotates fetched photo dicts in place with category and attributes."""

    def __init__(
        self,
        *,
        client,
        model: Optional[str] = None,
        confidence_threshold: float = 0.6,
        batch_size: int = 10,
        cost_per_photo_usd: float = DEFAULT_COST_PER_PHOTO_USD,
    ):
        self.client = client
        self.model = model
        self.confidence_threshold = float(confidence_threshold)
        self.batch_size = max(1, int(batch_size))
        self.cost_per_photo_usd = float(cost_per_photo_usd)

    # ── the live path ────────────────────────────────────────────────────────
    async def annotate(
        self, photos: list[dict], *, derive_attributes: bool = True,
        venue_id: str = "",
    ) -> dict:
        """Classify a venue's photos and attach their attributes.

        Photos are annotated IN PLACE — the archive pipeline then files each one
        under `photo["category"]`. Returns the counts and the estimated cost.
        """
        stats = {"classified": 0, "attributed": 0, "cost_usd": 0.0}
        if not photos:
            return stats

        # Kept whatever happens next: once the classifier owns `category`, this
        # is the only record of what the source called it, and it is what the
        # photo falls back to.
        for photo in photos:
            photo.setdefault("source_category", photo.get("category"))

        urls = [p.get("url") for p in photos]
        verdicts = await self._classify(urls, venue_id)
        for photo, verdict in zip(photos, verdicts):
            if self._apply_verdict(photo, verdict):
                stats["classified"] += 1

        if derive_attributes:
            stats["attributed"] = await self._attach_attributes(photos, venue_id)

        stats["cost_usd"] = round(
            (stats["classified"] + stats["attributed"]) * self.cost_per_photo_usd, 6
        )
        PHOTO_CLASSIFICATION_COST_USD.inc(stats["cost_usd"])
        return stats

    async def _classify(self, urls: list[str], venue_id: str) -> list[dict]:
        try:
            verdicts = await self.client.classify_photos(
                urls, model=self.model, batch_size=self.batch_size
            )
        except Exception as e:  # noqa: BLE001 — degrade, never fail the venue
            logger.error(
                f"[PhotoClassification] classification failed for {venue_id}; "
                f"photos keep their source category: {e}"
            )
            PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="classify_error").inc(
                len(urls)
            )
            return []
        # Short responses are padded rather than truncating the venue's photos.
        return list(verdicts) + [{}] * max(0, len(urls) - len(verdicts))

    def _apply_verdict(self, photo: dict, verdict: Any) -> bool:
        """True when the photo got a real category out of this."""
        if not isinstance(verdict, dict) or not verdict:
            # No verdict at all — the model failed or said nothing. The source's
            # category stands; this is NOT `other`.
            PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="no_verdict").inc()
            return False

        quality = validate_quality(verdict.get("quality"))
        if quality:
            photo["quality"] = quality

        # Only where the provider was silent. A provider answer is a fact and
        # outranks the model's read every time.
        if photo.get("authorship") in UNKNOWN_AUTHORSHIP:
            guess = validate_authorship_guess(verdict.get("likely_authorship"))
            if guess:
                photo["likely_authorship"] = guess

        category = str(verdict.get("category") or "")
        try:
            confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        if category not in PHOTO_CATEGORIES:
            PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="unknown_category").inc()
            photo["category"] = CATEGORY_OTHER
        elif confidence < self.confidence_threshold:
            PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="low_confidence").inc()
            photo["category"] = CATEGORY_OTHER
        else:
            photo["category"] = category
        photo["classification_confidence"] = round(confidence, 3)

        if photo["category"] == CATEGORY_OTHER:
            # WHY it is `other` comes from the first pass, because the second
            # one deliberately skips `other` entirely. It costs nothing extra —
            # pass 1 already looked at the image — and it is what lets an
            # `other` photo be found and reclassified later without re-billing.
            kind = validate_attributes(CATEGORY_OTHER, verdict)
            if kind:
                photo["attributes"] = kind

        PHOTO_CLASSIFICATION_TOTAL.labels(category=photo["category"]).inc()
        return True

    async def _attach_attributes(self, photos: list[dict], venue_id: str) -> int:
        """Second pass, grouped by category. `other` is skipped entirely."""
        groups: dict[str, list[dict]] = {}
        for photo in photos:
            category = photo.get("category")
            if category in PHOTO_CATEGORIES and category != CATEGORY_OTHER:
                groups.setdefault(category, []).append(photo)

        attributed = 0
        for category, group in groups.items():
            urls = [p.get("url") for p in group]
            try:
                results = await self.client.derive_attributes(
                    category, urls, model=self.model, batch_size=self.batch_size
                )
            except Exception as e:  # noqa: BLE001
                # A categorized photo with no attributes is still useful, and
                # re-derivable from S3 later without paying a provider again.
                logger.warning(
                    f"[PhotoClassification] attributes failed for {venue_id} "
                    f"({category}); photos stay categorized: {e}"
                )
                PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(
                    reason="attributes_error"
                ).inc(len(group))
                continue
            attributed += self._apply_attributes(category, group, results)
        return attributed

    def _apply_attributes(self, category: str, group: list[dict], results) -> int:
        applied = 0
        for photo, result in zip(group, list(results or [])):
            if not isinstance(result, dict) or not result:
                continue
            attributes = validate_attributes(category, result.get("attributes"))
            if attributes:
                photo["attributes"] = attributes
            people = validate_people(result.get("people"))
            if people:
                photo["people"] = people
            if attributes or people:
                applied += 1
        return applied

    # ── the backfill path ────────────────────────────────────────────────────
    async def derive_for_archived(self, entries: list[dict], urls: list[str]) -> int:
        """Attach attributes to already-archived photos, from their stored copies.

        Same second pass, reading presigned S3 urls instead of provider links —
        which is what lets the attribute schema grow without re-fetching, and
        without a provider bill.
        """
        staged = [
            dict(entry, url=url) for entry, url in zip(entries, urls)
        ]
        attributed = await self._attach_attributes(staged, venue_id="(archived)")
        for entry, photo in zip(entries, staged):
            for field in ("attributes", "people"):
                if photo.get(field):
                    entry[field] = photo[field]
        return attributed
