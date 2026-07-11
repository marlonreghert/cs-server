"""Unit tests for the server-side batch venue-add service."""
import asyncio

import fakeredis
import pytest

from app.handlers.add_venue_handler import AddVenueOutcome
from app.models.batch_add import BatchAddRequest
import app.services.batch_add_service as bas
from app.services.batch_add_service import BatchAddService, _classify


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
    `dao_venues` maps venue_id -> _Venue for the active check."""
    def __init__(self, script, cached=None, dao_venues=None):
        self.script = script
        self.calls = []
        self._cached = cached or {}
        self.venue_dao = _Dao(dao_venues or {})

    def _lookup_cached_venue_id(self, name, address):
        return self._cached.get(name)

    async def add(self, request):
        self.calls.append(request.venue_name)
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
        redis_client=fakeredis.FakeStrictRedis(decode_responses=False),
        google_client=google,
        budget_service=budget or _Budget(),
    )


async def _run_to_completion(svc, req):
    accepted = svc.start_job(req)
    job_id = accepted["job_id"]
    # Drain the background task the service scheduled.
    for _ in range(200):
        task = svc._tasks.get(job_id)
        if task is None:
            break
        await asyncio.sleep(0)
        if task.done():
            break
    # ensure any trailing awaits settle
    await asyncio.sleep(0)
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
    for _ in range(200):
        task = svc._tasks.get(first["job_id"])
        if task is None or task.done():
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert job_lock.is_running(bas.BATCH_ADD_LOCK) is False

    # A new batch may start now that the first finished.
    third = svc.start_job(req)
    assert third["status"] == "running"
