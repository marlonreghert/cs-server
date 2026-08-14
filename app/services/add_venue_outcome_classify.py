"""Shared AddVenueOutcome -> short outcome-label classification.

Extracted from app/services/batch_add_service.py (plans/260813_add-venue-
async-job.md) so both BatchAddService (per-row batch summaries) and
AddVenueJobService (the recent-jobs monitoring list) report outcomes using
ONE vocabulary (created, already_exists, geo_linked, quota_exhausted,
besttime_error, ...) instead of two independent copies of the same
status-code/body mapping. Pure function, no batch- or job-specific state.
"""
from __future__ import annotations

from app.handlers.add_venue_handler import AddVenueOutcome


def classify(outcome: AddVenueOutcome) -> dict:
    """Map an AddVenueOutcome to a short outcome-label result dict."""
    code = outcome.status_code
    body = outcome.body or {}
    status = body.get("status")
    res = {
        "http": code,
        "venue_id": body.get("venue_id"),
        "detail": None,
        "newly_linked": None,
        "match_reason": None,
    }
    if code == 201:
        if status == "created_google_only":
            # Catalogued from Google metadata alone (no BestTime venue, no
            # credit drawn) — a success state, not a spend-stopping one. See
            # plans/260804_add-venue-google-only.md.
            res["outcome"] = "created_google_only"
        elif body.get("recovered_from_timeout"):
            res["outcome"] = "created_recovered_timeout"
        else:
            res["outcome"] = "created"
        # Instagram discovery outcome for this row — see
        # plans/260811_add-venue-instagram-discovery.md. Always present on a
        # 201 body (AddVenueHandler._finalize_created_venue sets it).
        res["instagram"] = body.get("instagram")
    elif code == 200 and status == "already_exists":
        res["outcome"] = "already_exists"
    elif code == 200 and status == "matched_via_geo_fallback":
        res["outcome"] = "geo_linked"
        res["newly_linked"] = body.get("newly_linked")
        res["match_reason"] = body.get("match_reason")
        res["venue_id"] = body.get("venue_id")
        # Present only when newly_linked (AddVenueHandler only runs discovery
        # for a newly-linked geo-fallback outcome); None otherwise.
        res["instagram"] = body.get("instagram")
    elif code == 429:
        # Internal quota vs BestTime's own venue cap.
        res["outcome"] = (
            "besttime_monthly_cap"
            if "cap" in str(body.get("detail", "")).lower()
            else "quota_exhausted"
        )
        res["detail"] = str(body.get("detail"))
    elif code == 502:
        detail = str(body.get("detail", ""))
        if "unparseable" in detail.lower():
            res["outcome"] = "besttime_bad_response"
        elif "candidates_seen" in body or "rejected the address" in detail:
            res["outcome"] = "besttime_rejected_no_geo_match"
            res["detail"] = body.get("besttime_message") or detail
        else:
            res["outcome"] = "besttime_error"
            res["detail"] = detail
    else:
        res["outcome"] = f"http_{code}"
        res["detail"] = str(body)[:300]
    return res
