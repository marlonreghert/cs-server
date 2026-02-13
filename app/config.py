"""Configuration management using Pydantic BaseSettings with JSON file support."""
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def flatten_json_config(config: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested JSON config into flat key-value pairs.

    Supports nested structures like:
    {
        "redis": {"redis_host": "localhost", "redis_port": 6379},
        "server": {"server_port": 8080}
    }

    Becomes:
    {"redis_host": "localhost", "redis_port": 6379, "server_port": 8080}

    Keys starting with "_" (like "_comment") are skipped.
    """
    result = {}

    for key, value in config.items():
        # Skip comment keys
        if key.startswith("_"):
            continue

        if isinstance(value, dict):
            # Recursively flatten nested dicts
            nested = flatten_json_config(value)
            result.update(nested)
        else:
            result[key] = value

    return result


def load_json_config(config_file: Optional[str] = None) -> dict[str, Any]:
    """Load configuration from a JSON file.

    Supports both flat and nested JSON structures. Nested structures are
    automatically flattened. Keys starting with "_" are treated as comments
    and ignored.

    Args:
        config_file: Path to JSON config file. If None, checks CONFIG_FILE env var.

    Returns:
        Dictionary of configuration values (flattened), or empty dict if no file found.
    """
    file_path = config_file or os.getenv("CONFIG_FILE")

    if not file_path:
        return {}

    path = Path(file_path)
    if not path.exists():
        logger.warning(f"Config file not found: {file_path}")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
            logger.info(f"Loaded configuration from: {file_path}")
            # Flatten nested structure
            return flatten_json_config(config)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config file {file_path}: {e}")
        return {}
    except Exception as e:
        logger.error(f"Error reading config file {file_path}: {e}")
        return {}


class Settings(BaseSettings):
    """Application configuration with JSON file and environment variable support.

    Configuration priority (highest to lowest):
    1. Environment variables
    2. JSON config file (specified via CONFIG_FILE env var)
    3. Default values
    """

    # Redis Configuration
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    # Venues Refresher Configuration
    # 43200 minutes = 30 days
    venues_catalog_refresh_minutes: int = 43200
    venues_live_refresh_minutes: int = 5
    weekly_forecast_cron: str = "0 0 * * 0"  # Sundays at 00:00

    # BestTime API Configuration
    besttime_private_key: str = "pri_aff50a71a038456db88864b16d9d6800"
    besttime_public_key: str = "pub_4f4f184e1a5f4f50a48e945fde7ab2ea"
    besttime_endpoint_base_v1: str = "https://besttime.app/api/v1"
    besttime_search_polling_wait_seconds: int = 15

    # Google Places API Configuration
    # Enrichment includes: vibe attributes, business status checks, permanently closed detection
    google_places_api_key: str = ""
    google_places_enrichment_enabled: bool = False  # Disabled by default
    google_places_enrichment_cron: str = "0 3 * * *"  # Daily at 3 AM
    google_places_enrichment_on_startup: bool = False  # If True, run enrichment on startup

    # Permanently closed venue removal (uses Google Places API businessStatus)
    # When enabled, venues marked as CLOSED_PERMANENTLY by Google are removed from the database
    # This runs during the google_places_enrichment refresh
    remove_permanently_closed_venues: bool = True  # Enabled by default when enrichment runs

    # Temporarily closed venue removal (uses Google Places API businessStatus)
    # When enabled, venues marked as CLOSED_TEMPORARILY by Google are removed from the database
    # This runs during the google_places_enrichment refresh
    remove_temporarily_closed_venues: bool = True  # Enabled by default when enrichment runs

    # Photo enrichment configuration (uses Google Places API)
    photo_enrichment_enabled: bool = False  # Disabled by default, set PHOTO_ENRICHMENT_ENABLED=true to enable
    photo_enrichment_on_startup: bool = False  # If True, fetch photos on startup
    photo_enrichment_limit: int = 20  # Max venues to enrich with photos per refresh cycle (to control API costs)
    photos_per_venue: int = 5  # Number of photos to fetch per venue

    # Instagram Discovery (Apify) Configuration
    apify_api_token: str = ""
    instagram_enrichment_enabled: bool = False
    instagram_enrichment_cron: str = "0 4 * * 1"  # Weekly: Monday at 4 AM
    instagram_enrichment_on_startup: bool = False
    instagram_min_confidence: float = 0.50
    instagram_auto_accept_threshold: float = 0.75
    instagram_search_candidates: int = 3
    instagram_enrichment_limit: int = 0  # Max venues per run (0 = unlimited)
    instagram_cache_ttl_days: int = 30
    instagram_not_found_cache_ttl_days: int = 7

    # Server Configuration
    server_port: int = 8080
    log_level: str = "INFO"

    # Startup Configuration
    # If False, skip initial venue refresh on startup (only schedule jobs)
    refresh_on_startup: bool = True
    # If set (> 0), overrides the limit for each location when fetching venues
    venue_limit_override: int = 0
    # Global cap on total venues fetched across all locations (-1 = disabled, 0 = fetch none)
    venue_total_limit: int = -1

    # Project Paths
    project_root: str = ""
    resources_path_prefix: str = "resources"

    # Resource Files
    search_venue_response_resource: str = "search_venues_response.json"
    venue_static_resource: str = "venue_static.json"
    search_progress_response_resource: str = "search_progress_response.json"
    live_forecast_response_resource: str = "live_forecast_response.json"
    venue_filter_response_resource: str = "venue_filter_response.json"
    venues_ids_resource: str = "static_venues_ids.json"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    def __init__(self, **kwargs):
        """Initialize settings from JSON file and environment variables.

        Priority: env vars > JSON config > defaults
        """
        # Load JSON config first (if CONFIG_FILE is set)
        json_config = load_json_config()

        # Merge: kwargs override JSON config
        merged_kwargs = {**json_config, **kwargs}

        super().__init__(**merged_kwargs)

        if not self.project_root:
            # Use PROJECT_ROOT env var or current working directory
            self.project_root = os.getenv("PROJECT_ROOT", os.getcwd())

    @property
    def base_dir(self) -> Path:
        """Get the project root directory as a Path object."""
        return Path(self.project_root)

    def get_resource_path(self, resource_file: str) -> Path:
        """Get the full path to a resource file."""
        return self.base_dir / self.resources_path_prefix / resource_file

    @property
    def redis_address(self) -> str:
        """Get Redis connection address in host:port format."""
        return f"{self.redis_host}:{self.redis_port}"


# Global settings instance
settings = Settings()
