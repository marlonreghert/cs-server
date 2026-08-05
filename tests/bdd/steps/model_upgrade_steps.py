"""Behave steps for tests/bdd/enrichment/model-upgrade-gpt-5-6-luna.feature.

Drives the REAL `OpenAIPhotoClassifierClient` and `OpenAIInstagramJudgeClient`
(plus the real `InstagramJudge` for the schema scenario) over a fake stand-in
for the raw `AsyncOpenAI` SDK object — only the network call is faked, so the
sampling-parameter decision, the batch retry ladder, the token accounting and
the judge's parsing are all real code.

`_FakeOpenAI.simulate_pinned_rejection` reproduces the actual API behavior
measured against gpt-5.6-luna: a 400 `invalid_request_error` whenever
`temperature` is present for a `gpt-5.6*` model. Scenarios that exercise a
pinning model through it are genuinely red before the fix — the call sites
send `temperature` unconditionally today — and genuinely green once the
shared `sampling_kwargs` helper is wired in.
"""
from __future__ import annotations

import asyncio
import json

import httpx
from behave import given, then, when  # type: ignore[import-untyped]
from openai import BadRequestError
from prometheus_client import REGISTRY

from app.api.openai_instagram_judge_client import OpenAIInstagramJudgeClient
from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient
from app.services.instagram_judge import InstagramJudge


# ── the fake OpenAI SDK object ───────────────────────────────────────────────
class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _CompletionTokensDetails:
    def __init__(self, reasoning_tokens=0):
        self.reasoning_tokens = reasoning_tokens


