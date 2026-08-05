"""Unit coverage for the shared sampling-parameter compatibility helper.

`sampling_kwargs` is the one place that decides whether `temperature` is safe
to send a model. Two properties matter most, because getting either wrong
means every call site inherits the mistake:

  * `temperature=0` must survive — the Instagram judge's determinism depends
    on it, and a truthiness check (`if temperature:`) would silently discard
    exactly that value.
  * An unmeasured model must default to "pass temperature through" — an
    allow-list would silently drop it for every future model instead.

The second half of this file drives each of the four clients' REAL request
construction over a capturing fake, so a client that forgets to spread the
helper into its `create(...)` call fails here rather than in production.
"""
from __future__ import annotations

import asyncio

from prometheus_client import REGISTRY

from app.api.openai_compat import PINNED_SAMPLING_PREFIXES, pins_sampling, sampling_kwargs

LUNA = "gpt-5.6-luna"
NANO = "gpt-5.4-nano"
MINI = "gpt-5.4-mini"
UNKNOWN = "some-future-model-nobody-has-measured"


class TestSamplingKwargs:
    def test_pinning_model_omits_temperature(self):
        assert sampling_kwargs(LUNA, 0.1) == {}

    def test_54_family_model_passes_temperature_through(self):
        assert sampling_kwargs(NANO, 0.1) == {"temperature": 0.1}
        assert sampling_kwargs(MINI, 0.2) == {"temperature": 0.2}

    def test_unknown_model_passes_temperature_through(self):
        # Default-permissive: an unmeasured model behaves like today, not like
        # the one class that has actually been shown to reject the parameter.
        assert sampling_kwargs(UNKNOWN, 0.1) == {"temperature": 0.1}

    def test_zero_temperature_is_not_dropped_as_falsy(self):
        kwargs = sampling_kwargs(NANO, 0)
        assert "temperature" in kwargs, "temperature=0 was silently discarded"
        assert kwargs["temperature"] == 0

    def test_pins_sampling_matches_the_measured_family(self):
        assert pins_sampling(LUNA) is True
        assert pins_sampling(NANO) is False
        assert pins_sampling("") is False

    def test_pinned_prefixes_cover_the_whole_56_family(self):
        for suffix in ("luna", "sol", "terra"):
            assert pins_sampling(f"gpt-5.6-{suffix}"), suffix

    def test_prefix_list_is_not_accidentally_broad(self):
        # A prefix that swallows every model would make "unknown passes
        # through" meaningless.
        assert PINNED_SAMPLING_PREFIXES == ("gpt-5.6",)


# ── a capturing fake for the raw AsyncOpenAI surface ─────────────────────────
class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _Chat:
    def __init__(self, outer):
        self.completions = outer


class _CapturingClient:
    """Records every call's kwargs and answers with a fixed, valid payload."""

    def __init__(self, content: str = '{"results": []}'):
        self.chat = _Chat(self)
        self.calls: list[dict] = []
        self.content = content

    async def create(self, **kw):
        self.calls.append(kw)
        return _Resp(self.content)


