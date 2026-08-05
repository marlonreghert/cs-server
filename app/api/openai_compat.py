"""Shared sampling-parameter compatibility across OpenAI chat-completions clients.

Reasoning models (the gpt-5.6 family: luna, sol, terra) reject a custom
`temperature` outright:

    400 invalid_request_error
    Unsupported value: 'temperature' does not support 0.1 with this model.
    Only the default (1) value is supported.

5.4-family models (nano, mini) accept and, in one case, rely on it: the
Instagram judge runs at `temperature=0` deliberately, because an adjudicator
that answers differently on a re-run is not an adjudicator.

Detection is an explicit prefix list of models that PIN sampling, not an
allow-list of models that accept it. An unrecognised model — one that hasn't
been measured against the live API — passes `temperature` through unchanged,
which is today's behavior. Only the exact class that has actually been
measured (gpt-5.6) is treated as pinned; an allow-list would silently drop
temperature for every future model instead.

See `plans/260805_model-upgrade-gpt-5-6-luna.md` for how this was measured.
"""
from __future__ import annotations

# Prefixes of models known to reject a custom `temperature`. Extend only after
# measuring a 400 against the live API for the new prefix.
PINNED_SAMPLING_PREFIXES: tuple[str, ...] = ("gpt-5.6",)


def pins_sampling(model: str) -> bool:
    """True if `model` rejects a non-default `temperature`."""
    return bool(model) and model.startswith(PINNED_SAMPLING_PREFIXES)


def sampling_kwargs(model: str, temperature: float) -> dict:
    """kwargs to spread into a chat-completions `create(...)` call.

    `{}` for a model that pins sampling, `{"temperature": temperature}`
    otherwise. `temperature=0` is a real, deliberate value (the Instagram
    judge's determinism) — it is never dropped by a truthiness check, only
    omitted when the model itself refuses a custom value.
    """
    if pins_sampling(model):
        return {}
    return {"temperature": temperature}


__all__ = ["pins_sampling", "sampling_kwargs", "PINNED_SAMPLING_PREFIXES"]
