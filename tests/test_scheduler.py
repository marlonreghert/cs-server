"""Pins `weekly_forecast_cron`'s CURRENT (APScheduler-native, not standard
Unix cron) day-of-week behavior — found while building
plans/260809_scheduled-incremental-instagram-crawl.md's crawl-target
scheduler, and deliberately left unchanged there (see the NOTE at
`register_refresh_jobs`'s Job 3 registration in main.py and the comment on
`Settings.weekly_forecast_cron`).

`main.py` registers this job via bare `CronTrigger.from_crontab`, whose
day-of-week field is APScheduler's own 0=Monday..6=Sunday, NOT standard
cron's 0=Sunday..6=Saturday. `weekly_forecast_cron`'s default, "0 0 * * 0",
is commented "Sundays at 00:00" but actually fires MONDAY 00:00. This test
exists so that divergence is visible and intentional to whoever next touches
this site, rather than a trap — it protects the CURRENT behavior, not the
comment's claim about it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger

from app.config import Settings


def test_weekly_forecast_cron_currently_fires_monday_not_sunday_apscheduler_native_quirk():
    settings = Settings()
    trigger = CronTrigger.from_crontab(settings.weekly_forecast_cron, timezone="America/Recife")

    # A Monday, squarely inside Monday in America/Recife too (see
    # test_instagram_crawl_service.py::TestBuildCronTriggerWeekday for why
    # a midnight-UTC anchor is NOT safe here — it lands on Sunday evening in
    # Recife and would match the wrong occurrence).
    after = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    fire = trigger.get_next_fire_time(None, after)

    # The comment says "Sundays at 00:00". The ACTUAL fire day, under
    # APScheduler's own from_crontab day-of-week numbering, is Monday.
    assert fire.strftime("%A") == "Monday", (
        f"weekly_forecast_cron fired on {fire.strftime('%A')}, not Monday — "
        "if this now fails, either APScheduler's from_crontab semantics "
        "changed, or someone routed this site through the day-of-week-safe "
        "build_cron_trigger helper (a deliberate, operator-approved change, "
        "not an accident this test should silently swallow)"
    )
