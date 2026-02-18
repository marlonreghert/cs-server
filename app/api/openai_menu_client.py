"""OpenAI GPT-4o vision client for menu data extraction.

Sends menu photos to GPT-4o and extracts structured menu data
(items, prices, descriptions, sections) as JSON.
"""
import json
import logging
import re
import time

from openai import AsyncOpenAI

from app.models.menu import MenuSection, MenuItem
from app.metrics import (
    OPENAI_API_CALLS_TOTAL,
    OPENAI_API_CALL_DURATION_SECONDS,
)

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """## Role
You are an advanced OCR and Data Extraction Specialist for the food & beverage industry.
You will receive multiple photos from a venue's Google Maps listing. These photos are a
mixed set — some may be menus/cardápios, others may be food, ambiance, etc.

## Objective
1. IDENTIFY which images contain menu/price information (cardápio, tabela de preços, etc.)
2. EXTRACT every single menu item, price, description, and modifier from those images.
3. SKIP images that are not menus (food photos, ambiance, selfies, etc.)
If none of the images contain menu data, return an empty menu_sections array.

## Extraction Rules
1. Hierarchy: Group items under Sections (e.g., "Entradas", "Burgers", "Bebidas").
   If section name missing, infer from context or use "General".
2. Item Details: name, description, price(s).
3. Complex Pricing: sizes as [{label, price}] array.
4. Modifiers & Add-ons: "Adicionais" as separate objects.
5. Dietary Tags: (V), (VG), "Sem Glúten", 🌶️ → tags array.
6. Formatting: Prices as floats, text in original language (Portuguese).

## JSON Schema
{
  "menu_sections": [{
    "section_name": "...",
    "items": [{
      "name": "...",
      "description": "...",
      "prices": [{"label": "...", "price": 22.00}],
      "dietary_tags": [],
      "is_available": true
    }]
  }],
  "metadata": {
    "currency_detected": "BRL",
    "last_updated_date": null,
    "menu_images_found": 0
  }
}"""


