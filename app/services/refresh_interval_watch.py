"""Admin-tunable live forecast refresh interval.

A lightweight watcher job reads `admin_config:live_refresh_minutes` from
Redis (written by the vibesadmin panel, mirroring the
`admin_config:venue_photos_cache_ttl_days` pattern) and reschedules the
`live_forecast_refresh` job on the running scheduler when the effective
value changes. Absent key -> settings default; invalid value -> keep the
current schedule. Bad input can never stall or kill the refresh.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from apscheduler.triggers.interval import IntervalTrigger

from app.metrics import (
    BACKGROUND_JOB_DURATION_SECONDS,
    BACKGROUND_JOB_LAST_RUN_TIMESTAMP,
    BACKGROUND_JOB_RUNS_TOTAL,
    LIVE_REFRESH_INTERVAL_MINUTES,
)

logger = logging.getLogger(__name__)

ADMIN_LIVE_REFRESH_MINUTES_KEY = "admin_config:live_refresh_minutes"
MIN_REFRESH_MINUTES = 1
MAX_REFRESH_MINUTES = 120
LIVE_REFRESH_JOB_ID = "live_forecast_refresh"
WATCH_JOB_NAME = "refresh_interval_watch"
WATCH_INTERVAL_SECONDS = 60


class RefreshIntervalWatcher:
    """Applies the admin-configured live refresh interval to the scheduler.

    `redis_client` needs only `.get(key)` (GeoRedisClient and raw
    redis-py/fakeredis clients all qualify). `scheduler` needs only
    `.reschedule_job(job_id, trigger=...)`.
    """

    def __init__(self, redis_client, scheduler, default_minutes: int):
        self._redis = redis_client
        self._scheduler = scheduler
        self._default = int(default_minutes)
        self._applied = int(default_minutes)
        # Warn once per distinct rejected raw value, not on every tick.
        self._last_rejected: Optional[str] = None
        LIVE_REFRESH_INTERVAL_MINUTES.set(self._applied)

    @property
    def applied_minutes(self) -> int:
        return self._applied

    def _parse_minutes(self, raw: str) -> Optional[int]:
        """A valid value is a JSON integer within [MIN, MAX] — bare or
        wrapped as {"minutes": N} (the shape the vibesadmin → cs-server
        config proxy stores); else None."""
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if isinstance(value, dict):
            value = value.get("minutes")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if not MIN_REFRESH_MINUTES <= value <= MAX_REFRESH_MINUTES:
            return None
        return value

    def check_once(self) -> None:
        """Read the admin key and reschedule when the effective value changed.

        Raises on Redis/scheduler errors (the caller counts them); invalid
        values are handled here and never raise.
        """
        raw = self._redis.get(ADMIN_LIVE_REFRESH_MINUTES_KEY)
        if raw is None:
            effective, source = self._default, "default"
            self._last_rejected = None
        else:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            parsed = self._parse_minutes(raw)
            if parsed is None:
                if raw != self._last_rejected:
                    logger.warning(
                        f"[{WATCH_JOB_NAME}] Rejected "
                        f"{ADMIN_LIVE_REFRESH_MINUTES_KEY}={raw!r} "
                        f"(need integer in [{MIN_REFRESH_MINUTES}, "
                        f"{MAX_REFRESH_MINUTES}]); keeping "
                        f"{self._applied} minutes"
                    )
                    self._last_rejected = raw
                return
            effective, source = parsed, "admin"
            self._last_rejected = None

        if effective == self._applied:
            return

        self._scheduler.reschedule_job(
            LIVE_REFRESH_JOB_ID, trigger=IntervalTrigger(minutes=effective)
        )
        logger.info(
            f"[{WATCH_JOB_NAME}] {LIVE_REFRESH_JOB_ID} interval changed: "
            f"{self._applied} -> {effective} minutes (source={source})"
        )
        self._applied = effective
        LIVE_REFRESH_INTERVAL_MINUTES.set(effective)

    async def run(self) -> None:
        """Scheduled entry point with the standard background-job metrics."""
        start_time = time.perf_counter()
        try:
            self.check_once()
            duration = time.perf_counter() - start_time
            BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=WATCH_JOB_NAME).observe(
                duration
            )
            BACKGROUND_JOB_RUNS_TOTAL.labels(
                job_name=WATCH_JOB_NAME, status="success"
            ).inc()
            BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(
                job_name=WATCH_JOB_NAME
            ).set_to_current_time()
        except Exception as e:
            duration = time.perf_counter() - start_time
            BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=WATCH_JOB_NAME).observe(
                duration
            )
            BACKGROUND_JOB_RUNS_TOTAL.labels(
                job_name=WATCH_JOB_NAME, status="error"
            ).inc()
            logger.error(
                f"[{WATCH_JOB_NAME}] watch cycle failed after "
                f"{duration:.3f}s; keeping {self._applied} minutes: {e}"
            )
