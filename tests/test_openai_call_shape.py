"""No OpenAI call site may use `max_tokens`.

Measured against the live API on 2026-07-31:

    model=gpt-5.4-mini
    FAIL  max_tokens=200:            400 Unsupported parameter: 'max_tokens' is
                                     not supported with this model. Use
                                     'max_completion_tokens' instead.
    OK    max_completion_tokens=200: '{"is_match":true,...}'
    OK    temperature=0

The migration to GPT-5.4 changed the model ids in six clients and left every
`max_tokens=` in place, which would have turned menu extraction, photo
classification, the vibe classifier and the Instagram judge into 400-error
generators the moment it deployed. A grep is the cheapest possible guard against
that returning, so it lives here rather than in anyone's memory.
"""
import glob
import os
import re

import pytest

CLIENTS = sorted(glob.glob(os.path.join("app", "api", "openai_*.py")))


def test_there_are_openai_clients_to_check():
    """If the glob ever stops matching, the rest of this file passes vacuously."""
    assert CLIENTS, "no OpenAI clients found — this guard would be silently inert"


@pytest.mark.parametrize("path", CLIENTS)
def test_no_client_passes_max_tokens(path):
    source = open(path).read()
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"\bmax_tokens\s*=", line)
    ]
    assert not offenders, (
        f"{path} passes max_tokens, which GPT-5.4 rejects with a 400. "
        f"Use max_completion_tokens: {offenders}"
    )


@pytest.mark.parametrize("path", CLIENTS)
def test_every_client_bounds_its_output(path):
    """The cap must not simply be deleted to satisfy the test above — an
    unbounded reply is how a batch silently truncates or a bill runs away."""
    source = open(path).read()
    if "chat.completions.create" not in source:
        pytest.skip("not a chat-completions client")
    assert "max_completion_tokens" in source, f"{path} sets no output bound"


class TestModelsAreMigrated:
    """Nothing should still point at the retired GPT-4 family."""

    def test_no_client_defaults_to_a_gpt4_model(self):
        stale = {}
        for path in CLIENTS:
            for match in re.finditer(r'"(gpt-4[^"]*)"', open(path).read()):
                stale.setdefault(path, []).append(match.group(1))
        assert not stale, f"still on GPT-4: {stale}"

    def test_settings_carry_no_gpt4_defaults(self):
        from app.config import Settings

        settings = Settings()
        stale = {
            name: value
            for name, value in settings.model_dump().items()
            if isinstance(value, str) and value.startswith("gpt-4")
        }
        assert not stale, f"settings still default to GPT-4: {stale}"
