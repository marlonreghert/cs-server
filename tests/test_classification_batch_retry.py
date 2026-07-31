"""A failed classification batch must be retried, and counted when it isn't.

A real 250-venue run came back only 41% classified. The losses were shaped
exactly like the batch size — whole blocks of 10, 20, 30, 40 photos keeping
their source category — because a batch that raised was written off after one
attempt and nothing above it could tell that from success.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from prometheus_client import REGISTRY

import app.api.openai_photo_classifier_client as mod
from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient


class _Chat:
    def __init__(self, outer):
        self.completions = outer


class _FakeOpenAI:
    """Fails the first `fail_times` calls, then answers normally."""

    def __init__(self, fail_times=0, verdicts_per_call=None):
        self.fail_times = fail_times
        self.calls = 0
        self.verdicts_per_call = verdicts_per_call
        self.chat = _Chat(self)

    async def create(self, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("rate limited")
        n = self.verdicts_per_call if self.verdicts_per_call is not None else 10
        items = [{"index": i, "category": "food_drinks", "category_confidence": 0.9}
                 for i in range(n)]
        payload = json.dumps({"results": items})

        class _Msg:  # minimal shape the client reads
            content = payload

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = None
        return _Resp()


def _client(fake):
    c = OpenAIPhotoClassifierClient(api_key="k", model="m")
    c.client = fake
    return c


def _urls(n):
    return [f"https://x/{i}.jpg" for i in range(n)]


def _metric(reason):
    v = REGISTRY.get_sample_value(
        "photo_classification_fallbacks_total", {"reason": reason}
    )
    return 0.0 if v is None else float(v)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    # The retry backoff is real sleeping; tests assert the ladder, not the wait.
    monkeypatch.setattr(mod, "BATCH_RETRY_BASE_SECONDS", 0.0)


class TestRetry:
    def test_a_transient_failure_is_retried_and_recovers(self):
        fake = _FakeOpenAI(fail_times=1)
        out = asyncio.run(_client(fake).classify_photos(_urls(10), batch_size=10))
        assert fake.calls == 2, "the batch was not retried"
        assert sum(1 for v in out if v) == 10, "recovered batch lost its verdicts"

    def test_it_gives_up_after_the_configured_attempts(self):
        fake = _FakeOpenAI(fail_times=99)
        out = asyncio.run(_client(fake).classify_photos(_urls(10), batch_size=10))
        assert fake.calls == mod.BATCH_MAX_ATTEMPTS
        # Photos are still returned — empty verdicts, never dropped.
        assert len(out) == 10 and all(v == {} for v in out)

    def test_a_successful_batch_is_not_retried(self):
        fake = _FakeOpenAI(fail_times=0)
        asyncio.run(_client(fake).classify_photos(_urls(10), batch_size=10))
        assert fake.calls == 1

    def test_one_dead_batch_does_not_cost_the_other_batches(self):
        # The original property: batch 1 fails permanently, batch 2 must survive.
        class _FirstBatchAlwaysFails(_FakeOpenAI):
            async def create(self, **kw):
                self.calls += 1
                if self.calls <= mod.BATCH_MAX_ATTEMPTS:
                    raise RuntimeError("dead")
                return await _FakeOpenAI.create(self, **kw)

        fake = _FirstBatchAlwaysFails()
        out = asyncio.run(_client(fake).classify_photos(_urls(20), batch_size=10))
        assert len(out) == 20
        assert all(v == {} for v in out[:10]), "first batch should be empty"
        assert any(v for v in out[10:]), "second batch must still classify"


class TestExhaustedBatchIsCounted:
    def test_giving_up_increments_the_fallback_counter_per_photo(self):
        before = _metric("batch_exhausted")
        asyncio.run(_client(_FakeOpenAI(fail_times=99)).classify_photos(
            _urls(10), batch_size=10))
        # Per photo, not per batch: the operator cares how many photos lost a
        # category, which is what the S3 audit measures.
        assert _metric("batch_exhausted") - before == 10

    def test_a_recovered_batch_is_not_counted_as_exhausted(self):
        before = _metric("batch_exhausted")
        asyncio.run(_client(_FakeOpenAI(fail_times=1)).classify_photos(
            _urls(10), batch_size=10))
        assert _metric("batch_exhausted") == before