class OpenAIMenuClient:
    """Async client for OpenAI GPT-4o menu extraction."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key)

    async def close(self):
        """Close the OpenAI client."""
        await self.client.close()

    async def extract_menu_from_photos(
        self, photo_urls: list[str]
    ) -> tuple[list[MenuSection], str | None, str]:
        """Extract structured menu data from photo URLs using GPT-4o vision.

        Args:
            photo_urls: List of presigned S3 URLs for menu photos

        Returns:
            Tuple of (sections, currency_detected, raw_response)
        """
        # Build content blocks: text prompt + image URLs
        content = [{"type": "text", "text": EXTRACTION_PROMPT}]
        for url in photo_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url, "detail": "high"},
            })

        start_time = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
                max_tokens=4096,
                response_format={"type": "json_object"},
            )

            duration = time.perf_counter() - start_time
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint="menu_extraction").observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint="menu_extraction", status="success").inc()

            raw_text = response.choices[0].message.content or ""
            logger.info(
                f"[OpenAIMenu] Extraction complete in {duration:.1f}s, "
                f"tokens: {response.usage.total_tokens if response.usage else '?'}"
            )

            return self._parse_response(raw_text)

        except Exception as e:
            duration = time.perf_counter() - start_time
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint="menu_extraction").observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint="menu_extraction", status="error").inc()
            logger.error(f"[OpenAIMenu] Extraction failed: {e}")
            raise

    def _parse_response(
        self, raw_text: str
    ) -> tuple[list[MenuSection], str | None, str]:
        """Parse GPT-4o JSON response into MenuSection objects.

        Args:
            raw_text: Raw JSON string from GPT-4o

        Returns:
            Tuple of (sections, currency_detected, raw_text)
        """
        # Strip markdown code fences if present
        cleaned = raw_text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"[OpenAIMenu] Failed to parse JSON response: {e}")
            return [], None, raw_text

        # Extract metadata
        metadata = data.get("metadata", {})
        currency = metadata.get("currency_detected")

        # Parse sections
        sections = []
        for section_data in data.get("menu_sections", []):
            items = []
            for item_data in section_data.get("items", []):
                items.append(MenuItem(
                    name=item_data.get("name", ""),
                    description=item_data.get("description"),
                    prices=item_data.get("prices", []),
                    dietary_tags=item_data.get("dietary_tags", []),
                    modifiers=item_data.get("modifiers", []),
                ))
            sections.append(MenuSection(
                name=section_data.get("section_name", "General"),
                items=items,
            ))

        logger.info(
            f"[OpenAIMenu] Parsed {len(sections)} sections with "
            f"{sum(len(s.items) for s in sections)} items"
        )
        return sections, currency, raw_text

    async def classify_menu_photos(
        self,
        photo_urls: list[str],
        model: str = "gpt-4o-mini",
        confidence_threshold: float = 0.6,
    ) -> list[int]:
        """Classify which photos are menus using GPT-4o-mini vision.

        Sends all photos in a single call with detail="low" (cheap thumbnails).
        Returns indices of photos that are classified as menus.

        On error, returns all indices (graceful degradation — let GPT-4o
        handle the full set rather than losing data).

        Args:
            photo_urls: List of image URLs (presigned S3 URLs)
            model: Model to use for classification (default: gpt-4o-mini)
            confidence_threshold: Minimum confidence to consider a photo a menu

        Returns:
            List of indices into photo_urls that are classified as menus.
        """
        all_indices = list(range(len(photo_urls)))

        if not photo_urls:
            return []

        filter_prompt = (
            "You will receive photos from a restaurant's Google Maps listing. "
            "For each image, determine if it shows a physical menu, cardápio, "
            "price list, price board, or tabela de preços for a restaurant/bar/cafe.\n\n"
            "Reply ONLY with a JSON object:\n"
            '{"results": [{"index": 0, "is_menu": true, "confidence": 0.95}, ...]}\n\n'
            "Rules:\n"
            "- Food plates, drinks, ambiance, selfies, exterior shots are NOT menus\n"
            "- Chalkboards or wall boards with prices ARE menus\n"
            "- Digital displays showing prices ARE menus\n"
            "- Include one entry per image, in order"
        )

        content = [{"type": "text", "text": filter_prompt}]
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
                temperature=0.1,
                max_tokens=1024,
                response_format={"type": "json_object"},
            )

            duration = time.perf_counter() - start_time
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint="photo_filter").observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint="photo_filter", status="success").inc()

            raw_text = response.choices[0].message.content or ""
            logger.info(
                f"[OpenAIMenu] Photo classification complete in {duration:.1f}s, "
                f"tokens: {response.usage.total_tokens if response.usage else '?'}"
            )

            return self._parse_filter_response(raw_text, len(photo_urls), confidence_threshold)

        except Exception as e:
            duration = time.perf_counter() - start_time
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint="photo_filter").observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint="photo_filter", status="error").inc()
            logger.error(f"[OpenAIMenu] Photo classification failed: {e}")
            # Graceful degradation: return all indices
            return all_indices

    def _parse_filter_response(
        self, raw_text: str, count: int, confidence_threshold: float
    ) -> list[int]:
        """Parse GPT-4o-mini classification response.

        Args:
            raw_text: Raw JSON string from GPT-4o-mini
            count: Expected number of results
            confidence_threshold: Minimum confidence to consider a menu

        Returns:
            List of indices classified as menus.
        """
        all_indices = list(range(count))

        cleaned = raw_text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"[OpenAIMenu] Failed to parse filter response: {e}")
            return all_indices

        results = data.get("results", [])
        if not results:
            logger.warning("[OpenAIMenu] Empty results in filter response")
            return all_indices

        menu_indices = []
        for entry in results:
            idx = entry.get("index", -1)
            is_menu = entry.get("is_menu", False)
            confidence = entry.get("confidence", 0.0)

            if 0 <= idx < count and is_menu and confidence >= confidence_threshold:
                menu_indices.append(idx)

        logger.info(
            f"[OpenAIMenu] Filter: {len(menu_indices)}/{count} photos classified as menus "
            f"(threshold={confidence_threshold})"
        )

        return menu_indices
