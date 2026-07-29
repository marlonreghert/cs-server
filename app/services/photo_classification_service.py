"""Label fetched photos with our own categories and attributes.

Sits between the fetch and the store in the archive pipeline, so a photo lands
in the right folder the first time — no second write, no object copy.

Four rules carry the behaviour and must not be relaxed:

1. **Classification is an enhancement, not a dependency.** If the model fails,
   the photo keeps the category its source gave it and is still archived.
   Losing a photo we already paid to fetch because a classifier was unavailable
   is the wrong trade.
2. **A failure and a low confidence are different outcomes.** A failure keeps
   the source's category; a low-confidence verdict is filed as `other`. Filing
   a failure as `other` would erase what the source did know, and guessing on
   low confidence would invent what nobody knows.
3. **Attribute confidence is per attribute.** The model can be certain a room
   is a bar and unsure whether it has screens. Anything under the bar becomes
   `not_classified` on its own, without dragging its neighbours down with it.
4. **`authorship` is never written here.** It is the provider's fact. The
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
    NOT_CLASSIFIED,
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
        attribute_confidence_threshold: float = 0.8,
        batch_size: int = 10,
        cost_per_photo_usd: float = DEFAULT_COST_PER_PHOTO_USD,
    ):
        self.client = client
        self.model = model
        # Two bars, and the attribute one is higher on purpose. A wrong category
        # misfiles a photo; a wrong attribute is read as a fact about the venue.
        self.confidence_threshold = float(confidence_threshold)
        self.attribute_confidence_threshold = float(attribute_confidence_threshold)
        self.batch_size = max(1, int(batch_size))
        self.cost_per_photo_usd = float(cost_per_photo_usd)

    # ── the live path ────────────────────────────────────────────────────────
    async def annotate(
        self, photos: list[dict], *, derive_attributes: bool = True,
        recategorize: bool = True, venue_id: str = "",
    ) -> dict:
        """Classify a venue's photos in one call and attach what came back.

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
        verdicts = await self._classify(urls, venue_id, derive_attributes)
        for photo, verdict in zip(photos, verdicts):
            classified, attributed = self._apply_verdict(
                photo, verdict, derive_attributes, recategorize
            )
            stats["classified"] += int(classified)
            stats["attributed"] += int(attributed)

        stats["cost_usd"] = round(
            stats["classified"] * self.cost_per_photo_usd, 6
        )
        PHOTO_CLASSIFICATION_COST_USD.inc(stats["cost_usd"])
        return stats

    async def _classify(
        self, urls: list[str], venue_id: str, with_attributes: bool
    ) -> list[dict]:
        try:
            verdicts = await self.client.classify_photos(
                urls, model=self.model, batch_size=self.batch_size,
                with_attributes=with_attributes,
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

    def _apply_verdict(
        self, photo: dict, verdict: Any, derive_attributes: bool,
        recategorize: bool = True,
    ) -> tuple[bool, bool]:
        """(got a category, got any attribute worth storing)."""
        if not isinstance(verdict, dict) or not verdict:
            # No verdict at all — the model failed or said nothing. The source's
            # category stands; this is NOT `other`.
            PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="no_verdict").inc()
            return False, False

        quality = validate_quality(
            verdict.get("quality"), self.attribute_confidence_threshold
        )
        photo["quality"] = quality

        # Only where the provider was silent. A provider answer is a fact and
        # outranks the model's read every time.
        if photo.get("authorship") in UNKNOWN_AUTHORSHIP:
            guess = validate_authorship_guess(
                verdict.get("likely_authorship"), self.attribute_confidence_threshold
            )
            if guess:
                photo["likely_authorship"] = guess

        category = str(verdict.get("category") or "")
        try:
            confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        if not recategorize:
            # Re-reading an archived photo: its category is in the S3 key of an
            # object that already exists, so the verdict's category is dropped
            # and the attributes are validated against the category the photo
            # is actually filed under.
            return True, (self._apply_attributes(photo, verdict)
                          if derive_attributes else False)

        if category not in PHOTO_CATEGORIES:
            PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="unknown_category").inc()
            photo["category"] = CATEGORY_OTHER
        elif confidence < self.confidence_threshold:
            PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(reason="low_confidence").inc()
            photo["category"] = CATEGORY_OTHER
        else:
            photo["category"] = category
        photo["classification_confidence"] = round(confidence, 3)

        PHOTO_CLASSIFICATION_TOTAL.labels(category=photo["category"]).inc()
        if not derive_attributes:
            return True, False
        return True, self._apply_attributes(photo, verdict)

    def _apply_attributes(self, photo: dict, verdict: dict) -> bool:
        """Attach the category's attributes and, if anyone is visible, the people.

        Every field of the schema is written, as a value or as `not_classified`.
        A known unknown is worth storing: it says the question was asked, which
        an absent key does not, and it is what makes "how much of the catalogue
        can we actually read" a query rather than a guess.
        """
        attributes = validate_attributes(
            photo["category"], verdict.get("attributes"),
            self.attribute_confidence_threshold,
        )
        if attributes:
            photo["attributes"] = attributes
        people = validate_people(
            verdict.get("people"), self.attribute_confidence_threshold
        )
        if people:
            photo["people"] = people

        known = [v for v in list(attributes.values()) + list(people.values())
                 if v != NOT_CLASSIFIED]
        if not known:
            PHOTO_CLASSIFICATION_FALLBACKS_TOTAL.labels(
                reason="no_confident_attribute"
            ).inc()
        return bool(known)

    # ── the backfill path ────────────────────────────────────────────────────
    async def derive_for_archived(self, entries: list[dict], urls: list[str]) -> int:
        """Re-classify already-archived photos from their stored copies.

        Same single call, reading presigned S3 urls instead of provider links —
        which is what lets the schema grow without re-fetching, and without a
        provider bill.

        The call returns a category too, and it is **deliberately discarded**:
        the category is in the S3 key of an object that already exists, so
        writing a new one into the manifest would leave an entry disagreeing
        with its own key. Recategorizing means moving objects, which is a
        different job from extending a schema. `authorship` is likewise carried
        through untouched, exactly as on the live path.
        """
        staged = [dict(entry, url=url) for entry, url in zip(entries, urls)]
        stats = await self.annotate(
            staged, recategorize=False, venue_id="(archived)"
        )
        for entry, photo in zip(entries, staged):
            for field in ("quality", "attributes", "people", "likely_authorship"):
                if photo.get(field) is not None:
                    entry[field] = photo[field]
        return stats["attributed"]
