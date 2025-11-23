#!/usr/bin/env python3
"""
BestTime API crawler script to fetch venues and their live forecast status.

This script provides two functions:
1. get_available_venues() - Fetches venues near a location using /venues/filter endpoint
2. get_venue_status() - Fetches live forecast status for each venue using /forecasts/live endpoint

Location: Recife, Brazil (-8.060090, -34.889501)
"""

import requests
import json
import time
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
from urllib.parse import urlencode
import pandas as pd

# Configuration
BESTTIME_ENDPOINT_BASE_V1 = "https://besttime.app/api/v1"
BESTTIME_PRIVATE_KEY = "pri_aff50a71a038456db88864b16d9d6800"
BESTTIME_PUBLIC_KEY = "pub_4f4f184e1a5f4f50a48e945fde7ab2ea"

# Location: Recife, Brazil
LOCATION_LAT = -8.060090
LOCATION_LNG = -34.889501

# CSV file paths
VENUES_CSV = "besttime_venues.csv"
VENUE_STATUS_CSV = "besttime_venue_status.csv"


@dataclass
class Venue:
    """Represents a venue from BestTime API."""
    venue_id: str
    venue_name: str
    venue_address: str
    venue_lat: float
    venue_lng: float
    forecast: bool
    processed: bool


@dataclass
class VenueStatus:
    """Represents the live forecast status of a venue."""
    venue_id: str
    venue_name: str
    venue_address: str
    venue_live_busyness: Optional[int]
    venue_live_busyness_available: bool
    venue_forecasted_busyness: Optional[int]
    venue_forecast_busyness_available: bool
    venue_live_forecasted_delta: Optional[int]
    status: str


def get_available_venues() -> List[Venue]:
    """
    Fetches available venues near the configured location using BestTime API filter endpoint.
    Returns all matching venues directly without polling.
    
    Returns:
        List of Venue objects
    """
    print(f"🔍 Fetching venues near (lat={LOCATION_LAT}, lng={LOCATION_LNG})...")
    
    # Use the /venues/filter endpoint with GET request
    search_url = f"{BESTTIME_ENDPOINT_BASE_V1}/venues/filter"
    search_params = {
        "api_key_private": BESTTIME_PRIVATE_KEY,
        "lat": LOCATION_LAT,
        "lng": LOCATION_LNG,
        "radius": "10000",
        "foot_traffic": "both",
        "busy_min": "1",
        "busy_max": "100",
        "live": "true",
        "limit": "25",
    }
    
    try:
        # Construct full URL for reproducibility
        query_string = urlencode(search_params)
        full_url = f"{search_url}?{query_string}"
        print(f"📍 GET Request URL:")
        print(f"   {full_url}")
        
        response = requests.get(search_url, params=search_params)
        response.raise_for_status()
        search_result = response.json()
        
        print(f"✅ Venues fetched successfully")
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching venues: {e}")
        return []
    
    # Extract venues from response
    venues = []
    venues_data = search_result.get("venues", [])
    
    print(f"📊 Found {len(venues_data)} venues")
    
    # Extract venue data
    for venue_data in venues_data:
        venue = Venue(
            venue_id=venue_data.get("venue_id", ""),
            venue_name=venue_data.get("venue_name", ""),
            venue_address=venue_data.get("venue_address", ""),
            venue_lat=venue_data.get("venue_lat", 0.0),
            venue_lng=venue_data.get("venue_lng", 0.0),
            forecast=venue_data.get("forecast", False),
            processed=venue_data.get("processed", False),
        )
        # Only add if we have a valid venue_id
        if venue.venue_id:
            venues.append(venue)
    
    # Remove duplicates by venue_id, keeping only unique venues
    seen = {}
    for venue in venues:
        if venue.venue_id not in seen:
            seen[venue.venue_id] = venue
    
    unique_venues = list(seen.values())
    duplicates_removed = len(venues) - len(unique_venues)
    
    if duplicates_removed > 0:
        print(f"⚠️  Removed {duplicates_removed} duplicate venues")
    
    return unique_venues


def save_venues_to_csv(venues: List[Venue]) -> None:
    """
    Saves venues to a CSV file using pandas.
    
    Args:
        venues: List of Venue objects
    """
    if not venues:
        print("⚠️  No venues to save")
        return
    
    try:
        df = pd.DataFrame([asdict(v) for v in venues])
        df.to_csv(VENUES_CSV, index=False)
        print(f"💾 Saved {len(venues)} venues to {VENUES_CSV}")
    except Exception as e:
        print(f"❌ Error saving venues to CSV: {e}")


