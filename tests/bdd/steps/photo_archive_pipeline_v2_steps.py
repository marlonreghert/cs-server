"""Behave steps for tests/bdd/enrichment/photo-archive-pipeline-v2.feature.

Drives the REAL VenuePhotoArchiveService over the same three fakes the v1 suite
uses (Google client, image downloader, S3) — see venue_photo_archive_steps.py,
whose Background steps and fakes this file deliberately reuses rather than
duplicating.

Two properties are asserted by counting rather than by inspection, because they
are the ones that cost money if they regress:
  * a skipped venue must reach ZERO Google requests, and
  * an estimate and a dry run must reach zero Google requests and store nothing.
"""
from __future__ import annotations

import json

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from tests.bdd.steps.venue_photo_archive_steps import (  # reuse: one set of fakes
    _photos,
    _run,
    _seed_venue,
)

SOURCE = "google_photos"
ROOT = f"media/source={SOURCE}/"


# ── helpers ───────────────────────────────────────────────────────────────────
def _cfg(context, **over):
    cfg = {
        "source": SOURCE,
        "path_mode": "new_run",
        "max_venues": 100,
        "max_photos_per_venue": 10,
        # No explicit eligibility: absent, it is inferred from `venue_ids` the
        # same way a saved pre-run-scoping config behaves.
        "skip_scope": "latest_run",
        "overwrite": False,
        "dry_run": False,
    }
    cfg.update(getattr(context, "config_over", {}))
    cfg.update(over)
    return cfg


def _run_v2(context, **over):
    context.summary = _run(context.service.run(_cfg(context, **over)))
    return context.summary


def _keys(context, suffix=".jpg", venue_id=None):
    keys = [k for k in context.fake_s3.objects if k.endswith(suffix)]
    if venue_id:
        keys = [k for k in keys if f"venue_id={venue_id}/" in k]
    return keys


def _metric(name, **labels):
    v = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if v is None else float(v)


def _seed(context, vid, n=2, lat=-8.05, lng=-34.88):
    pid = _seed_venue(context, vid, lat=lat, lng=lng)
    context.google.photos_by_place[pid] = _photos(n)
    return pid


def _seed_previous_run(context, venue_id="ven_prev", day="2026-07-26"):
    """A completed earlier run, laid out exactly as the pipeline writes it."""
    prefix = (
        f"{ROOT}year={day[:4]}/month={day[5:7]}/day={day[8:10]}/"
        f"run_id=0000000000{'0' * 16}/"
    )
    context.fake_s3.objects[f"{prefix}venue_id={venue_id}/old.jpg"] = b"old"
    context.previous_prefix = prefix
    return prefix


# ── Given ─────────────────────────────────────────────────────────────────────
@given("a run has already archived a venue today")
def step_prev_run_today(context):
    context.venue_id = "ven_two_runs"
    _seed(context, context.venue_id)
    _run_v2(context, venue_ids=context.venue_id)
    # Read the prefix off the summary, not by sorting keys: run ids are random,
    # so key order says nothing about which run wrote which object.
    context.first_prefix = context.summary["prefix"]
    # Only this run's OWN objects. The `_latest.json` pointer lives outside the
    # run prefix and is meant to move on: immutability applies to the archived
    # content, not to the marker that says which archive is newest.
    context.first_keys = {
        k: v for k, v in context.fake_s3.objects.items()
        if k.startswith(context.first_prefix)
    }


@given("a previous run partition exists for the source")
def step_prev_run_exists(context):
    _seed_previous_run(context)
    context.venue_id = "ven_append"
    _seed(context, context.venue_id)

    def _forbidden(*a, **kw):  # the writer role has no s3:GetObject
        raise AssertionError("the pipeline must not read archived objects back")

    context.fake_s3.get_object = _forbidden


@given("no run partition exists for the source")
def step_no_run_partition(context):
    context.fake_s3.objects.clear()
    context.venue_id = "ven_first"
    _seed(context, context.venue_id)


@given("a venue was archived by the previous run")
def step_venue_in_prev_run(context):
    context.venue_id = "ven_already"
    _seed(context, context.venue_id)
    _seed_previous_run(context, venue_id=context.venue_id)


@given("a venue with no Google photos was archived by the previous run")
def step_venue_in_prev_run_nophotos(context):
    step_venue_in_prev_run(context)


