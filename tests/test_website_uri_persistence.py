"""`website_uri` must survive into the PERSISTED attributes, not just the API response.

This is the regression that broke the Instagram cascade's free tier. The field
was declared on GooglePlacesDetailsResponse — the transient API response —
fetched, used inline to extract a handle, and then dropped. VibeAttributes, the
model that is actually stored, never carried it. Anything reading it back later
therefore got None for every venue in the catalog, and a whole cascade tier was
structurally dead while looking perfectly correct in review.

Declaring a field is not the same as persisting it. These tests assert the
round-trip.
"""
from app.api.google_places_client import GooglePlacesAPIClient
from app.models.vibe_attributes import GooglePlacesDetailsResponse, VibeAttributes


def _client():
    return GooglePlacesAPIClient(api_key="test")


class TestPersistedShape:
    def test_vibe_attributes_declares_website_uri(self):
        """The PERSISTED model must have the field — not only the response model."""
        assert "website_uri" in VibeAttributes.model_fields

    def test_it_survives_serialization(self):
        v = VibeAttributes(venue_id="v", website_uri="https://instagram.com/x")
        assert v.model_dump()["website_uri"] == "https://instagram.com/x"

    def test_it_round_trips_through_a_dump_and_reload(self):
        """What the DAO actually does: dump to JSON, store, reload."""
        original = VibeAttributes(venue_id="v", website_uri="https://barvibes.com")
        reloaded = VibeAttributes(**original.model_dump())
        assert reloaded.website_uri == "https://barvibes.com"


class TestMapper:
    def test_carries_the_website_from_the_api_response(self):
        details = GooglePlacesDetailsResponse(
            place_id="p1", website_uri="https://instagram.com/barvibes"
        )
        attrs = _client().details_to_vibe_attributes("ven_1", details)
        assert attrs.website_uri == "https://instagram.com/barvibes"

    def test_a_venue_without_a_website_stays_none(self):
        details = GooglePlacesDetailsResponse(place_id="p1")
        assert _client().details_to_vibe_attributes("ven_1", details).website_uri is None

    def test_the_mapped_value_is_what_gets_stored(self):
        """End to end: response -> mapper -> the dict the DAO persists."""
        details = GooglePlacesDetailsResponse(
            place_id="p1", website_uri="https://instagram.com/tasquinhadotio"
        )
        stored = _client().details_to_vibe_attributes("ven_1", details).model_dump()
        assert stored["website_uri"] == "https://instagram.com/tasquinhadotio"


class TestCascadeCanReadItBack:
    def test_the_free_tier_reads_what_the_mapper_wrote(self):
        """The exact path that was silently returning None for every venue."""
        import asyncio

        from app.services.instagram_cascade_adapters import GoogleListingWebsiteSource

        details = GooglePlacesDetailsResponse(
            place_id="p1", website_uri="https://instagram.com/barvibes"
        )
        attrs = _client().details_to_vibe_attributes("ven_1", details)

        class _Dao:
            def get_vibe_attributes(self, venue_id):
                return VibeAttributes(**attrs.model_dump())  # as reloaded from storage

        got = asyncio.run(GoogleListingWebsiteSource(_Dao()).website_for("ven_1"))
        assert got == "https://instagram.com/barvibes"
