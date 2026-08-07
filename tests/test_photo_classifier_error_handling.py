"""A BadRequestError from OpenAI must be diagnosed by its actual cause, not
reported as a parameter rejection unconditionally.

Production reproduction (2026-08-07 RCA): classifying an Instagram CDN url
against gpt-5.6-luna returns `400 invalid_image_url` with `param: None`. The
handler logged `request rejected parameter '?'` for every 400 regardless of
`code`, sending an operator after a model-parameter bug when the real cause
was an image the model could not download. This file locks the corrected
branching: `invalid_image_url` is reported as an image-fetch failure naming
the HOST ONLY (never the full signed url — it is a bearer credential for that
object), a genuine parameter rejection keeps today's message, and anything
else keeps a generic branch.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from openai import BadRequestError
from prometheus_client import REGISTRY

from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient

# A realistic signed Instagram CDN url: a query-string token that must never
# reach a log line, only the host in front of it.
SIGNED_INSTAGRAM_URL = (
    "https://instagram.fper12-1.fna.fbcdn.net/v/t51.82787-15/photo.jpg"
    "?_nc_ht=instagram.fper12-1.fna.fbcdn.net&oe=66B0A1C2&SECRETTOKEN=abc123"
)


def _bad_request_error(*, code, param, message) -> BadRequestError:
    """A REAL openai.BadRequestError, built the way the SDK builds one from a
    JSON error body — `.code`/`.param` are parsed from `body` in APIError.__init__,
    so a hand-built exception here behaves identically to what a live 400
    response would raise."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(400, request=request)
    body = {"message": message, "type": "invalid_request_error", "param": param, "code": code}
    return BadRequestError(message, response=response, body=body)


class _Chat:
    def __init__(self, outer):
        self.completions = outer


class _FakeOpenAIRaising:
    """Always raises the given exception from chat.completions.create."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0
        self.chat = _Chat(self)

    async def create(self, **kw):
        self.calls += 1
        raise self.exc


def _client(fake) -> OpenAIPhotoClassifierClient:
    c = OpenAIPhotoClassifierClient(api_key="k", model="gpt-5.6-luna")
    c.client = fake
    return c


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _metric(reason: str) -> float:
    v = REGISTRY.get_sample_value("photo_classification_fallbacks_total", {"reason": reason})
    return 0.0 if v is None else float(v)


class TestInvalidImageUrl:
    def test_reported_as_an_image_failure_naming_the_host_only(self, caplog):
        exc = _bad_request_error(
            code="invalid_image_url", param=None,
            message=f"Error while downloading {SIGNED_INSTAGRAM_URL}.",
        )
        fake = _FakeOpenAIRaising(exc)
        with caplog.at_level(logging.ERROR):
            out = _run(_client(fake).classify_photos([SIGNED_INSTAGRAM_URL], batch_size=10))

        # The photo keeps its source category rather than the whole call
        # blowing up — a per-image download problem must not poison an
        # unrelated batch's classification.
        assert out == [{}]

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "image" in messages.lower()
        assert "instagram.fper12-1.fna.fbcdn.net" in messages
        # The signed query string (the actual bearer credential) must never
        # appear in a log line — only the host.
        assert "SECRETTOKEN" not in messages
        assert SIGNED_INSTAGRAM_URL not in messages

    def test_not_reported_as_a_rejected_parameter(self, caplog):
        exc = _bad_request_error(
            code="invalid_image_url", param=None,
            message=f"Error while downloading {SIGNED_INSTAGRAM_URL}.",
        )
        fake = _FakeOpenAIRaising(exc)
        with caplog.at_level(logging.ERROR):
            _run(_client(fake).classify_photos([SIGNED_INSTAGRAM_URL], batch_size=10))

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "rejected parameter" not in messages

    def test_counted_under_its_own_fallback_reason(self):
        before = _metric("image_fetch_failed")
        exc = _bad_request_error(code="invalid_image_url", param=None, message="Error while downloading https://x/y.jpg")
        _run(_client(_FakeOpenAIRaising(exc)).classify_photos(["https://x/y.jpg"], batch_size=10))
        assert _metric("image_fetch_failed") - before == 1

    def test_other_batches_still_get_a_chance(self):
        """A per-image download failure must not poison a venue's OTHER
        batches — only genuine call-site bugs (a real parameter rejection)
        are worth aborting the whole run for."""
        class _FailsFirstBatchOnly:
            def __init__(self):
                self.calls = 0
                self.chat = _Chat(self)

            async def create(self, **kw):
                self.calls += 1
                if self.calls == 1:
                    raise _bad_request_error(
                        code="invalid_image_url", param=None,
                        message="Error while downloading https://x/bad.jpg",
                    )
                import json

                payload = json.dumps({"results": [
                    {"index": i, "category": "food_drinks", "confidence": 0.9}
                    for i in range(10)
                ]})

                class _Msg:
                    content = payload

                class _Choice:
                    message = _Msg()

                class _Resp:
                    choices = [_Choice()]
                    usage = None

                return _Resp()

        fake = _FailsFirstBatchOnly()
        urls = [f"https://x/{i}.jpg" for i in range(20)]
        out = _run(_client(fake).classify_photos(urls, batch_size=10))
        assert len(out) == 20
        assert all(v == {} for v in out[:10]), "first (failing) batch must keep no verdict"
        assert all(v for v in out[10:]), "second batch must still be classified"


class TestGenuineParameterRejection:
    def test_keeps_todays_message_and_raises(self, caplog):
        exc = _bad_request_error(
            code="unknown_parameter", param="response_format",
            message="Unknown parameter: 'response_format'.",
        )
        fake = _FakeOpenAIRaising(exc)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(BadRequestError):
                _run(_client(fake).classify_photos(["https://x/y.jpg"], batch_size=10))

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "rejected parameter" in messages
        assert "response_format" in messages

    def test_not_counted_as_an_image_fetch_failure(self):
        before = _metric("image_fetch_failed")
        exc = _bad_request_error(code="unknown_parameter", param="response_format", message="bad param")
        with pytest.raises(BadRequestError):
            _run(_client(_FakeOpenAIRaising(exc)).classify_photos(["https://x/y.jpg"], batch_size=10))
        assert _metric("image_fetch_failed") - before == 0


class TestUnknown400:
    def test_keeps_a_generic_branch_and_raises(self, caplog):
        exc = _bad_request_error(code=None, param=None, message="Something else went wrong.")
        fake = _FakeOpenAIRaising(exc)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(BadRequestError):
                _run(_client(fake).classify_photos(["https://x/y.jpg"], batch_size=10))

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "rejected parameter" not in messages
        assert "image" not in messages.lower() or "invalid_image_url" not in messages

    def test_not_counted_as_an_image_fetch_failure(self):
        before = _metric("image_fetch_failed")
        exc = _bad_request_error(code=None, param=None, message="Something else went wrong.")
        with pytest.raises(BadRequestError):
            _run(_client(_FakeOpenAIRaising(exc)).classify_photos(["https://x/y.jpg"], batch_size=10))
        assert _metric("image_fetch_failed") - before == 0