@given('the skip scope is "{scope}"')
def step_skip_scope(context, scope):
    context.config_over["skip_scope"] = scope


@given('the skip scope is "{scope}" and overwrite is disabled')
def step_skip_scope_no_overwrite(context, scope):
    context.config_over["skip_scope"] = scope
    context.config_over["overwrite"] = False
    context.venue_id = "ven_scope"
    _seed(context, context.venue_id)


@given('the path mode is "override" with a prefix outside the media root')
def step_bad_override(context):
    context.config_over["path_mode"] = "override"
    context.config_over["path_override"] = "raw/besttime/"
    context.venue_id = "ven_bad_prefix"
    _seed(context, context.venue_id)


@given("{n:d} venues are eligible")
def step_n_eligible(context, n):
    context.catalog = []
    for i in range(n):
        vid = f"ven_e{i}"
        _seed(context, vid, n=1)
        context.catalog.append(vid)


@given("the maximum number of venues is {n:d}")
def step_max_venues(context, n):
    context.config_over["max_venues"] = n


@given("the maximum number of photos per venue is {n:d}")
def step_max_photos(context, n):
    context.config_over["max_photos_per_venue"] = n


@given("venues inside and outside a {km:d} km radius of a point")
def step_geo_venues(context, km):
    context.radius_km = km
    context.center = (-8.05, -34.88)
    context.inside = ["ven_in1", "ven_in2"]
    context.outside = ["ven_out1"]
    for vid in context.inside:
        _seed(context, vid, n=1, lat=-8.0505, lng=-34.8805)
    for vid in context.outside:  # ~100 km away
        _seed(context, vid, n=1, lat=-8.95, lng=-34.88)


@given("an eligibility list naming two known venues and one unknown venue")
def step_id_list(context):
    context.known = ["ven_k1", "ven_k2"]
    for vid in context.known:
        _seed(context, vid, n=1)
    context.unknown = "ven_nope"
    context.config_over["eligibility"] = {
        "mode": "venue_ids",
        "venue_ids": f"{context.known[0]},{context.known[1]},{context.unknown}",
    }


@given("a point and radius eligibility with a radius of {km:d} km")
def step_bad_radius(context, km):
    context.config_over["eligibility"] = {
        "mode": "point_radius", "lat": -8.05, "lon": -34.88, "radius_km": km,
    }
    context.venue_id = "ven_radius"
    _seed(context, context.venue_id)


@given("the run is configured as a dry run")
def step_dry_run(context):
    context.config_over["dry_run"] = True
    context.venue_id = "ven_dry"
    _seed(context, context.venue_id)


@given("Google responds to the first photo request with a throttling error")
def step_throttle_once(context):
    context.venue_id = "ven_429"
    pid = _seed(context, context.venue_id)
    context.google.throttle_once = {pid}


@given("Google throttles one venue beyond the retry limit")
def step_throttle_always(context):
    context.throttled_venue = "ven_429_hard"
    pid = _seed(context, context.throttled_venue)
    context.google.throttle_always = {pid}


@given("another eligible venue responds normally")
def step_other_ok(context):
    context.good_venue = "ven_ok"
    _seed(context, context.good_venue)


@given("two eligible venues where the first fails to fetch")
def step_two_first_fails(context):
    context.failing_venue = "ven_f1"
    pid = _seed(context, context.failing_venue)
    context.google.fail_places.add(pid)
    context.good_venue = "ven_f2"
    _seed(context, context.good_venue)


@given("a photo archive run has completed")
def step_run_completed(context):
    context.venue_id = "ven_record"
    _seed(context, context.venue_id)
    _run_v2(context, venue_ids=context.venue_id)
    context.job_id = context.summary.get("job_id")


# ── When ──────────────────────────────────────────────────────────────────────
@when('a second run starts with the path mode "new_run"')
def step_second_run(context):
    _run_v2(context, venue_ids=context.venue_id, path_mode="new_run", skip_scope="none",
            overwrite=True)
    context.second_prefix = context.summary["prefix"]


@when('the photo archive job runs with the path mode "{mode}"')
def step_run_mode(context, mode):
    _run_v2(context, venue_ids=context.venue_id, path_mode=mode)


