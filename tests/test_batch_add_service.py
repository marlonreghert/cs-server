"""Unit tests for the server-side batch venue-add service."""
import asyncio
import pytest

from app.handlers.add_venue_handler import AddVenueOutcome
from app.config import settings
from app.models.batch_add import BatchAddRequest
import app.services.batch_add_service as bas
from app.services.batch_add_service import BatchAddService, _classify
from tests.async_job_wait import await_job_task
from tests.venue_add_job_fake import InMemoryVenueAddJobStore


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch):
    # Zero the coord-retry backoffs + steady pace so tests don't wall-clock sleep.
    monkeypatch.setattr(bas, "_COORD_RETRY_BACKOFFS", (0.0, 0.0))
    monkeypatch.setattr(bas, "_GOOGLE_PACE_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _clean_batch_lock():
    # The batch single-flight lock is module-global (app/services/job_lock).
    # Reset it around every test so a run that leaves it held (or a crashed
    # launch) never leaks into the next test.
    from app.services import job_lock
    job_lock.release(bas.BATCH_ADD_LOCK)
    yield
    job_lock.release(bas.BATCH_ADD_LOCK)


# ── outcome classification ───────────────────────────────────────────────────
@pytest.mark.parametrize("outcome, expected", [
    (AddVenueOutcome(201, {"status": "created", "venue_id": "v1"}), "created"),
    (AddVenueOutcome(201, {"status": "created", "recovered_from_timeout": True,
                           "venue_id": "v2"}), "created_recovered_timeout"),
    (AddVenueOutcome(201, {"status": "created_google_only", "source": "google_places",
                           "venue_id": "vsg_abc"}), "created_google_only"),
    (AddVenueOutcome(200, {"status": "already_exists", "venue_id": "v3"}),
     "already_exists"),
    (AddVenueOutcome(200, {"status": "matched_via_geo_fallback",
                           "newly_linked": True, "match_reason": "containment",
                           "venue_id": "v4"}), "geo_linked"),
    (AddVenueOutcome(429, {"detail": "Monthly venue quota exhausted"}),
     "quota_exhausted"),
    (AddVenueOutcome(429, {"detail": "BestTime monthly venue cap reached"}),
     "besttime_monthly_cap"),
    (AddVenueOutcome(502, {"detail": "BestTime returned an unparseable response"}),
     "besttime_bad_response"),
    (AddVenueOutcome(502, {"detail": "BestTime rejected the address ...",
                           "besttime_message": "too new", "candidates_seen": 0}),
     "besttime_rejected_no_geo_match"),
    (AddVenueOutcome(502, {"detail": "BestTime is unavailable: ReadTimeout"}),
     "besttime_error"),
])
def test_classify(outcome, expected):
    assert _classify(outcome)["outcome"] == expected


def test_classify_geo_link_carries_reason():
    r = _classify(AddVenueOutcome(200, {"status": "matched_via_geo_fallback",
                                        "newly_linked": True,
                                        "match_reason": "exact", "venue_id": "vX"}))
    assert r["newly_linked"] is True and r["match_reason"] == "exact"
    assert r["venue_id"] == "vX"


def test_classify_carries_instagram_on_created_row():
    """plans/260811_add-venue-instagram-discovery.md: _classify must copy the
    instagram object onto a created row's batch result."""
    ig = {"status": "found", "handle": "barvibes", "url": "https://instagram.com/barvibes",
          "source": "google_website", "confidence": 0.9}
    r = _classify(AddVenueOutcome(201, {
        "status": "created", "venue_id": "v1", "instagram": ig,
    }))
    assert r["outcome"] == "created"
    assert r["instagram"] == ig


def test_classify_carries_instagram_on_newly_linked_geo_row():
    ig = {"status": "not_found", "handle": None, "url": None, "source": None,
          "confidence": 0.0}
    r = _classify(AddVenueOutcome(200, {
        "status": "matched_via_geo_fallback", "newly_linked": True,
        "match_reason": "exact", "venue_id": "v4", "instagram": ig,
    }))
    assert r["outcome"] == "geo_linked"
    assert r["instagram"] == ig


def test_classify_instagram_is_none_when_geo_link_was_not_newly_linked():
    """A geo-fallback link to an already-catalogued venue runs no discovery —
    AddVenueHandler's body carries no 'instagram' key at all, so the row
    reflects that (None, not a stale/fabricated value)."""
    r = _classify(AddVenueOutcome(200, {
        "status": "matched_via_geo_fallback", "newly_linked": False,
        "match_reason": "exact", "venue_id": "v5",
    }))
    assert r["outcome"] == "geo_linked"
    assert r["instagram"] is None


def test_classify_created_google_only_is_not_confused_with_timeout_recovery():
    """A 201 with status=created_google_only must classify distinctly from a
    plain 'created' even though both are 201s — the batch job summary needs
    to tell the two apart (no BestTime venue exists for the Google-only row)."""
    r = _classify(AddVenueOutcome(201, {
        "status": "created_google_only", "source": "google_places",
        "venue_id": "vsg_xyz",
    }))
    assert r["outcome"] == "created_google_only"
    assert r["venue_id"] == "vsg_xyz"


def test_created_google_only_is_not_a_stop_outcome():
    """created_google_only is a success state (no BestTime credit was drawn,
    nothing failed) — it must never appear in _STOP_OUTCOMES, or a batch job
    of mostly-unforecastable venues would halt on its own first success."""
    assert "created_google_only" not in bas._STOP_OUTCOMES


# ── service harness ──────────────────────────────────────────────────────────
class _Snap:
    def __init__(self, n):
        self.month_counter, self.quota, self.year_month = n, 1000, "2026-07"


class _Budget:
    def __init__(self):
        self.n = 400

    def get_snapshot(self):
        return _Snap(self.n)


class _Venue:
    def __init__(self, active):
        self._active = active

    def is_active(self):
        return self._active


class _Dao:
    def __init__(self, venues=None):
        self.venues = venues or {}

    def get_venue(self, vid):
        return self.venues.get(vid)


class _Handler:
    """Scripted handler: maps venue_name -> AddVenueOutcome; records calls.

    `cached` maps (name) -> venue_id for the address-hash fast-path;
    `dao_venues` maps venue_id -> _Venue for the active check. `trust_calls`
    records each add()'s (venue_name, coordinates_trusted) so tests can pin
    the trust flag BatchAddService._resolve_coords threads through (see
    plans/260816_venue-address-cache-integrity.md)."""
    def __init__(self, script, cached=None, dao_venues=None):
        self.script = script
        self.calls = []
        self.trust_calls = []
        self._cached = cached or {}
        self.venue_dao = _Dao(dao_venues or {})

    def _lookup_cached_venue_id(self, name, address):
        return self._cached.get(name)

    async def add(self, request, *, coordinates_trusted=True):
        self.calls.append(request.venue_name)
        self.trust_calls.append((request.venue_name, coordinates_trusted))
        return self.script[request.venue_name]


class _Google:
    """Resolves coords for names in `coords`; None otherwise. `fail_first`
    maps name -> number of leading None responses before success (to exercise
    the paced retry)."""
    def __init__(self, coords, fail_first=None):
        self.coords = coords
        self.fail_first = dict(fail_first or {})
        self.calls = {}

    async def resolve_coordinates(self, name, address, place_id=None,
                                  lat_bias=None, lng_bias=None):
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.fail_first.get(name, 0) > 0:
            self.fail_first[name] -= 1
            return place_id, None, None
        c = self.coords.get(name)
        if c is None:
            return place_id, None, None
        return (place_id or "pid_" + name), c[0], c[1]


def _service(handler, google=None, budget=None):
    return BatchAddService(
        handler=handler,
        job_store=InMemoryVenueAddJobStore(),
        google_client=google,
        budget_service=budget or _Budget(),
    )


async def _run_to_completion(svc, req):
    accepted = svc.start_job(req)
    job_id = accepted["job_id"]
    # Await the background task the service scheduled — see
    # tests/async_job_wait.py for why counting asyncio.sleep(0) yields here was
    # a race rather than a wait.
    await await_job_task(svc, job_id)
    return svc.get_job(job_id)


@pytest.mark.asyncio
async def test_batch_runs_all_rows_and_summarizes():
    handler = _Handler({
        "A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"}),
        "B": AddVenueOutcome(200, {"status": "already_exists", "venue_id": "vB"}),
        "C": AddVenueOutcome(502, {"detail": "rejected the address",
                                   "besttime_message": "too new",
                                   "candidates_seen": 0}),
    })
    google = _Google({"A": (-9.6, -35.7), "B": (-9.61, -35.71), "C": (-9.62, -35.72)})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": "A", "venue_address": "addr A"},
        {"venue_name": "B", "venue_address": "addr B"},
        {"venue_name": "C", "venue_address": "addr C"},
    ])
    job = await _run_to_completion(svc, req)
    assert job["status"] == "done"
    assert job["processed"] == 3
    assert job["summary"] == {"created": 1, "already_exists": 1,
                              "besttime_rejected_no_geo_match": 1}
    assert handler.calls == ["A", "B", "C"]
    assert job["budget_before"]["month_counter"] == 400
    assert job["budget_after"] is not None


