"""Every label of the four counters added by
plans/260907_events-non-event-and-recurrence-normalisation.md exists AT
IMPORT.

This is not tidiness. The production diagnostic these counters exist for is
the one this repo's own playbook records: **an ABSENT outcome label proves
that code path never ran — do not read it as zero.** That reading is only
sound while `app/metrics.py` zero-fills each closed label set at import;
delete a zero-fill and a label that has simply not been hit yet becomes
indistinguishable from a code path that cannot run. Before this file, any of
the four zero-fill loops could be deleted with the whole unit suite AND the
whole BDD suite staying green.

**Why a subprocess.** The property is "present before anything increments
them", and no in-process test can observe it. `tests/test_redis_projection_
events.py` runs real projection cycles and
`tests/bdd/persistence/events-non-event-and-recurrence-normalisation.feature`
exercises every outcome label by design; either may run first depending on
collection order, after which every label is present whether or not the
zero-fill exists, and the assertion silently stops testing anything. Worse,
the obvious in-process check is actively self-fulfilling: `Counter.labels(...)`
CREATES the child, so any helper that reads a value through `.labels()` —
`_counter()` in `test_event_extraction_service.py`, `_baseline()` in the BDD
steps — manufactures the very labels it then asserts exist.

A fresh interpreter importing `app.metrics` alone (no app wiring, no DAO, no
routes) is the only vantage point from which "at import" is observable, and
it is exactly how a scrape of a freshly started process sees it.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Imports the metrics module and NOTHING else, then reads the exposition via
# `collect()` — never via `.labels()`, which would create what it claims to
# find.
_PROBE = """
import json
from app import metrics


def by(counter, label):
    return {
        sample.labels[label]: sample.value
        for metric in counter.collect()
        for sample in metric.samples
        if sample.name.endswith("_total")
    }


print(json.dumps({
    "recurrence_text": by(metrics.EVENTS_PROJECTION_RECURRENCE_TEXT_TOTAL, "outcome"),
    "category": by(metrics.EVENTS_PROJECTION_CATEGORY_TOTAL, "outcome"),
    "ends_at": by(metrics.EVENTS_PROJECTION_ENDS_AT_TOTAL, "outcome"),
    "suppressed": by(metrics.EVENT_EXTRACTION_SUPPRESSED_RECURRING_TOTAL, "kind"),
}))
"""


@pytest.fixture(scope="module")
def labels_at_import():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_recurrence_text_counter_exposes_every_outcome_at_import(labels_at_import):
    assert labels_at_import["recurrence_text"] == {
        "absent": 0.0,
        "normalized": 0.0,
        "unchanged": 0.0,
    }


def test_category_counter_exposes_every_outcome_at_import(labels_at_import):
    """`unchanged` is the label N3 was raised about — declared but never
    reached. `off_vocabulary` carries the standing monitor for the C1 class
    (a `buffet`-shaped category appearing in production), and that watch is
    unreadable if an untouched label is simply missing from the exposition."""
    assert labels_at_import["category"] == {
        "absent": 0.0,
        "canonicalized": 0.0,
        "off_vocabulary": 0.0,
        "unchanged": 0.0,
    }


def test_ends_at_counter_exposes_every_outcome_at_import(labels_at_import):
    """Six labels, with `dropped_zero` deliberately split from
    `dropped_inverted`. `derived` matters most for the post-deploy check: it
    must become non-zero on the first cycle, and "absent" and "zero" have to
    be distinguishable for that reading to mean anything."""
    assert labels_at_import["ends_at"] == {
        "absent": 0.0,
        "carried": 0.0,
        "derived": 0.0,
        "dropped_implausible": 0.0,
        "dropped_inverted": 0.0,
        "dropped_zero": 0.0,
    }


def test_suppression_counter_exposes_every_kind_at_import(labels_at_import):
    """The watch rule is "a non-zero reading at a venue that has never had
    one means the rule is eating real recurring nights". A label that only
    materialises once it is first hit cannot express that, because the first
    hit is the event you needed to see coming."""
    assert labels_at_import["suppressed"] == {
        "food": 0.0,
        "menu": 0.0,
        "other": 0.0,
        "promotion": 0.0,
    }