class _Usage:
    def __init__(self, prompt_tokens=0, completion_tokens=0, reasoning_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.completion_tokens_details = _CompletionTokensDetails(reasoning_tokens)


class _Resp:
    def __init__(self, content, usage=None, finish_reason="stop"):
        self.choices = [_Choice(content, finish_reason)]
        self.usage = usage


class _Chat:
    def __init__(self, outer):
        self.completions = outer


def _bad_request_error(param: str) -> BadRequestError:
    """A `BadRequestError` shaped like the real 400 gpt-5.6-luna returns."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return BadRequestError(
        message=(
            f"Unsupported value: '{param}' does not support this value with "
            "this model. Only the default value is supported."
        ),
        response=response,
        body={
            "message": f"Unsupported value: '{param}'.",
            "type": "invalid_request_error",
            "param": param,
            "code": None,
        },
    )


class _FakeOpenAI:
    """Stand-in for `AsyncOpenAI`. Records every call's kwargs."""

    def __init__(self) -> None:
        self.chat = _Chat(self)
        self.calls: list[dict] = []
        self.next_response = None
        self.raise_error: Exception | None = None
        # Reproduces the measured live behavior: gpt-5.6* 400s the instant a
        # `temperature` kwarg is present, regardless of its value.
        self.simulate_pinned_rejection = False

    async def create(self, **kw):
        self.calls.append(kw)
        if (
            self.simulate_pinned_rejection
            and "temperature" in kw
            and str(kw.get("model", "")).startswith("gpt-5.6")
        ):
            raise _bad_request_error("temperature")
        if self.raise_error is not None:
            raise self.raise_error
        return self.next_response


def _ok_classification_response(n: int = 1, usage=None) -> _Resp:
    payload = json.dumps({"results": [
        {"index": i, "category": "interior", "confidence": 0.9} for i in range(n)
    ]})
    return _Resp(payload, usage=usage)


def _ok_judge_response(is_match=True, confidence=0.87, reason="Names match closely") -> _Resp:
    payload = json.dumps({"is_match": is_match, "confidence": confidence, "reason": reason})
    return _Resp(payload)


def _reasoning_metric() -> float:
    v = REGISTRY.get_sample_value(
        "openai_tokens_total", {"endpoint": "photo_classify", "direction": "reasoning"}
    )
    return 0.0 if v is None else float(v)


# ── Given: the model ─────────────────────────────────────────────────────────
@given('the target model is "{model}"')
def step_target_model(context, model):
    context.model = model


@given("the target model is an unrecognised model name")
def step_target_model_unrecognised(context):
    context.model = "totally-unrecognised-future-model"


# ── When/Then: building a request (the shared sampling decision) ───────────
@when("a classification request is built with temperature {temp}")
def step_build_classification_request(context, temp):
    fake = _FakeOpenAI()
    fake.next_response = _ok_classification_response(1)
    client = OpenAIPhotoClassifierClient(api_key="k", model=context.model)
    client.client = fake
    asyncio.run(client.classify_photos(["https://x/0.jpg"], batch_size=1))
    context.sent_kwargs = fake.calls[-1]


@when("a judge request is built with temperature {temp}")
def step_build_judge_request(context, temp):
    fake = _FakeOpenAI()
    fake.next_response = _ok_judge_response()
    client = OpenAIInstagramJudgeClient(api_key="k")
    client.client = fake
    asyncio.run(client.judge_instagram_match(prompt="p", model=context.model))
    context.sent_kwargs = fake.calls[-1]


@then("the request carries no temperature")
def step_no_temperature(context):
    assert "temperature" not in context.sent_kwargs, context.sent_kwargs


@then("the request carries temperature {temp}")
def step_carries_temperature(context, temp):
    assert context.sent_kwargs.get("temperature") == float(temp), context.sent_kwargs


# ── Given/Then: a full batch of ten ─────────────────────────────────────────
@given("a batch of 10 photos is classified")
def step_batch_of_ten(context):
    fake = _FakeOpenAI()
    fake.simulate_pinned_rejection = True
    fake.next_response = _ok_classification_response(
        10, usage=_Usage(prompt_tokens=9000, completion_tokens=791, reasoning_tokens=184)
    )
    client = OpenAIPhotoClassifierClient(api_key="k", model=context.model)
    client.client = fake
    context.fake_openai = fake
    context.classify_result = asyncio.run(
        client.classify_photos([f"https://x/{i}.jpg" for i in range(10)], batch_size=10)
    )


@then("10 verdicts are returned")
def step_ten_verdicts(context):
    assert len(context.classify_result) == 10, context.classify_result


@then("no verdict falls back for a missing response")
def step_no_fallback(context):
    assert all(v for v in context.classify_result), context.classify_result


# ── Given/When/Then: a rejected parameter ───────────────────────────────────
@given("the model rejects a request parameter")
def step_model_rejects_param(context):
    fake = _FakeOpenAI()
    fake.raise_error = _bad_request_error("temperature")
    context.model = "gpt-5.6-luna"
    client = OpenAIPhotoClassifierClient(api_key="k", model=context.model)
    client.client = fake
    context.classifier_client = client
    context.fake_openai = fake


@when("a classification request is made")
def step_classification_request_made(context):
    context.classification_error = None
    try:
        context.classify_result = asyncio.run(
            context.classifier_client.classify_photos(["https://x/0.jpg"], batch_size=1)
        )
    except Exception as e:  # noqa: BLE001 — captured for the assertion below
        context.classification_error = e


@then("the failure is reported with the rejected parameter")
def step_failure_reports_param(context):
    assert context.classification_error is not None, "expected the request to raise"
    assert getattr(context.classification_error, "param", None) == "temperature", (
        context.classification_error
    )


@then("the request is not silently retried")
def step_not_retried(context):
    assert len(context.fake_openai.calls) == 1, (
        f"expected exactly one attempt, got {len(context.fake_openai.calls)}"
    )


# ── Given/When/Then: reasoning tokens ────────────────────────────────────────
@given("the target model reports reasoning tokens in its usage")
def step_model_reports_reasoning_tokens(context):
    fake = _FakeOpenAI()
    # A non-pinning model in play here — this scenario isolates token
    # accounting from the sampling-parameter behavior covered elsewhere.
    context.model = "gpt-5.4-nano"
    fake.next_response = _ok_classification_response(
        1, usage=_Usage(prompt_tokens=1500, completion_tokens=300, reasoning_tokens=120)
    )
    client = OpenAIPhotoClassifierClient(api_key="k", model=context.model)
    client.client = fake
    context.classifier_client = client
    context.reasoning_before = _reasoning_metric()


@when("a classification request completes")
def step_classification_completes(context):
    context.classify_result = asyncio.run(
        context.classifier_client.classify_photos(["https://x/0.jpg"], batch_size=1)
    )


@then("the reasoning tokens are counted in the token metric")
def step_reasoning_tokens_counted(context):
    after = _reasoning_metric()
    assert after - context.reasoning_before == 120, (context.reasoning_before, after)


# ── When/Then: the judge's schema ────────────────────────────────────────────
class _Venue:
    venue_name = "Bar Test"
    venue_address = "Recife, PE"


@when("the Instagram judge adjudicates a candidate")
def step_judge_adjudicates(context):
    fake = _FakeOpenAI()
    fake.simulate_pinned_rejection = True
    fake.next_response = _ok_judge_response()
    client = OpenAIInstagramJudgeClient(api_key="k")
    client.client = fake
    context.fake_openai = fake
    judge = InstagramJudge(client, model=context.model)
    context.judge_verdict = asyncio.run(judge.judge(
        venue=_Venue(), candidate="bartest", profile={}, venue_photos=[],
    ))


@then("the verdict carries the same fields as before the upgrade")
def step_verdict_schema_unchanged(context):
    v = context.judge_verdict
    assert v is not None, "the judge returned no verdict (the request was likely rejected)"
    assert v.is_match is True, v
    # No images were supplied, so this is a text-only verdict and the model's
    # raw 0.87 is capped at TEXT_ONLY_CONFIDENCE_CEILING (0.80) — unrelated to
    # this migration. What this scenario protects is that the same four
    # fields (mode, is_match, confidence, reason) still come back at all.
    assert v.confidence == 0.80, v
    assert v.reason == "Names match closely", v
    assert v.mode == "text_only", v