@pytest.mark.asyncio
async def test_quota_exhausted_stops_the_batch():
    """Pinned to concurrency 1, where "stop" means STRICTLY no further row is
    attempted. That exact guarantee cannot survive parallelism — see the
    sibling test below — so it is asserted in the regime where it holds rather
    than quietly relaxed for both."""
    settings.batch_add_concurrency = 1
    handler = _Handler({
        "A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"}),
        "B": AddVenueOutcome(429, {"detail": "Monthly venue quota exhausted"}),
        "C": AddVenueOutcome(201, {"status": "created", "venue_id": "vC"}),
    })
    google = _Google({"A": (-9.6, -35.7), "B": (-9.61, -35.71), "C": (-9.62, -35.72)})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": "A", "venue_address": "a"},
        {"venue_name": "B", "venue_address": "b"},
        {"venue_name": "C", "venue_address": "c"},
    ])
    job = await _run_to_completion(svc, req)
    assert job["status"] == "stopped"
    assert job["processed"] == 2  # C never attempted
    assert handler.calls == ["A", "B"]
    assert "quota_exhausted" in job["stopped_reason"]


@pytest.mark.asyncio
async def test_unresolved_coords_row_is_skipped_without_calling_handler():
    handler = _Handler({
        "A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"}),
    })
    google = _Google({"A": (-9.6, -35.7)})  # "Ghost" absent -> unresolved
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": "Ghost", "venue_address": "nowhere"},
        {"venue_name": "A", "venue_address": "a"},
    ])
    job = await _run_to_completion(svc, req)
    assert job["status"] == "done"
    assert job["summary"] == {"skipped_unresolved_coords": 1, "created": 1}
    assert handler.calls == ["A"]  # Ghost never reached the handler


