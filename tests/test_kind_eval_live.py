"""The resampled live eval — the PRE-MERGE GATE for an extraction-prompt
`kind` change.

plans/260907_events-non-event-and-recurrence-normalisation.md §5 and its
Test Plan. Every test here calls the REAL model and spends money, so the
module is marked `live_openai`, `pytest.ini` deselects that marker by
default, and neither Make target enumerates this file. Run it deliberately:

    .venv/bin/python -m pytest tests/test_kind_eval_live.py -m live_openai

The fixture's SHAPE is guarded offline and in CI by
`tests/test_kind_eval_fixture.py` — that guard used to live in this module,
where it never ran anywhere (F1).

## What it gates, and what it deliberately does not

- A row with `blocking: true` GATES on its `accepted_kinds` (or its single
  `expected_kind`): one disagreement in any resample fails the merge. A rule
  that removes a real music, karaoke, quiz or family night is worse than the
  defect it fixes, and the price-headline controls (G4-G8) are where
  over-suppression would show up first.
- Some rows accept MORE THAN ONE answer, on purpose. "caldinho grátis na
  segunda" is a free dish on a standing weekday: `menu` and `promotion` are
  both correct suppressions and pinning one would be gating a coin flip.
  What is asserted there is "not `event`".
- A row with `blocking: false` is SCORED AND REPORTED, never gated. That
  covers the live captions (whose labels no operator has confirmed yet) and
  `Oktoberfest BeerDock`, §1b's one named live casualty of the §0
  concession, which gating would re-erect the boundary the operator removed.

**Resample 3×.** A single sample cannot tell a stable rule from a lucky one.

Residual limitation, recorded so nobody over-reads a green run: the eval
sends the caption but not the flyer IMAGE, so a post whose only event
evidence is on the flyer is judged more harshly here than in production.
That biases the eval toward FINDING false positives, which is the safe
direction.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from app.api.openai_event_extraction_client import (
    OpenAIEventExtractionClient,
    parse_extraction_response,
)
from app.config import settings
from app.models.event_kind import normalize_kind

FIXTURE = Path(__file__).parent / "fixtures" / "kind_eval_captions.json"
RESAMPLES = int(os.environ.get("KIND_EVAL_RESAMPLES", "3"))

pytestmark = pytest.mark.live_openai


def _rows() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _accepted(row) -> list[str]:
    return row.get("accepted_kinds") or (
        [row["expected_kind"]] if row["expected_kind"] else []
    )


async def _answers(rows: list[dict]) -> list[list[str]]:
    if not settings.openai_api_key:
        pytest.skip("openai_api_key is not configured")
    client = OpenAIEventExtractionClient(api_key=settings.openai_api_key)
    try:
        out = []
        for row in rows:
            answers = []
            for _ in range(RESAMPLES):
                raw = await client.extract(caption=row["caption"])
                answers.append(
                    normalize_kind(parse_extraction_response(raw).get("kind"))
                )
            out.append(answers)
        return out
    finally:
        await client.close()


def test_no_blocking_row_is_misclassified_across_three_resamples():
    rows = [r for r in _rows() if r["blocking"]]
    assert rows, "no blocking rows in the fixture"
    answers = asyncio.run(_answers(rows))

    failures = []
    for row, samples in zip(rows, answers):
        accepted = _accepted(row)
        if any(sample not in accepted for sample in samples):
            failures.append(
                f"{row['id']} ({row['shape']}): accepted {accepted}, "
                f"got {Counter(samples)}"
            )
    assert not failures, "\n".join(failures)


def test_the_ungated_rows_are_recorded_not_gated():
    """The live captions and §1b's named casualty. Their answers are printed
    for the merge note (and for the operator's label confirmation) rather
    than asserted on."""
    rows = [r for r in _rows() if not r["blocking"]]
    if not rows:
        pytest.skip("no ungated rows in the fixture")
    answers = asyncio.run(_answers(rows))
    for row, samples in zip(rows, answers):
        proposed = row.get("proposed_kind") or "-"
        print(f"RECORDED (not gated) {row['id']}: proposed={proposed} "
              f"observed={Counter(samples)}")
