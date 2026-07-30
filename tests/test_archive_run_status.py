"""Unit tests for the archive run-status derivation.

The BDD feature covers what an operator sees end-to-end. These cover the
derivation itself, where the edges live: which buckets count as undelivered, and
the totality property — `run_status` must never raise, because it runs after the
photos are already stored and a metric must not be able to fail a completed run.
"""
from __future__ import annotations

import pytest

from app.services.venue_photo_archive_service import UNDELIVERED_BUCKETS, run_status


def _summary(**over):
    base = {
        "archived": 0, "skipped_existing": 0, "no_place_id": 0, "info_only": 0,
        "timeout": 0, "no_query": 0, "no_result": 0, "failed": 0,
        "credit_exhausted": False, "aborted": False,
    }
    base.update(over)
    return base


class TestSuccess:
    def test_all_archived_is_success(self):
        assert run_status(_summary(archived=8)) == "success"

    def test_a_run_that_only_skipped_is_still_success(self):
        # A no-op run is not a failure: everything asked for was already there.
        assert run_status(_summary(skipped_existing=8)) == "success"

    def test_info_only_counts_as_delivered(self):
        # The venue was found and its place data stored; it simply had no photos.
        assert run_status(_summary(archived=3, info_only=2)) == "success"

    def test_empty_run_is_success(self):
        assert run_status(_summary()) == "success"


class TestPartial:
    @pytest.mark.parametrize("bucket", UNDELIVERED_BUCKETS)
    def test_any_undelivered_venue_makes_the_run_partial(self, bucket):
        assert run_status(_summary(archived=7, **{bucket: 1})) == "partial"

    def test_a_run_that_delivered_nothing_is_partial_not_success(self):
        # The regression that motivated this: 1 archived of 8 was reported clean.
        assert run_status(_summary(archived=0, timeout=8)) == "partial"

    def test_mostly_good_is_still_partial(self):
        assert run_status(_summary(archived=99, no_result=1)) == "partial"


class TestError:
    def test_credit_exhaustion_is_an_error_not_a_partial(self):
        # The run STOPPED; the venues it never reached are not "undelivered", they
        # were never attempted. That is a different thing from a venue that failed.
        assert run_status(_summary(archived=3, credit_exhausted=True)) == "error"

    def test_abort_is_an_error(self):
        assert run_status(_summary(archived=3, aborted=True)) == "error"

    def test_error_wins_over_partial(self):
        assert run_status(_summary(timeout=2, credit_exhausted=True)) == "error"


class TestTotality:
    @pytest.mark.parametrize("bad", [
        {},                                   # nothing at all
        {"archived": None},                   # a missing count
        {"timeout": "3"},                     # a stringly-typed count
        {"failed": object()},                 # something uncastable
    ])
    def test_never_raises(self, bad):
        # Runs after the photos are stored; a metric must not be able to fail a
        # run that already did its work.
        assert run_status(dict(bad)) in ("success", "partial", "error")

    def test_an_uncastable_summary_resolves_to_error_not_success(self):
        # An unclassifiable run is exactly the one an operator must look at, so
        # the safe default is the loud one.
        assert run_status({"failed": object()}) == "error"

    def test_stringly_typed_counts_are_still_counted(self):
        assert run_status({"timeout": "3"}) == "partial"