@pytest.mark.asyncio
async def test_prepassed_coords_skip_google():
    handler = _Handler({
        "A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"}),
    })
    # No google client at all — coords supplied inline must still work.
    svc = _service(handler, google=None)
    req = BatchAddRequest(venues=[
        {"venue_name": "A", "venue_address": "a",
         "venue_lat": -9.6, "venue_lng": -35.7, "place_id": "pidA"},
    ])
    job = await _run_to_completion(svc, req)
    assert job["summary"] == {"created": 1}
    assert handler.calls == ["A"]


# ── coordinate-trust gate (plans/260816_venue-address-cache-integrity.md) ─────
@pytest.mark.asyncio
async def test_caller_supplied_latlng_is_trusted():
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})})
    svc = _service(handler, google=None)
    req = BatchAddRequest(venues=[
        {"venue_name": "A", "venue_address": "a", "venue_lat": -9.6, "venue_lng": -35.7},
    ])
    await _run_to_completion(svc, req)
    assert handler.trust_calls == [("A", True)]


@pytest.mark.asyncio
async def test_caller_supplied_place_id_resolution_is_trusted():
    # No lat/lng on the row — must resolve via Google, but the place_id came
    # from the caller, so the resulting coordinate is trusted.
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})})
    google = _Google({"A": (-9.6, -35.7)})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": "A", "venue_address": "a", "place_id": "pid_caller_A"},
    ])
    await _run_to_completion(svc, req)
    assert handler.trust_calls == [("A", True)]