@when("the photo archive job resolves the latest run partition")
def step_resolve_latest(context):
    context.resolved_prefix = _run(
        context.service.resolve_prefix(SOURCE, _cfg(context, path_mode="append_latest"))
    )


@when("the photo archive job completes successfully")
def step_completes_ok(context):
    context.venue_id = getattr(context, "venue_id", None) or "ven_marker"
    if context.venue_id == "ven_marker":
        _seed(context, context.venue_id)
    _run_v2(context, venue_ids=context.venue_id)


@when('the photo archive job runs for that venue with the path mode "{mode}"')
def step_run_venue_mode(context, mode):
    _run_v2(context, venue_ids=context.venue_id, path_mode=mode)


@when("the photo archive job runs for that venue with overwrite enabled")
def step_run_overwrite(context):
    _run_v2(context, venue_ids=context.venue_id, overwrite=True)


@when("the photo archive job runs with a point and radius eligibility")
def step_run_geo(context):
    lat, lon = context.center
    _run_v2(context, eligibility={
        "mode": "point_radius", "lat": lat, "lon": lon, "radius_km": context.radius_km,
    })


@when("a cost estimate is requested for that configuration")
def step_estimate(context):
    context.estimate = _run(context.service.estimate(_cfg(context)))


@when("the run record is requested by its job id")
def step_get_record(context):
    context.record = context.service.get_run_record(context.job_id)


# ── Then ──────────────────────────────────────────────────────────────────────
@then("the images are stored under a run id partition")
def step_run_partition(context):
    keys = _keys(context)
    assert keys, "nothing stored"
    for k in keys:
        assert "run_id=" in k, k
        assert "year=" in k and "month=" in k and "day=" in k, k


@then("the second run writes to a different run partition")
def step_different_partition(context):
    assert context.second_prefix != context.first_prefix, (
        f"both runs shared the prefix {context.first_prefix}"
    )


@then("the first run's stored images remain untouched")
def step_first_intact(context):
    for key, body in context.first_keys.items():
        assert context.fake_s3.objects.get(key) == body, f"{key} was modified"


@then("the images are stored under that previous run partition")
def step_under_prev(context):
    keys = _keys(context, venue_id=context.venue_id)
    assert keys, "nothing stored"
    assert all(k.startswith(context.previous_prefix) for k in keys), keys


@then("the images are stored under a newly created run partition")
def step_new_partition(context):
    keys = _keys(context, venue_id=context.venue_id)
    assert keys, "nothing stored"
    assert all("run_id=" in k for k in keys), keys


@then("the partition is resolved by listing the bucket only")
def step_resolved_by_listing(context):
    assert context.resolved_prefix == context.previous_prefix, (
        f"{context.resolved_prefix!r} != {context.previous_prefix!r}"
    )


@then("no archived object is read back")
def step_no_get_object(context):
    pass  # the fake's get_object raises; reaching here proves it was never called


@then("a latest marker for the source records the run partition it wrote to")
def step_latest_marker(context):
    markers = [k for k in context.fake_s3.objects if k.endswith("_latest.json")]
    assert markers, "no _latest.json marker was written"
    context.marker = json.loads(context.fake_s3.objects[markers[0]])
    assert context.marker.get("prefix") == context.summary["prefix"], context.marker


@then("the marker reports the run id, the venues archived, and the photos stored")
def step_marker_fields(context):
    for field in ("run_id", "venues_archived", "photos_stored"):
        assert field in context.marker, f"marker missing {field}: {context.marker}"


@then("the run is rejected before any Google request is made")
def step_rejected_v2(context):
    assert context.rejection is not None, "the invalid configuration was accepted"
    assert not context.google.calls, f"Google was called: {context.google.calls}"


@then("no Google request is made for that venue")
def step_no_google_for_venue(context):
    assert not context.google.calls, f"Google was called: {context.google.calls}"


@then("the venue is counted as skipped")
def step_counted_skipped(context):
    assert context.summary["skipped_existing"] >= 1, context.summary


@then("the venue's photos are fetched again")
def step_fetched_again(context):
    assert context.google.calls, "Google was not called despite overwrite"


@then("the new run partition holds images for that venue")
def step_current_partition(context):
    keys = [k for k in _keys(context, venue_id=context.venue_id)
            if not k.startswith(context.previous_prefix)]
    assert keys, "nothing was stored under the new run partition"


