"""The anti-drift guard for the `kind` precedence.

plans/260907_events-non-event-and-recurrence-normalisation.md §1 and its
Test Plan. This asserts WIRING, never model behaviour: that the amended
what-is-announced test lives on the ONE shared constant both prompts
interpolate, and that both prompts therefore carry it. CLAUDE.md's own
warning is the reason it exists — "both extraction prompts must change
together; they have drifted before", and extending only
`MULTI_EVENT_EXTRACTION_PROMPT` is the exact half-fix
`plans/260810_post-kind-and-post-extraction-attribution.md` calls out.

The model-behaviour half is a resampled live eval outside this suite
(`@pytest.mark.live_openai`), deselected by default so neither
`make test-unit` nor `make test-bdd` acquires a network dependency.
"""
from __future__ import annotations

import pytest

from app.api.openai_event_extraction_client import (
    EXTRACTION_PROMPT,
    MULTI_EVENT_EXTRACTION_PROMPT,
    _KIND_FIELD_DOC,
    _build_extraction_prompt,
    _build_multi_event_extraction_prompt,
)

# The single sentence that keeps karaoke, quiz / trivia, kids / family and
# EVERY music or genre night. §1a: all four protected shapes are held by
# this one clause, so an edit that trims it must fail a test, not only an
# eval someone might skip.
#
# Matched against a WHITESPACE-FLATTENED prompt (`_flat`): the sentence is
# wrapped across lines in the source, and a test that could be broken by
# rewrapping the paragraph would be a test of the line breaks, not of the
# rule.
_ALONGSIDE_SENTENCE = (
    "Food or a price mentioned ALONGSIDE one of those does not change the "
    "answer"
)


def _flat(text: str) -> str:
    return " ".join(text.split())

# The suppression half — the two bullets that make the `menu` and
# `promotion` branches reachable for the posts they were written for.
_FOOD_BULLET = "If the answer is FOOD"
_PRICE_BULLET = "If the answer is a PRICE"


class TestTheSharedConstantCarriesTheRule:
    def test_it_asks_what_the_post_announces_before_the_precedence(self):
        assert "what is this post" in _KIND_FIELD_DOC
        assert "ANNOUNCING?" in _KIND_FIELD_DOC

    def test_it_carries_the_food_and_price_bullets(self):
        """Delete either and the standing-offer class walks straight back
        into the feed through the `event`-first precedence."""
        assert _FOOD_BULLET in _KIND_FIELD_DOC
        assert _PRICE_BULLET in _KIND_FIELD_DOC
        assert '"menu"' in _KIND_FIELD_DOC
        assert '"promotion"' in _KIND_FIELD_DOC

    def test_it_carries_the_alongside_sentence(self):
        """§1a: ONE clause protects all four shapes §0 refused to concede.
        Trim it and karaoke, quiz / trivia, kids / family and every music
        night fail at once."""
        assert _ALONGSIDE_SENTENCE in _flat(_KIND_FIELD_DOC)

    def test_the_rule_names_the_protected_shapes_explicitly(self):
        """Revision 1 failed because its rescue sentence enumerated only "a
        performance, a competition, a class or a screening", leaving
        karaoke, quiz / trivia and kids / family unrescued. Naming them is
        what stops that recurring."""
        for shape in ("karaoke", "quiz", "kids", "family", "genre night"):
            assert shape in _KIND_FIELD_DOC, shape

    def test_a_schedule_alone_does_not_make_a_standing_offer_an_event(self):
        assert "even when the post states days" in _KIND_FIELD_DOC


class TestBothPromptsCarryItBecauseBothInterpolateTheConstant:
    @pytest.mark.parametrize(
        "prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT]
    )
    def test_the_whole_constant_appears_verbatim(self, prompt):
        """Asserted as a whole-substring containment rather than field by
        field: that is what proves the prompt INTERPOLATES the constant
        rather than having grown its own copy of the rule."""
        assert _KIND_FIELD_DOC in prompt

    @pytest.mark.parametrize(
        "prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT]
    )
    def test_each_prompt_carries_the_alongside_sentence(self, prompt):
        assert _ALONGSIDE_SENTENCE in _flat(prompt)
        assert _FOOD_BULLET in prompt
        assert _PRICE_BULLET in prompt

    def test_neither_prompt_states_the_rule_twice(self):
        """A second copy is how the two would drift apart again."""
        for prompt in (EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT):
            assert _flat(prompt).count(_ALONGSIDE_SENTENCE) == 1

    def test_a_runtime_vocabulary_rebuild_still_carries_the_rule(self):
        """Both prompts are also rebuilt per call with the LIVE admin
        category vocabulary, so the guard must hold on the rebuilt string
        too — not only on the module-level snapshot."""
        vocabulary = ("forró", "sertanejo")
        for builder in (
            _build_extraction_prompt, _build_multi_event_extraction_prompt
        ):
            assert _KIND_FIELD_DOC in builder(vocabulary)
