"""One-shot probe script for the BestTime add-venue contract.

Runs the probes documented in
plans/add_venue_by_address_26_05_26.md (Pre-Implementation Verification).

Probes:
    A  Idempotent re-add of a known inventory venue using a Google Places
       formatted_address.
    B  Deliberately fake address; expected geocoder rejection.
    C  /venues/filter with a tight radius around the Probe A venue.
    D  Real fresh venue not in inventory. Spends one of the +500 monthly
       slots. Requires interactive confirmation. After the fresh create,
       immediately re-calls /forecasts on the same venue to capture the
       follow-up idempotent shape.

Captured fixtures are written to tests/fixtures/besttime/ with
api_key_private redacted.

Usage:
    BESTTIME_KEY=... GOOGLE_PLACES_KEY=... \\
        python scripts/probe_besttime_forecasts.py \\
            --probe a            # A only (cheap)
            --probe b            # B only (cheap)
            --probe c            # C only (cheap)
            --probe abc          # A + B + C (default for unattended runs)
            --probe d            # D only (one-shot, expensive)

The script never re-runs Probe D unattended; it always prompts.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "besttime"
REDACTED = "<redacted>"

BESTTIME_BASE = "https://besttime.app/api/v1"
GOOGLE_PLACES_BASE = "https://places.googleapis.com/v1"

# Probe A target venue (chain store, low-stakes, already in inventory)
PROBE_A_NAME = "Casas Bahia"
PROBE_A_INVENTORY_ADDRESS_SUBSTR = "Agamenon Magalhães 153"
PROBE_A_LAT = -8.0419949
PROBE_A_LNG = -34.8823209

# Probe B fake-address constants
PROBE_B_NAME = "Bar Inexistente Que Nao Existe Em Lugar Algum"
PROBE_B_ADDRESS = "Rua Inventada 99999, Cidade Que Nao Existe, ZZ 00000-000 Brazil"

# Probe C radius (meters)
PROBE_C_RADIUS_M = 200


def _redact_params(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    for k in ("api_key_private", "api_key_public"):
        if k in out:
            out[k] = REDACTED
    return out


def _record(
    name: str,
    *,
    method: str,
    url: str,
    request_params: dict[str, Any],
    request_body: Any,
    response: httpx.Response,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "probe": name,
        "request": {
            "method": method,
            "url": url,
            "params": _redact_params(request_params),
            "body": request_body,
        },
        "response": {
            "status_code": response.status_code,
            "headers": {k: v for k, v in response.headers.items()},
        },
    }
    try:
        record["response"]["body"] = response.json()
    except Exception:
        record["response"]["body_text"] = response.text
    return record


def _write_fixture(filename: str, payload: Any) -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    target = FIXTURES_DIR / filename
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return target


def google_places_formatted_address(
    google_key: str,
    venue_name: str,
    lat: float,
    lng: float,
    radius_m: float = 500.0,
) -> tuple[str, dict[str, Any]]:
    """Fetch the canonical Google Places formatted_address for a venue.

    Returns (formatted_address, raw_first_place_dict).
    """
    url = f"{GOOGLE_PLACES_BASE}/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": google_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.location",
    }
    body = {
        "textQuery": venue_name,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius_m,
            }
        },
        "maxResultCount": 1,
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    places = data.get("places", [])
    if not places:
        raise RuntimeError(f"Google Places returned no candidates for {venue_name!r}")
    place = places[0]
    addr = place.get("formattedAddress")
    if not addr:
        raise RuntimeError("Google Places returned a place without formattedAddress")
    return addr, place


def probe_a(besttime_key: str, google_key: str) -> dict[str, Any]:
    print("[Probe A] Resolving Google Places formatted_address for Probe A target...")
    formatted_address, place = google_places_formatted_address(
        google_key, PROBE_A_NAME, PROBE_A_LAT, PROBE_A_LNG
    )
    print(f"[Probe A] Google formatted_address: {formatted_address}")

    url = f"{BESTTIME_BASE}/forecasts"
    params = {
        "api_key_private": besttime_key,
        "venue_name": PROBE_A_NAME,
        "venue_address": formatted_address,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, params=params)
    record = _record(
        "A_idempotent_readd_with_google_formatted_address",
        method="POST",
        url=url,
        request_params=params,
        request_body=None,
        response=resp,
    )
    record["context"] = {
        "google_places_match": {
            "id": place.get("id"),
            "displayName": place.get("displayName"),
            "formattedAddress": place.get("formattedAddress"),
            "location": place.get("location"),
        },
        "expected_outcome": "status=OK, venue_info.venue_id == existing inventory id",
    }
    return record


def probe_b(besttime_key: str) -> dict[str, Any]:
    url = f"{BESTTIME_BASE}/forecasts"
    params = {
        "api_key_private": besttime_key,
        "venue_name": PROBE_B_NAME,
        "venue_address": PROBE_B_ADDRESS,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, params=params)
    record = _record(
        "B_deliberate_fake_address",
        method="POST",
        url=url,
        request_params=params,
        request_body=None,
        response=resp,
    )
    record["context"] = {
        "expected_outcome": "HTTP 400 or 200 with status=Error and no venue_info.venue_id",
    }
    return record


def probe_c(besttime_key: str) -> dict[str, Any]:
    url = f"{BESTTIME_BASE}/venues/filter"
    params = {
        "api_key_private": besttime_key,
        "lat": PROBE_A_LAT,
        "lng": PROBE_A_LNG,
        "radius": PROBE_C_RADIUS_M,
        "foot_traffic": "both",
        "limit": 25,
        "busy_min": 0,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, params=params)
    record = _record(
        "C_venues_filter_radius_200m",
        method="GET",
        url=url,
        request_params=params,
        request_body=None,
        response=resp,
    )
    record["context"] = {
        "expected_outcome": (
            "Response includes the Probe A inventory venue (proves geo "
            "fallback can match an existing inventory venue from "
            "coordinates alone)."
        ),
    }
    return record


def _venue_in_inventory(besttime_key: str, name: str, address_substr: str) -> bool:
    """Cheap pre-check: list inventory and search for name + address substring."""
    url = f"{BESTTIME_BASE}/venues"
    with httpx.Client(timeout=60.0) as client:
        page = 0
        while True:
            resp = client.get(
                url,
                params={
                    "api_key_private": besttime_key,
                    "limit": 1000,
                    "page": page,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return False
            for v in data:
                if (
                    v.get("venue_name") == name
                    and address_substr in (v.get("venue_address") or "")
                ):
                    return True
            if len(data) < 1000:
                return False
            page += 1


def probe_d(besttime_key: str, google_key: str) -> list[dict[str, Any]]:
    print()
    print("=" * 70)
    print("PROBE D — fresh BestTime venue creation")
    print("=" * 70)
    print(
        "This probe spends 1 of our +500 monthly venue slots. Run only "
        "ONCE, supervised, and never in CI."
    )
    print()
    print("You must supply:")
    print("  1. The venue NAME you want to add (we actually want it)")
    print("  2. An approximate lat,lng so we can resolve the Google Places")
    print("     formatted_address with a tight location bias.")
    print()
    venue_name = input("Venue name (e.g., 'Empório Pernambuco'): ").strip()
    if not venue_name:
        print("[Probe D] aborted: empty name")
        sys.exit(1)
    lat_str = input("Approx lat (e.g., -8.05): ").strip()
    lng_str = input("Approx lng (e.g., -34.88): ").strip()
    try:
        lat = float(lat_str)
        lng = float(lng_str)
    except ValueError:
        print("[Probe D] aborted: lat/lng not numeric")
        sys.exit(1)

    print("[Probe D] Resolving Google Places formatted_address...")
    formatted_address, place = google_places_formatted_address(
        google_key, venue_name, lat, lng
    )
    print(f"[Probe D] Google match: name={place.get('displayName')}")
    print(f"[Probe D] formatted_address: {formatted_address}")
    print(f"[Probe D] location: {place.get('location')}")
    print()
    print("[Probe D] Checking BestTime inventory to ensure this is NOT a")
    print("[Probe D] re-add (which would not exercise the fresh-create path)...")
    addr_substr_for_check = formatted_address.split(",")[0].strip()
    if _venue_in_inventory(besttime_key, venue_name, addr_substr_for_check):
        print(
            f"[Probe D] aborted: a venue named {venue_name!r} with address "
            f"substring {addr_substr_for_check!r} already exists in inventory. "
            "Pick a different venue."
        )
        sys.exit(1)

    print()
    print("=" * 70)
    print("FINAL CONFIRMATION")
    print("=" * 70)
    print(f"  venue_name:    {venue_name}")
    print(f"  venue_address: {formatted_address}")
    print()
    print("This call WILL spend 1 of our 500 monthly venue slots.")
    confirm = input('Type "I CONFIRM" to proceed: ').strip()
    if confirm != "I CONFIRM":
        print("[Probe D] aborted: confirmation phrase not entered exactly")
        sys.exit(1)

    url = f"{BESTTIME_BASE}/forecasts"
    params = {
        "api_key_private": besttime_key,
        "venue_name": venue_name,
        "venue_address": formatted_address,
    }
    with httpx.Client(timeout=60.0) as client:
        print("[Probe D] Calling POST /forecasts (fresh create)...")
        resp = client.post(url, params=params)
    fresh_record = _record(
        "D_fresh_create",
        method="POST",
        url=url,
        request_params=params,
        request_body=None,
        response=resp,
    )
    fresh_record["context"] = {
        "google_places_match": {
            "id": place.get("id"),
            "displayName": place.get("displayName"),
            "formattedAddress": place.get("formattedAddress"),
            "location": place.get("location"),
        },
        "expected_outcome": (
            "status=OK with a populated venue_info.venue_id. analysis array "
            "may be partial or pending on first create."
        ),
    }

    if resp.status_code >= 400 or (
        isinstance(fresh_record["response"].get("body"), dict)
        and fresh_record["response"]["body"].get("status") != "OK"
    ):
        print(
            "[Probe D] BestTime did NOT return success — stopping without "
            "the immediate re-read. Surface this body to the user."
        )
        return [fresh_record]

    print("[Probe D] Fresh create succeeded. Now re-calling for idempotent shape...")
    with httpx.Client(timeout=30.0) as client:
        resp2 = client.post(url, params=params)
    reread_record = _record(
        "D_fresh_then_reread",
        method="POST",
        url=url,
        request_params=params,
        request_body=None,
        response=resp2,
    )
    reread_record["context"] = {
        "expected_outcome": (
            "Same status=OK and same venue_id as the fresh create. Compare "
            "structures to lock down whether fresh vs re-add diverge."
        ),
    }
    return [fresh_record, reread_record]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe",
        default="abc",
        help="Which probes to run: a, b, c, d, or any combination (default: abc)",
    )
    args = parser.parse_args()
    selected = args.probe.lower()

    besttime_key = os.environ.get("BESTTIME_KEY")
    google_key = os.environ.get("GOOGLE_PLACES_KEY")
    if not besttime_key:
        print("ERROR: BESTTIME_KEY env var is required", file=sys.stderr)
        return 2
    needs_google = "a" in selected or "d" in selected
    if needs_google and not google_key:
        print("ERROR: GOOGLE_PLACES_KEY env var is required for probes a/d", file=sys.stderr)
        return 2

    print(f"[probe] Selected: {selected.upper()}")
    print(f"[probe] Fixtures dir: {FIXTURES_DIR}")

    if "a" in selected:
        rec = probe_a(besttime_key, google_key)
        path = _write_fixture("forecasts_post_known_ok.json", rec)
        print(f"[Probe A] wrote {path.relative_to(REPO_ROOT)}  status={rec['response']['status_code']}")
    if "b" in selected:
        rec = probe_b(besttime_key)
        path = _write_fixture("forecasts_post_unknown_error.json", rec)
        print(f"[Probe B] wrote {path.relative_to(REPO_ROOT)}  status={rec['response']['status_code']}")
    if "c" in selected:
        rec = probe_c(besttime_key)
        path = _write_fixture("venues_filter_radius_200m.json", rec)
        print(f"[Probe C] wrote {path.relative_to(REPO_ROOT)}  status={rec['response']['status_code']}")
    if "d" in selected:
        recs = probe_d(besttime_key, google_key)
        path1 = _write_fixture("forecasts_post_fresh_create_ok.json", recs[0])
        print(f"[Probe D] wrote {path1.relative_to(REPO_ROOT)}  status={recs[0]['response']['status_code']}")
        if len(recs) > 1:
            path2 = _write_fixture("forecasts_post_fresh_then_reread_ok.json", recs[1])
            print(f"[Probe D] wrote {path2.relative_to(REPO_ROOT)}  status={recs[1]['response']['status_code']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
