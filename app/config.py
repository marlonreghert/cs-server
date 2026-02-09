"""Configuration management using Pydantic BaseSettings."""
import os
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration with environment variable support."""

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

    # Google Places API Configuration (for vibe attributes)
    google_places_api_key: str = ""
    vibe_attributes_refresh_enabled: bool = False  # Disabled by default
    vibe_attributes_refresh_cron: str = "0 3 * * *"  # Daily at 3 AM
    vibe_attributes_refresh_on_startup: bool = False  # If True, refresh vibe attributes on startup

    # Server Configuration
    server_port: int = 8080
    log_level: str = "INFO"

    # Startup Configuration
    # If False, skip initial venue refresh on startup (only schedule jobs)
    refresh_on_startup: bool = True
    # If set (> 0), overrides the limit for each location when fetching venues
    venue_limit_override: int = 0

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
        """Initialize settings and set project_root if not provided."""
        super().__init__(**kwargs)
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
