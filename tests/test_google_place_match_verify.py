"""Google place-id resolution must verify the candidate NAME before accepting it.

Regression guard for the shared-photos bug: Text Search ranks by relevance to
the whole "{name} {address}" query, so a venue near a stronger listing could be
handed that OTHER place's id — and therefore its photos. `search_place_id` now
scans a few candidates and returns a place_id only when its Google displayName
matches the venue's name; otherwise None (a generic image beats a wrong one).
"""

import httpx
import pytest

from app.api.google_places_client import GooglePlacesAPIClient
from app.utils.text_norm import fold_text, names_match


def _client(places: list[dict]) -> GooglePlacesAPIClient:
    """A client whose Text Search always returns `places` (as the API would)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/places:searchText")
        return httpx.Response(200, json={"places": places})

    client = GooglePlacesAPIClient(api_key="unit-key")
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=5.0
    )
    return client


def _place(pid: str, name: str) -> dict:
    return {"id": pid, "displayName": {"text": name, "languageCode": "pt"}}


@pytest.mark.asyncio
async def test_top_result_matching_name_is_accepted():
    client = _client([_place("places/GALETUS", "Galetus | DuGalego Bar")])
    assert (
        await client.search_place_id("Galetus | DuGalego Bar", "R. X, Recife")
        == "places/GALETUS"
    )


@pytest.mark.asyncio
async def test_mismatched_top_result_is_refused_not_borrowed():
    # The Varanda-do-Rock / Galetus scenario: the only candidate is a different
    # venue's strong listing. We must NOT inherit its id (which would inherit its
    # photos) — return None instead.
    client = _client([_place("places/GALETUS", "Galetus | DuGalego Bar")])
    assert (
        await client.search_place_id("Restaurante Varanda do Rock", "R. Y, Recife")
        is None
    )


@pytest.mark.asyncio
async def test_scans_past_a_wrong_top_result_to_a_matching_candidate():
    client = _client(
        [
            _place("places/GALETUS", "Galetus | DuGalego Bar"),
            _place("places/VARANDA", "Restaurante Varanda do Rock"),
        ]
    )
    assert (
        await client.search_place_id("Varanda do Rock", "R. Y, Recife")
        == "places/VARANDA"
    )


@pytest.mark.asyncio
async def test_containment_match_accepts_curated_suffix_name():
    # Our stored name carries a curated suffix; Google's is the base name.
    client = _client([_place("places/ADEGA", "Adega do Futuro")])
    assert (
        await client.search_place_id("Adega do Futuro - Gastronomia", "R. Z, Recife")
        == "places/ADEGA"
    )


@pytest.mark.asyncio
async def test_no_candidates_returns_none():
    assert await _client([]).search_place_id("Whatever", "Nowhere") is None


def test_names_match_semantics():
    assert names_match("Galetus | DuGalego Bar", "galetus dugalego bar")
    assert names_match("Adega do Futuro - Gastronomia", "Adega do Futuro")
    assert not names_match("Restaurante Varanda do Rock", "Galetus | DuGalego Bar")
    # A short generic token must not containment-link to a longer unrelated name.
    assert not names_match("Bar", "Galetus Bar")
    assert not names_match("", "Galetus")
    assert fold_text("LAÇA, Pina") == "laca pina"