class TestClientsApplyTheHelper:
    """Each client must spread `sampling_kwargs` into its own request."""

    def test_menu_extraction_omits_temperature_for_a_pinning_model(self):
        from app.api.openai_menu_client import OpenAIMenuClient

        client = OpenAIMenuClient(api_key="k", model=LUNA)
        fake = _CapturingClient('{"menu_sections": []}')
        client.client = fake
        asyncio.run(client.extract_menu_from_photos(["https://x/0.jpg"]))
        assert "temperature" not in fake.calls[-1], fake.calls[-1]

    def test_menu_extraction_keeps_temperature_for_a_54_model(self):
        from app.api.openai_menu_client import OpenAIMenuClient

        client = OpenAIMenuClient(api_key="k", model=NANO)
        fake = _CapturingClient('{"menu_sections": []}')
        client.client = fake
        asyncio.run(client.extract_menu_from_photos(["https://x/0.jpg"]))
        assert fake.calls[-1]["temperature"] == 0.1

    def test_menu_photo_filter_omits_temperature_for_a_pinning_model(self):
        from app.api.openai_menu_client import OpenAIMenuClient

        client = OpenAIMenuClient(api_key="k")
        fake = _CapturingClient('{"results": []}')
        client.client = fake
        asyncio.run(client.classify_menu_photos(["https://x/0.jpg"], model=LUNA))
        assert "temperature" not in fake.calls[-1], fake.calls[-1]

    def test_vibe_stage_a_omits_temperature_for_a_pinning_model(self):
        from app.api.openai_vibe_client import OpenAIVibeClient

        client = OpenAIVibeClient(api_key="k")
        fake = _CapturingClient("{}")
        client.client = fake
        asyncio.run(client.classify_venue_vibes_stage_a(["https://x/0.jpg"], model=LUNA))
        assert "temperature" not in fake.calls[-1], fake.calls[-1]

    def test_vibe_stage_b_omits_temperature_for_a_pinning_model(self):
        from app.api.openai_vibe_client import OpenAIVibeClient

        client = OpenAIVibeClient(api_key="k")
        fake = _CapturingClient("{}")
        client.client = fake
        asyncio.run(client.classify_venue_vibes_stage_b(
            ["https://x/0.jpg"], stage_a_result={}, uncertain_facets=["musica"], model=LUNA,
        ))
        assert "temperature" not in fake.calls[-1], fake.calls[-1]

    def test_judge_omits_temperature_for_a_pinning_model(self):
        from app.api.openai_instagram_judge_client import OpenAIInstagramJudgeClient

        client = OpenAIInstagramJudgeClient(api_key="k")
        fake = _CapturingClient('{"is_match": true, "confidence": 0.9, "reason": "ok"}')
        client.client = fake
        asyncio.run(client.judge_instagram_match(prompt="p", model=LUNA))
        assert "temperature" not in fake.calls[-1], fake.calls[-1]

    def test_judge_keeps_zero_temperature_for_a_54_model(self):
        from app.api.openai_instagram_judge_client import OpenAIInstagramJudgeClient

        client = OpenAIInstagramJudgeClient(api_key="k")
        fake = _CapturingClient('{"is_match": true, "confidence": 0.9, "reason": "ok"}')
        client.client = fake
        asyncio.run(client.judge_instagram_match(prompt="p", model=MINI))
        assert fake.calls[-1]["temperature"] == 0

    def test_photo_classifier_omits_temperature_for_a_pinning_model(self):
        from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient

        client = OpenAIPhotoClassifierClient(api_key="k", model=LUNA)
        fake = _CapturingClient(
            '{"results": [{"index": 0, "category": "interior", "confidence": 0.9}]}'
        )
        client.client = fake
        asyncio.run(client.classify_photos(["https://x/0.jpg"], batch_size=1))
        assert "temperature" not in fake.calls[-1], fake.calls[-1]

    def test_photo_classifier_keeps_temperature_for_a_54_model(self):
        from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient

        client = OpenAIPhotoClassifierClient(api_key="k", model=NANO)
        fake = _CapturingClient(
            '{"results": [{"index": 0, "category": "interior", "confidence": 0.9}]}'
        )
        client.client = fake
        asyncio.run(client.classify_photos(["https://x/0.jpg"], batch_size=1))
        assert fake.calls[-1]["temperature"] == 0.1


class TestReasoningTokenAccounting:
    """Lower-level edge cases the batch-of-ten BDD scenario doesn't reach."""

    def test_reasoning_tokens_are_added_to_the_metric(self):
        from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient

        client = OpenAIPhotoClassifierClient(api_key="k", model=NANO)

        class _Details:
            reasoning_tokens = 42

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            completion_tokens_details = _Details()

        class _FakeResp:
            usage = _Usage()

        endpoint = "reasoning_accounting_probe"
        before = REGISTRY.get_sample_value(
            "openai_tokens_total", {"endpoint": endpoint, "direction": "reasoning"}
        ) or 0.0
        client._record_usage(_FakeResp(), endpoint)
        after = REGISTRY.get_sample_value(
            "openai_tokens_total", {"endpoint": endpoint, "direction": "reasoning"}
        )
        assert after - before == 42

    def test_missing_reasoning_details_does_not_raise(self):
        from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient

        client = OpenAIPhotoClassifierClient(api_key="k", model=NANO)

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5
            # No completion_tokens_details at all — an older API response shape.

        class _FakeResp:
            usage = _Usage()

        counts = client._record_usage(_FakeResp(), "no_reasoning_details_probe")
        assert counts == {"input": 10, "output": 5}

    def test_output_total_is_not_double_counted_with_reasoning(self):
        # completion_tokens already INCLUDES reasoning tokens as a subset, per
        # the API's own accounting — the reasoning series is a breakout for
        # visibility, not an addition to the billed output total.
        from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient

        client = OpenAIPhotoClassifierClient(api_key="k", model=NANO)

        class _Details:
            reasoning_tokens = 184

        class _Usage:
            prompt_tokens = 9000
            completion_tokens = 791
            completion_tokens_details = _Details()

        class _FakeResp:
            usage = _Usage()

        counts = client._record_usage(_FakeResp(), "double_count_probe")
        assert counts == {"input": 9000, "output": 791}
        assert client.tokens == {"input": 9000, "output": 791}
