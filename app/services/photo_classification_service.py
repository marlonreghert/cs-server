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

import base64
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

# Fallback unit price, used only when a client cannot report token usage.
#
# MEASURED, not assumed: ~2,974 input tokens per photo at batch_size=10. The
# "85 tokens for a detail=low image" figure this was originally built on is the
# **gpt-4o** number — gpt-4o-mini bills a low-detail image at roughly 2,833
# tokens, about 33x more, and that single wrong constant made the whole
# catalogue estimate ~9x too cheap. Which is precisely why cost is now read off
# `response.usage` instead of multiplied out from a constant.
DEFAULT_COST_PER_PHOTO_USD = 0.00054

# gpt-4o-mini list rates. UNVERIFIED against OpenAI's current rate card, which is
# exactly why they are settings — the same call the Google photo unit price makes
# next door. Wrong rates make the cost panel wrong; they never make a run behave
# differently.
DEFAULT_COST_PER_1K_INPUT_USD = 0.00015
DEFAULT_COST_PER_1K_OUTPUT_USD = 0.0006

UNKNOWN_AUTHORSHIP = ("", "unknown", None)


def _image_reference(photo: dict, *, require_bytes: bool) -> Optional[str]:
    """What to hand the model for this photo: bytes already in hand, encoded
    as a data URI, or — only when the caller allows it — whatever is in
    `photo["url"]`.

    The live archive path attaches `_bytes`/`_content_type` to a photo BEFORE
    calling `annotate(..., require_bytes=True)`, specifically so this never
    asks a provider's CDN to serve OpenAI: Instagram's signed fbcdn links
    return `400 invalid_image_url` when the model tries to fetch them
    server-side, and the classify step is shared across every source, so it
    cannot rely on Google's urls happening to be openly fetchable.

    `require_bytes=True` with no bytes attached returns None rather than
    falling back to the url — a download that already failed would just 400
    again, so that photo gets no verdict instead (source category kept),
    exactly like any other classification miss.

    The one caller that never sets `require_bytes` is `derive_for_archived`
    (re-deriving attributes over an archived run): it has no bytes in
    memory, only `photo["url"]`, which by then is already a data URI read
    back from S3 (`read_image_data_uri` — a presigned url is ~1,900 chars
    and OpenAI refuses it outright, so it was never actually "a url" in the
    fetchable sense either).
    """
    data = photo.get("_bytes")
    if data:
        content_type = photo.get("_content_type") or "image/jpeg"
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
    if require_bytes:
        return None
    return photo.get("url")


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
        cost_per_1k_input_usd: float = DEFAULT_COST_PER_1K_INPUT_USD,
        cost_per_1k_output_usd: float = DEFAULT_COST_PER_1K_OUTPUT_USD,
    ):
        self.client = client
        self.model = model
        # Two bars, and the attribute one is higher on purpose. A wrong category
        # misfiles a photo; a wrong attribute is read as a fact about the venue.
        self.confidence_threshold = float(confidence_threshold)
        self.attribute_confidence_threshold = float(attribute_confidence_threshold)
        self.batch_size = max(1, int(batch_size))
        # Priced from the token counts the API reports. `cost_per_photo_usd`
        # survives only as the fallback for a client that cannot report them.
        self.cost_per_photo_usd = float(cost_per_photo_usd)
        self.cost_per_1k_input_usd = float(cost_per_1k_input_usd)
        self.cost_per_1k_output_usd = float(cost_per_1k_output_usd)

    # ── the live path ────────────────────────────────────────────────────────
    async def annotate(
        self, photos: list[dict], *, derive_attributes: bool = True,
        recategorize: bool = True, venue_id: str = "", require_bytes: bool = False,
    ) -> dict:
        """Classify a venue's photos in one call and attach what came back.

        Photos are annotated IN PLACE — the archive pipeline then files each one
        under `photo["category"]`. Returns the counts and the estimated cost.

        `require_bytes=True` (the live archive path) sends only photos that
        carry already-downloaded bytes — see `_image_reference`. A photo with
        no bytes is excluded from the model call entirely rather than falling
        back to a url, and gets no verdict (source category kept).
        """
        stats = {"classified": 0, "attributed": 0, "cost_usd": 0.0,
                 "input_tokens": 0, "output_tokens": 0}
        if not photos:
            return stats

        # Kept whatever happens next: once the classifier owns `category`, this
        # is the only record of what the source called it, and it is what the
        # photo falls back to.
        for photo in photos:
            photo.setdefault("source_category", photo.get("category"))

        refs: list[str] = []
        ref_indexes: list[int] = []
        for i, photo in enumerate(photos):
            ref = _image_reference(photo, require_bytes=require_bytes)
            if ref is not None:
                refs.append(ref)
                ref_indexes.append(i)

        dense_verdicts = (
            await self._classify(refs, venue_id, derive_attributes) if refs else []
        )
        verdicts: list[dict] = [{}] * len(photos)
        for idx, verdict in zip(ref_indexes, dense_verdicts):
            verdicts[idx] = verdict

        for photo, verdict in zip(photos, verdicts):
            classified, attributed = self._apply_verdict(
                photo, verdict, derive_attributes, recategorize
            )
            stats["classified"] += int(classified)
            stats["attributed"] += int(attributed)

        stats.update(self._price(len(photos)))
        PHOTO_CLASSIFICATION_COST_USD.inc(stats["cost_usd"])
        return stats

    def _price(self, photo_count: int) -> dict:
        """What the call actually cost, from the tokens the API reported.

        Falls back to the per-photo estimate only when the client cannot report
        tokens — an old client, or a failed call that never got a usage block.
        The estimate is a stand-in for a number we normally have exactly, so a
        run that quietly falls back should be visible rather than silently
        plausible.
        """
        take = getattr(self.client, "take_tokens", None)
        used = take() if callable(take) else None
        if not used or not (used.get("input") or used.get("output")):
            return {
                "cost_usd": round(photo_count * self.cost_per_photo_usd, 6),
                "input_tokens": 0, "output_tokens": 0, "cost_is_estimated": True,
            }
        cost = (
            used["input"] / 1000 * self.cost_per_1k_input_usd
            + used["output"] / 1000 * self.cost_per_1k_output_usd
        )
        return {
            "cost_usd": round(cost, 6),
            "input_tokens": used["input"],
            "output_tokens": used["output"],
            "cost_is_estimated": False,
        }

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
    async def derive_for_archived(
        self, entries: list[dict], urls: list[str], *, recategorize: bool = False
    ) -> int:
        """Re-classify already-archived photos from their stored copies.

        Same single call, reading each photo's bytes back from S3 as a data
        URI (`urls` here are already `read_image_data_uri` results, not
        provider links — a presigned url is ~1,900 chars and OpenAI refuses
        it outright) — which is what lets the schema grow without
        re-fetching, and without a provider bill.

        The call returns a category too, and it is **deliberately discarded**:
        the category is in the S3 key of an object that already exists, so
        writing a new one into the manifest would leave an entry disagreeing
        with its own key. Recategorizing means moving objects, which is a
        different job from extending a schema. `authorship` is likewise carried
        through untouched, exactly as on the live path.
        """
        staged = [dict(entry, url=url) for entry, url in zip(entries, urls)]
        stats = await self.annotate(
            staged, recategorize=recategorize, venue_id="(archived)"
        )
        fields = ["quality", "attributes", "people", "likely_authorship"]
        if recategorize:
            # Only for photos the classifier never reached: a live-path failure
            # leaves `category` equal to `source_category`, and those entries
            # carry no classifier fields at all. Rewriting a category that was
            # already decided would be churn, not repair.
            fields += ["category", "classification_confidence"]
        for entry, photo in zip(entries, staged):
            for field in fields:
                if photo.get(field) is not None:
                    entry[field] = photo[field]
        return stats["attributed"] if not recategorize else stats["classified"]
