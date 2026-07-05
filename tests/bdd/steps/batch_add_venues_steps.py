"""Behave steps for tests/bdd/api/batch-add-venues.feature.

Drives the real batch-add endpoints (wired in environment.py against the
in-memory container) over the real BatchAddService + AddVenueHandler. Rows carry
inline coordinates so no Google network call is needed; BestTime is the
programmable harness stub.
"""
from __future__ import annotations

import time

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import NewVenueResponse


@given("BestTime accepts every add and returns a created venue")
def step_besttime_accepts(context):
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate({
        "status": "OK",
        "venue_info": {
            "venue_id": "ven_batch_created_001",
            "venue_lat": -12.9714,
            "venue_lon": -38.5014,
        },
    })


@when("the operator submits a batch of 2 venues with coordinates")
def step_submit_batch(context):
    body = {
        "label": "bdd-batch",
        "resolve_coords": False,
        "venues": [
            {"venue_name": "Bar One", "venue_address": "Rua Um, 1 - Centro",
             "venue_lat": -12.9714, "venue_lng": -38.5014},
            {"venue_name": "Bar Two", "venue_address": "Rua Dois, 2 - Centro",
             "venue_lat": -12.9800, "venue_lng": -38.5100},
        ],
    }
    context.batch_post = context.client.post("/admin/venues/batch-add", json=body)
    if context.batch_post.status_code == 202:
        context.batch_job_id = context.batch_post.json()["job_id"]


@then("the batch endpoint returns 202 with a job id")
def step_batch_accepted(context):
    assert context.batch_post.status_code == 202, context.batch_post.text
    body = context.batch_post.json()
    assert body.get("job_id"), body
    assert body.get("total") == 2, body
    assert body.get("status") == "running", body


@then('polling the job eventually reports status "done"')
def step_poll_until_done(context):
    last = None
    for _ in range(200):
        resp = context.client.get(f"/admin/venues/batch-add/{context.batch_job_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["status"] in ("done", "stopped", "failed"):
            break
        time.sleep(0.02)
    assert last is not None and last["status"] == "done", last
    context.batch_job = last


@then("the job summary reports {created:d} created and {processed:d} processed")
def step_summary(context, created, processed):
    job = context.batch_job
    assert job["processed"] == processed, job
    assert job["summary"].get("created", 0) == created, job["summary"]


@when("the operator polls a batch job id that does not exist")
def step_poll_unknown(context):
    context.batch_unknown = context.client.get(
        "/admin/venues/batch-add/does-not-exist"
    )


@then("the batch poll returns 404")
def step_poll_404(context):
    assert context.batch_unknown.status_code == 404, context.batch_unknown.text
