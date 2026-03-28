"""Routers package."""
from app.routers.venue_router import router as venue_router, set_venue_handler
from app.routers.debug_router import router as debug_router, set_debug_dependencies
from app.routers.admin_trigger_router import router as admin_trigger_router, set_container as set_admin_container

__all__ = [
    "venue_router", "set_venue_handler",
    "debug_router", "set_debug_dependencies",
    "admin_trigger_router", "set_admin_container",
]
