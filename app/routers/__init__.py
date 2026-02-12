"""Routers package."""
from app.routers.venue_router import router as venue_router, set_venue_handler
from app.routers.debug_router import router as debug_router, set_debug_dependencies

__all__ = ["venue_router", "set_venue_handler", "debug_router", "set_debug_dependencies"]