def get_venue_status(venues: List[Venue]) -> List[VenueStatus]:
    """
    Fetches live forecast status for each venue.
    
    Args:
        venues: List of Venue objects
        
    Returns:
        List of VenueStatus objects
    """
    print(f"\n🔍 Fetching live forecast status for {len(venues)} venues...")
    
    statuses = []
    success_count = 0
    error_count = 0
    
    for idx, venue in enumerate(venues, 1):
        try:
            print(f"⏳ [{idx}/{len(venues)}] Fetching status for {venue.venue_name}...", end=" ")
            
            # Build the forecast endpoint with query parameters (like Go implementation)
            forecast_url = f"{BESTTIME_ENDPOINT_BASE_V1}/forecasts/live"
            forecast_params = {
                "api_key_private": BESTTIME_PRIVATE_KEY,
                "venue_id": venue.venue_id,
            }
            
            # Print URL for debugging (first venue only to avoid spam)
            if idx == 1:
                full_url = forecast_url + "?" + urlencode(forecast_params)
                print(f"\n    DEBUG: First request URL: {full_url}")
            
            response = requests.post(forecast_url, params=forecast_params)
            response.raise_for_status()
            forecast_data = response.json()
            
            # Debug first response structure
            if idx == 1:
                print(f"    DEBUG: Response keys: {list(forecast_data.keys())}")
                print(f"    DEBUG: Full response (first venue): {json.dumps(forecast_data, indent=2)}")
            
            analysis = forecast_data.get("analysis", {})
            venue_info = forecast_data.get("venue_info", {})
            
            # Extract busyness data
            live_busyness = analysis.get("venue_live_busyness")
            forecasted_busyness = analysis.get("venue_forecasted_busyness")
            delta = analysis.get("venue_live_forecasted_delta")
            
            status = VenueStatus(
                venue_id=venue.venue_id,
                venue_name=venue.venue_name,
                venue_address=venue.venue_address,
                venue_live_busyness=live_busyness,
                venue_live_busyness_available=analysis.get("venue_live_busyness_available", False),
                venue_forecasted_busyness=forecasted_busyness,
                venue_forecast_busyness_available=analysis.get("venue_forecast_busyness_available", False),
                venue_live_forecasted_delta=delta,
                status=forecast_data.get("status", "unknown"),
            )
            statuses.append(status)
            
            # Show data retrieved
            if live_busyness is not None:
                print(f"✅ (busyness: {live_busyness})")
            else:
                print("✅ (no live data available)")
            success_count += 1
            
        except requests.exceptions.RequestException as e:
            print(f"❌ HTTP Error: {e}")
            error_count += 1
            # Still add a record with None values
            status = VenueStatus(
                venue_id=venue.venue_id,
                venue_name=venue.venue_name,
                venue_address=venue.venue_address,
                venue_live_busyness=None,
                venue_live_busyness_available=False,
                venue_forecasted_busyness=None,
                venue_forecast_busyness_available=False,
                venue_live_forecasted_delta=None,
                status="error",
            )
            statuses.append(status)
        except Exception as e:
            print(f"❌ Error: {e}")
            error_count += 1
            # Still add a record with None values
            status = VenueStatus(
                venue_id=venue.venue_id,
                venue_name=venue.venue_name,
                venue_address=venue.venue_address,
                venue_live_busyness=None,
                venue_live_busyness_available=False,
                venue_forecasted_busyness=None,
                venue_forecast_busyness_available=False,
                venue_live_forecasted_delta=None,
                status="error",
            )
            statuses.append(status)
        
        # Be respectful to the API - add a small delay between requests
        if idx < len(venues):
            time.sleep(0.5)
    
    print(f"\n📊 Status fetch complete: {success_count} successful, {error_count} failed")
    return statuses


def save_venue_status_to_csv(statuses: List[VenueStatus]) -> None:
    """
    Saves venue statuses to a CSV file using pandas with proper headers.
    Removes duplicates by venue_id.
    
    Args:
        statuses: List of VenueStatus objects
    """
    if not statuses:
        print("⚠️  No statuses to save")
        return
    
    # Remove duplicates by keeping only the last occurrence of each venue_id
    seen = {}
    for status in statuses:
        seen[status.venue_id] = status
    
    unique_statuses = list(seen.values())
    duplicates_removed = len(statuses) - len(unique_statuses)
    
    if duplicates_removed > 0:
        print(f"⚠️  Removed {duplicates_removed} duplicate entries")
    
    try:
        # Convert to pandas DataFrame with proper column ordering
        data = [asdict(status) for status in unique_statuses]
        df = pd.DataFrame(data)
        
        # Ensure proper column order
        df = df[[
            "venue_id", "venue_name", "venue_address",
            "venue_live_busyness", "venue_live_busyness_available",
            "venue_forecasted_busyness", "venue_forecast_busyness_available",
            "venue_live_forecasted_delta", "status"
        ]]
        
        # Save to CSV with proper formatting
        df.to_csv(VENUE_STATUS_CSV, index=False, encoding="utf-8")
        print(f"💾 Saved {len(unique_statuses)} unique venue statuses to {VENUE_STATUS_CSV}")
    except Exception as e:
        print(f"❌ Error saving venue statuses to CSV: {e}")


def main():
    """Main entry point for the script."""
    print("=" * 70)
    print("🚀 BestTime API Venue Crawler")
    print("=" * 70)
    
    # Step 1: Get available venues
    print("\n📍 STEP 1: Fetching Available Venues")
    print("-" * 70)
    venues = get_available_venues()
    save_venues_to_csv(venues)
    
    if not venues:
        print("❌ No venues found. Exiting.")
        return
    
    # Step 2: Get status for each venue
    print("\n📍 STEP 2: Fetching Venue Status")
    print("-" * 70)
    statuses = get_venue_status(venues)
    save_venue_status_to_csv(statuses)
    
    print("\n" + "=" * 70)
    print("✅ Crawl Complete!")
    print(f"📁 Venues saved to: {VENUES_CSV}")
    print(f"📁 Statuses saved to: {VENUE_STATUS_CSV}")
    print("=" * 70)


if __name__ == "__main__":
    main()