@pytest.mark.asyncio
async def test_bare_text_search_resolution_is_untrusted():
    # No lat/lng, no place_id, no bias — the exact unbiased-Text-Search gap
    # that let a submission permanently mis-link to an unrelated venue.
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})})
    google = _Google({"A": (-9.6, -35.7)})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[{"venue_name": "A", "venue_address": "a"}])
    await _run_to_completion(svc, req)
    assert handler.trust_calls == [("A", False)]


@pytest.mark.asyncio
async def test_bias_only_resolution_is_still_untrusted():
    # bias_lat/bias_lng only steer Google's relevance ranking; they are not a
    # caller-verified location for THIS venue, so a Text-Search resolution
    # stays untrusted even when biased toward the right city.
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})})
    google = _Google({"A": (-9.6, -35.7)})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": "A", "venue_address": "a", "bias_lat": -9.6, "bias_lng": -35.7},
    ])
    await _run_to_completion(svc, req)
    assert handler.trust_calls == [("A", False)]


@pytest.mark.asyncio
async def test_already_active_row_skips_google_and_handler():
    # Re-run fast-path: an address-hash hit on an ACTIVE venue records
    # already_exists with zero Google/BestTime work.
    handler = _Handler(
        script={"B": AddVenueOutcome(201, {"status": "created", "venue_id": "vB"})},
        cached={"A": "vA"},
        dao_venues={"vA": _Venue(active=True)},
    )
    google = _Google({"B": (-9.6, -35.7)})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": "A", "venue_address": "a"},   # already active
        {"venue_name": "B", "venue_address": "b"},   # new
    ])
    job = await _run_to_completion(svc, req)
    assert job["summary"] == {"already_exists": 1, "created": 1}
    assert handler.calls == ["B"]            # A never reached the handler
    assert "A" not in google.calls           # A never touched Google


@pytest.mark.asyncio
async def test_deprecated_cached_row_falls_through_to_full_flow():
    # An address-hash hit whose venue is NOT active must not short-circuit.
    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA2"})},
        cached={"A": "vA_old"},
        dao_venues={"vA_old": _Venue(active=False)},
    )
    google = _Google({"A": (-9.6, -35.7)})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[{"venue_name": "A", "venue_address": "a"}])
    job = await _run_to_completion(svc, req)
    assert job["summary"] == {"created": 1}
    assert handler.calls == ["A"]


@pytest.mark.asyncio
async def test_coord_resolution_retries_a_transient_miss():
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created",
                                                   "venue_id": "vA"})})
    google = _Google({"A": (-9.6, -35.7)}, fail_first={"A": 1})  # miss then hit
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[{"venue_name": "A", "venue_address": "a"}])
    job = await _run_to_completion(svc, req)
    assert job["summary"] == {"created": 1}
    assert google.calls["A"] == 2  # first None, retry succeeded