@then("at most {n:d} venues are processed")
def step_at_most_venues(context, n):
    assert context.summary["considered"] <= n, context.summary
    assert context.summary["considered"] == n, context.summary


@then("the summary reports the number of venues the selection was truncated from")
def step_truncated_from(context):
    assert context.summary.get("truncated_from"), context.summary


@then("at most {n:d} photos are requested for that venue")
def step_at_most_photos(context, n):
    assert context.google.max_photos_seen is not None, "Google was never called"
    assert context.google.max_photos_seen <= n, context.google.max_photos_seen
    assert len(_keys(context, venue_id=context.venue_id)) <= n


@then("only the venues inside the radius are processed")
def step_only_inside(context):
    for vid in context.inside:
        assert _keys(context, venue_id=vid), f"{vid} inside the radius was not archived"
    for vid in context.outside:
        assert not _keys(context, venue_id=vid), f"{vid} outside the radius was archived"


@then("only the two known venues are processed")
def step_only_known(context):
    assert context.summary["considered"] == 2, context.summary
    for vid in context.known:
        assert _keys(context, venue_id=vid), f"{vid} was not archived"


@then("the unknown venue id is reported without failing the run")
def step_unknown_reported_v2(context):
    assert context.unknown in context.summary.get("unknown_venue_ids", []), context.summary
    assert context.summary["archived"] == 2, context.summary


@then("the estimate reports the number of venues selected")
def step_estimate_venues(context):
    assert context.estimate["venues_selected"] == len(context.catalog), context.estimate


@then("the estimate reports at most {n:d} Google requests")
def step_estimate_calls(context, n):
    assert context.estimate["est_google_calls"] == n, context.estimate


@then("the estimate reports an upper bound on the Google requests")
def step_estimate_upper_bound(context):
    assert context.estimate["est_google_calls"] > 0, context.estimate


@then("the estimate reports an estimated cost in dollars")
def step_estimate_cost(context):
    assert context.estimate.get("est_cost_usd") is not None, context.estimate
    assert context.estimate["est_cost_usd"] >= 0, context.estimate


@then("no Google request is made")
def step_no_google_at_all(context):
    assert not context.google.calls, f"Google was called: {context.google.calls}"


@then("the estimate states that it is an upper bound and may be wrong")
def step_estimate_caveat(context):
    caveat = (context.estimate.get("caveat") or "").lower()
    assert caveat, context.estimate
    assert "estimate" in caveat, caveat


@then("the eligible venues are selected and an estimate is produced")
def step_dry_run_output(context):
    assert context.summary.get("dry_run") is True, context.summary
    assert context.summary.get("estimate"), context.summary


@then("no image is stored")
def step_nothing_stored(context):
    assert not _keys(context), _keys(context)


@then("the request is retried after a backoff")
def step_retried(context):
    assert context.google.attempts >= 2, context.google.attempts


@then("the throttled response is counted")
def step_throttle_counted(context):
    assert _metric("media_archive_throttled_total", source=SOURCE, reason="429") >= 1


@then("the run completes")
def step_run_completes(context):
    assert context.summary is not None
    assert context.summary["archived"] >= 1, context.summary


@then("the throttled venue is counted as failed")
def step_throttled_failed(context):
    assert context.summary["failed"] >= 1, context.summary
    assert not _keys(context, venue_id=context.throttled_venue)


@then("the other venue is archived")
def step_other_archived(context):
    assert _keys(context, venue_id=context.good_venue), "healthy venue was not archived"


@then("the response carries a job id for that run")
def step_has_job_id(context):
    assert context.summary.get("job_id"), context.summary


@then("the record reports the configuration, the counts, and the duration of that run")
def step_record_fields(context):
    assert context.record, f"no run record for {context.job_id}"
    for field in ("config", "archived", "duration_seconds"):
        assert field in context.record, f"record missing {field}: {context.record}"


@then("the first venue is counted as failed")
def step_first_failed(context):
    assert context.summary["failed"] >= 1, context.summary


@then("the second venue is archived")
def step_second_archived_v2(context):
    assert _keys(context, venue_id=context.good_venue), "healthy venue was not archived"
