"""The data lake must never persist credentials.

BestTime authenticates with `api_key_private` / `api_key_public` as QUERY
PARAMS, and Google Places with `key=AIza...`, so the request block of every
archived record is one careless serialization away from leaking a live key into
permanent storage. These tests are the enforcement.
"""
import gzip
import json
from datetime import datetime, timezone

from app.dao.datalake_writer import DatalakeWriter, build_envelope, redact_params

PRIVATE_KEY = "pri_" + "a" * 24
PUBLIC_KEY = "pub_" + "b" * 24
GOOGLE_KEY = "AIza" + "c" * 30
NOW = datetime(2026, 7, 26, 0, 4, 3, tzinfo=timezone.utc)


class _FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {}


class TestRedactParams:
    def test_drops_secret_named_params_entirely(self):
        clean = redact_params(
            {
                "api_key_private": PRIVATE_KEY,
                "api_key_public": PUBLIC_KEY,
                "api_key": "whatever",
                "token": "t",
                "password": "p",
                "venue_id": "ven_1",
            }
        )
        assert clean == {"venue_id": "ven_1"}

    def test_drops_the_google_key_param(self):
        clean = redact_params({"key": GOOGLE_KEY, "place_id": "abc"})
        assert clean == {"place_id": "abc"}

    def test_masks_a_key_shaped_value_under_an_innocent_name(self):
        clean = redact_params({"note": f"debug {PRIVATE_KEY}", "venue_id": "ven_1"})
        assert clean["note"] == "***REDACTED***"
        assert clean["venue_id"] == "ven_1"

    def test_leaves_no_partial_key_material(self):
        """Masking to `pri_***REDACTED***` would still archive the prefix — the
        whole value has to go."""
        blob = json.dumps(redact_params({"note": PRIVATE_KEY, "g": GOOGLE_KEY}))
        assert "pri_" not in blob
        assert "AIza" not in blob

    def test_keeps_the_analytically_useful_params(self):
        clean = redact_params(
            {"api_key_private": PRIVATE_KEY, "lat": -8.05, "lng": -34.88, "radius": 1000}
        )
        assert clean == {"lat": -8.05, "lng": -34.88, "radius": 1000}

    def test_handles_missing_and_empty_params(self):
        assert redact_params(None) == {}
        assert redact_params({}) == {}


class TestEnvelopeRedaction:
    def test_the_request_block_is_redacted_before_serialization(self):
        envelope = build_envelope(
            dataset="live_forecast",
            ingested_at=NOW,
            request_params={"api_key_private": PRIVATE_KEY, "venue_id": "ven_1"},
        )
        assert envelope["request"] == {"venue_id": "ven_1"}


class TestArchivedBytes:
    async def test_no_key_material_reaches_the_uploaded_object(self):
        """The end-to-end guarantee: whatever the request carried, the bytes
        that land in S3 hold no credential."""
        fake_s3 = _FakeS3()
        writer = DatalakeWriter(
            bucket="test-lake",
            region="us-east-1",
            s3_client=fake_s3,
            now=lambda: NOW,
        )
        writer.record(
            dataset="live_forecast",
            endpoint="/forecasts/live",
            payload={"status": "OK"},
            request_params={
                "api_key_private": PRIVATE_KEY,
                "api_key_public": PUBLIC_KEY,
                "venue_id": "ven_1",
            },
        )
        await writer.flush()

        body = gzip.decompress(fake_s3.puts[0]["Body"]).decode("utf-8")
        assert PRIVATE_KEY not in body
        assert PUBLIC_KEY not in body
        assert "pri_" not in body
        assert "ven_1" in body, "non-secret context must survive"