@pytest.mark.asyncio
async def test_coord_resolution_gives_up_after_bounded_retries():
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created",
                                                   "venue_id": "vA"})})
    google = _Google({"A": (-9.6, -35.7)}, fail_first={"A": 9})  # always miss
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[{"venue_name": "A", "venue_address": "a"}])
    job = await _run_to_completion(svc, req)
    assert job["summary"] == {"skipped_unresolved_coords": 1}
    assert google.calls["A"] == 3   # initial + 2 bounded retries
    assert handler.calls == []


@pytest.mark.asyncio
async def test_job_doc_is_persisted_and_readable():
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created",
                                                   "venue_id": "vA"})})
    svc = _service(handler, _Google({"A": (-9.6, -35.7)}))
    req = BatchAddRequest(venues=[{"venue_name": "A", "venue_address": "a"}],
                          label="test-run")
    job = await _run_to_completion(svc, req)
    reread = svc.get_job(job["job_id"])
    assert reread["label"] == "test-run"
    assert reread["results"][0]["venue_id"] == "vA"


# ── single-flight: only one batch job at a time ──────────────────────────────
@pytest.mark.asyncio
async def test_second_batch_refused_while_one_is_running():
    from app.services import job_lock

    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created",
                                                   "venue_id": "vA"})})
    svc = _service(handler, _Google({"A": (-9.6, -35.7)}))
    req = BatchAddRequest(venues=[{"venue_name": "A", "venue_address": "a"}])

    first = svc.start_job(req)          # acquires the batch lock
    assert first["status"] == "running"
    assert job_lock.is_running(bas.BATCH_ADD_LOCK) is True

    second = svc.start_job(req)         # refused while the first is running
    assert second["status"] == "already_running"
    assert "job_id" not in second

    # Drain the first job; the lock must be released when it finishes.
    # _on_done (which releases the lock) is registered as the task's FIRST done
    # callback in start_job, before any waiter exists, so it has already run by
    # the time this await resumes.
    await await_job_task(svc, first["job_id"])
    assert job_lock.is_running(bas.BATCH_ADD_LOCK) is False

    # A new batch may start now that the first finished.
    third = svc.start_job(req)
    assert third["status"] == "running"

    # Drain it too — an undrained task here is a pre-existing loop/task leak
    # (harmless to this test's own assertions, but it gets garbage-collected
    # at an unpredictable later point, which pytest-asyncio flags with a
    # "Task was destroyed but it is pending!" warning wherever that GC happens
    # to land, potentially confusing an unrelated later test).
    await await_job_task(svc, third["job_id"])


# ── RDS job store: persistence + job_type (plans/260814_venue-add-job-rds-
# tracking.md) ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_job_is_stored_as_job_type_batch():
    """The job_store row must carry job_type="batch" so
    VenueAddJobStore/InMemoryVenueAddJobStore's shape_job_row applies the
    batch (not single) API-shape rules — checked here via the raw stored row
    (job_store.rows), since get_job()/shape_job_row strip job_type from the
    API-facing dict by design."""
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})})
    svc = _service(handler, _Google({"A": (-9.6, -35.7)}))
    req = BatchAddRequest(venues=[{"venue_name": "A", "venue_address": "a"}])
    job = await _run_to_completion(svc, req)
    stored = svc.job_store.rows[job["job_id"]]
    assert stored["job_type"] == "batch"


@pytest.mark.asyncio
async def test_persist_failure_is_swallowed_and_recorded_but_run_continues():
    """A job_store.save() failure must not crash the run (matches the
    pre-RDS Redis _save's own except-and-continue behaviour) and must
    increment VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL{job_type="batch"} so a
    sustained RDS outage is observable."""
    from app.metrics import VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL

    before = VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL.labels(job_type="batch")._value.get()

    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})})
    svc = _service(handler, _Google({"A": (-9.6, -35.7)}))
    svc.job_store.fail_next_save(1)  # fails the very first (initial) save
    req = BatchAddRequest(venues=[{"venue_name": "A", "venue_address": "a"}])
    job = await _run_to_completion(svc, req)

    assert job["status"] == "done"  # the run completed despite the one failed save
    after = VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL.labels(job_type="batch")._value.get()
    assert after == before + 1


class _FakeCrashedTask:
    """Minimal asyncio.Task stand-in for _on_done: claims to have crashed
    with a RuntimeError, without actually having to force _run_job's own
    per-row try/except (which swallows everything handler.add can raise) to
    leak all the way out — _on_done is the unit under test here, not
    _run_job."""
    def cancelled(self):
        return False

    def exception(self):
        return RuntimeError("simulated unexpected crash")


def test_on_done_crash_path_resave_preserves_job_type():
    """Regression test: _on_done's crash-path job comes from
    self.get_job(job_id), whose result has job_type stripped (shape_job_row
    — job_type must never appear in an HTTP response). _persist() must
    re-stamp job_type before writing back, or this crash-path save would
    silently drop the column (an explicit-column upsert, not a partial
    patch) after the very first save."""
    handler = _Handler({"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})})
    svc = _service(handler)
    job_id = "crash-test-job"
    svc.job_store.save({
        "job_id": job_id, "job_type": "batch", "label": None, "status": "running",
        "total": 1, "processed": 0, "started_at": 0.0, "finished_at": None,
        "stopped_reason": None, "resolve_coords": False, "summary": {}, "results": [],
        "budget_before": None, "budget_after": None,
    })

    svc._on_done(job_id, _FakeCrashedTask())

    stored = svc.job_store.rows[job_id]
    assert stored["job_type"] == "batch"
    assert stored["status"] == "failed"
    assert "RuntimeError" in stored["stopped_reason"]


# ── concurrency (settings.batch_add_concurrency) ────────────────────────────


@pytest.fixture(autouse=True)
def _restore_concurrency():
    """Every test in this module runs at the shipped default unless it says
    otherwise, and no test can leak its setting into the next one."""
    saved = settings.batch_add_concurrency
    yield
    settings.batch_add_concurrency = saved


@pytest.mark.asyncio
async def test_a_stop_outcome_halts_dispatch_and_only_drains_what_is_in_flight():
    """Parallel stop semantics, stated as a BOUND rather than an exact count.

    In flight rows cannot be un-started, so a stop can still complete up to
    `concurrency - 1` extra rows. What must hold is that dispatch stops: the
    remaining rows are never attempted. With 10 rows at concurrency 2 and a
    stop on row 0, anything approaching 10 would mean the stop did nothing.
    """
    settings.batch_add_concurrency = 2
    outcomes = {"A": AddVenueOutcome(429, {"detail": "Monthly venue quota exhausted"})}
    names = ["A"] + [f"R{i}" for i in range(9)]
    for n in names[1:]:
        outcomes[n] = AddVenueOutcome(201, {"status": "created", "venue_id": f"v{n}"})
    handler = _Handler(outcomes)
    google = _Google({n: (-9.6, -35.7) for n in names})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": n, "venue_address": n.lower()} for n in names
    ])
    job = await _run_to_completion(svc, req)
    assert job["status"] == "stopped"
    assert "quota_exhausted" in job["stopped_reason"]
    # The bound: the stopping row plus at most (concurrency - 1) already in
    # flight. Not 10 — that would mean dispatch never stopped.
    assert job["processed"] <= 2
    assert len(handler.calls) <= 2


@pytest.mark.asyncio
async def test_duplicate_rows_in_one_batch_are_serialized_so_only_one_is_bought():
    """The money bug parallelism would otherwise introduce.

    Sequential execution protected duplicates only by accident of ordering:
    row 1's add made the venue cache-visible before row 2 read the cache. Run
    them concurrently with no lock and both miss `_already_active_id`, both
    reach the handler, and both pay a BestTime credit to create the same venue
    twice.

    The module's shared `_Handler` has a static `cached` map, so it cannot
    express "a created venue becomes visible to the next lookup" — the exact
    behaviour under test. This local subclass adds only that, mirroring what
    the real handler does via `_save_address_cache`.

    Rows are byte-identical on purpose. The dispatch lock also folds case, but
    whether the REAL address cache normalizes case is a property of
    `_lookup_cached_venue_id`, not of this change, so asserting a
    different-case guarantee here would be claiming something this test does
    not establish.
    """
    settings.batch_add_concurrency = 4

    class _CachingHandler(_Handler):
        async def add(self, request, *, coordinates_trusted=True):
            outcome = await super().add(
                request, coordinates_trusted=coordinates_trusted
            )
            # The await is load bearing, not cosmetic. A fake that records the
            # call and writes the cache without ever yielding cannot overlap
            # two rows, so the test would pass with NO lock at all — verified
            # by mutation. This yield opens the exact window the per-key lock
            # exists to close.
            await asyncio.sleep(0.02)
            vid = (outcome.body or {}).get("venue_id")
            if vid:
                self._cached[request.venue_name] = vid
                self.venue_dao.venues[vid] = _Venue(active=True)
            return outcome

    handler = _CachingHandler({
        "Dup": AddVenueOutcome(201, {"status": "created", "venue_id": "vDup"}),
    })
    google = _Google({"Dup": (-9.6, -35.7)})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": "Dup", "venue_address": "same place"},
        {"venue_name": "Dup", "venue_address": "same place"},
        {"venue_name": "Dup", "venue_address": "same place"},
    ])
    job = await _run_to_completion(svc, req)
    assert job["status"] == "done"
    # Exactly one paid add for three identical rows; the other two short
    # circuit on the address-hash hit the first one created.
    assert handler.calls.count("Dup") == 1
    assert job["summary"].get("created") == 1
    assert job["summary"].get("already_exists") == 2


@pytest.mark.asyncio
async def test_results_are_reported_in_submission_order_not_completion_order():
    """Workers finish out of order; the report must stay diffable against the
    input list."""
    settings.batch_add_concurrency = 8
    names = [f"V{i}" for i in range(8)]

    class _SkewedHandler(_Handler):
        """Row 0 is the slowest, row 7 the fastest, so completion order is the
        REVERSE of submission order. Without the skew every row completes in
        dispatch order and the sort is unobservable — the test would pass with
        the sort deleted, verified by mutation."""
        async def add(self, request, *, coordinates_trusted=True):
            await asyncio.sleep(0.02 * (8 - int(request.venue_name[1:])))
            return await super().add(
                request, coordinates_trusted=coordinates_trusted
            )

    handler = _SkewedHandler({
        n: AddVenueOutcome(201, {"status": "created", "venue_id": f"v{n}"})
        for n in names
    })
    google = _Google({n: (-9.6, -35.7) for n in names})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": n, "venue_address": n.lower()} for n in names
    ])
    job = await _run_to_completion(svc, req)
    assert job["status"] == "done"
    assert [r["index"] for r in job["results"]] == list(range(8))
    assert [r["venue_name"] for r in job["results"]] == names
    assert job["processed"] == 8
    # The premise: they really did finish out of order. If this ever fails the
    # test above has stopped proving anything.
    assert handler.calls != names


@pytest.mark.asyncio
async def test_concurrency_one_is_exactly_the_sequential_path():
    """The way back. Same inputs, same outcomes, strict submission order."""
    settings.batch_add_concurrency = 1
    names = [f"V{i}" for i in range(5)]
    handler = _Handler({
        n: AddVenueOutcome(201, {"status": "created", "venue_id": f"v{n}"})
        for n in names
    })
    google = _Google({n: (-9.6, -35.7) for n in names})
    svc = _service(handler, google)
    req = BatchAddRequest(venues=[
        {"venue_name": n, "venue_address": n.lower()} for n in names
    ])
    job = await _run_to_completion(svc, req)
    assert handler.calls == names          # strict order, one at a time
    assert job["processed"] == 5
    assert job["summary"] == {"created": 5}
