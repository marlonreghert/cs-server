"""Behave steps for tests/bdd/persistence/events-serving-projection.feature.

Reuses the SHARED RDS/Redis layer environment.py already builds per scenario
(context.rds_store, context.redis_only_dao, context.redis_projection_service,
context.fake_redis, context.client/container for the admin geo-fence route)
— the same harness redis_projection_decoupling_steps.py and
geofence_city_circles_steps.py already exercise, so "the events projection
runs" is the real RedisProjectionService.project_events(), never a shortcut.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.config import settings
from app.dao.media_archive_store import MediaArchiveStore
from app.dao.venue_media_store import VenueMediaStore
from app.metrics import EVENT_FLYER_COPY_TOTAL
from app.models import Venue
from app.models.promoter_event_visibility import ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY
from app.services.event_date_resolver import RECIFE_TZ
from app.services.event_flyer_service import EventFlyerService

_DEFAULT_VENUE = "Casa Bacurau"
_DEFAULT_LAT, _DEFAULT_LNG = -8.05, -34.88  # inside Recife's default 40km circle
_NON_SERVABLE_LAT, _NON_SERVABLE_LNG = -27.0, -49.0  # nowhere near any seeded circle
_FLYER_OUTCOMES = ("copied", "unchanged", "no_key", "archive_missing", "access_denied", "failed")


# ── settings override helper (mirrors environment.py's own _settings_overrides
#    convention exactly, so after_scenario restores these automatically) ─────
def _override_setting(context, name: str, value) -> None:
    if not hasattr(context, "_settings_overrides"):
        context._settings_overrides = {}
    if name not in context._settings_overrides:
        context._settings_overrides[name] = getattr(settings, name)
    setattr(settings, name, value)


def _parse_recife(when: str) -> datetime:
    naive = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=RECIFE_TZ)


# ── venue fixtures ────────────────────────────────────────────────────────
def _venue_id_for(context, name: str, *, lat=_DEFAULT_LAT, lng=_DEFAULT_LNG) -> str:
    if not hasattr(context, "evs_venue_ids"):
        context.evs_venue_ids = {}
    if name in context.evs_venue_ids:
        return context.evs_venue_ids[name]
    venue_id = f"evs_{uuid.uuid4().hex[:12]}"
    context.rds_store.upsert_venue(
        Venue(venue_id=venue_id, venue_name=name, venue_lat=lat, venue_lng=lng)
    )
    context.evs_venue_ids[name] = venue_id
    return venue_id


# ── event fixtures ────────────────────────────────────────────────────────
def _insert_event(context, *, venue_name=_DEFAULT_VENUE, no_venue=False, non_servable_venue=False, **fields) -> str:
    event_id = uuid.uuid4().hex  # 32 lowercase hex chars, matching event_identity.py
    if no_venue:
        venue_id = None
    elif non_servable_venue:
        venue_id = _venue_id_for(
            context, f"non-servable-{uuid.uuid4().hex[:6]}",
            lat=_NON_SERVABLE_LAT, lng=_NON_SERVABLE_LNG,
        )
    else:
        venue_id = _venue_id_for(context, venue_name)
    base = {
        "event_id": event_id, "venue_id": venue_id, "status": "accepted",
        "post_type": "event", "source_handle": "evs_handle",
        "source_shortcode": event_id, "source_permalink": f"https://instagram.com/p/{event_id}",
    }
    base.update(fields)
    context.rds_store.insert_event(base)
    context.evs_last_event_id = event_id
    return event_id


def _now(context) -> datetime:
    return getattr(context, "evs_now", datetime(2026, 9, 5, 18, 0, tzinfo=RECIFE_TZ).astimezone(timezone.utc))


# ── flyer service (lazy; only scenarios that need it wire it) ────────────
class _FakeS3Media:
    def __init__(self):
        self.puts: list[dict] = []
        self.deny = False

    def put_object(self, **kwargs):
        if self.deny:
            raise RuntimeError("AccessDenied: not authorized to perform s3:PutObject")
        self.puts.append(dict(kwargs))
        return {}


class _NoSuchKeyError(Exception):
    def __init__(self, key):
        super().__init__(f"{key} not found")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3Archive:
    def __init__(self):
        self.objects: dict[str, tuple] = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _NoSuchKeyError(Key)
        data, content_type = self.objects[Key]

        class _Body:
            def read(self_inner):
                return data
        return {"Body": _Body(), "ContentType": content_type}


def _ensure_flyer_service(context) -> EventFlyerService:
    if getattr(context, "evs_flyer_service", None) is None:
        context.evs_fake_s3_media = _FakeS3Media()
        # `no object is uploaded to the media bucket` is ALREADY a shared
        # step, owned by instagram_profile_photo_hero_steps.py, reading
        # `context.recording_s3` — mirrored here (not re-defined, which
        # behave's global step registry forbids as an exact-text collision)
        # so that pre-existing generic step asserts against THIS fixture
        # too, the same "shared-step contract" pattern
        # geofence_city_circles_steps.py's own docstring documents.
        context.recording_s3 = context.evs_fake_s3_media
        context.evs_fake_s3_archive = _FakeS3Archive()
        media_store = VenueMediaStore(
            bucket="vibesense-media-test", region="us-east-1",
            cdn_base_url="https://media.vibesense.example", s3_client=context.evs_fake_s3_media,
        )
        archive_store = MediaArchiveStore(
            bucket="vibesense-lake-test", region="us-east-1", s3_client=context.evs_fake_s3_archive,
        )
        context.evs_flyer_service = EventFlyerService(
            archive_store=archive_store, media_store=media_store, rds_store=context.rds_store,
        )
        context.redis_projection_service.event_flyer_service = context.evs_flyer_service
    return context.evs_flyer_service


def _snapshot_flyer_counters(context) -> dict:
    return {o: EVENT_FLYER_COPY_TOTAL.labels(outcome=o)._value.get() for o in _FLYER_OUTCOMES}


# ── Background ────────────────────────────────────────────────────────────
@given("the events projection is enabled")
def step_given_projection_enabled(context):
    _override_setting(context, "events_projection_enabled", True)


@given("the projection horizon is {days:d} days")
def step_given_horizon(context, days):
    _override_setting(context, "events_projection_horizon_days", days)


@given('"{venue}" is a servable venue in Recife')
def step_given_servable_venue(context, venue):
    _venue_id_for(context, venue)


@given("the current time is {when} in Recife")
def step_given_current_time(context, when):
    context.evs_now = _parse_recife(when).astimezone(timezone.utc)


# ── event fixtures: one-off ────────────────────────────────────────────────
@given('an accepted event "{name}" at "{venue}" starting {when}')
def step_given_named_event_at_venue_starting(context, name, venue, when):
    starts_at = _parse_recife(when).astimezone(timezone.utc)
    _insert_event(context, venue_name=venue, title=name, starts_at=starts_at, time_known=True)


_NO_CLOCK_TIME_SUFFIX = "with no stated clock time"


@given('an accepted event at "{venue}" starting {when}')
def step_given_event_at_venue_starting(context, venue, when):
    if when.endswith(_NO_CLOCK_TIME_SUFFIX):
        date_str = when[: -len(_NO_CLOCK_TIME_SUFFIX)].strip()
        naive = datetime.strptime(f"{date_str} 00:00", "%Y-%m-%d %H:%M")
        starts_at = naive.replace(tzinfo=RECIFE_TZ).astimezone(timezone.utc)
        _insert_event(context, venue_name=venue, starts_at=starts_at, time_known=False)
        return
    starts_at = _parse_recife(when).astimezone(timezone.utc)
    _insert_event(context, venue_name=venue, starts_at=starts_at, time_known=True)


@given("an accepted event whose price text is \"{price_text}\"")
def step_given_event_with_price_text(context, price_text):
    starts_at = _now(context) + timedelta(hours=2)
    _insert_event(context, starts_at=starts_at, time_known=True, price_text=price_text)


@given('whose ticket info is "{ticket_info}"')
def step_given_last_event_ticket_info(context, ticket_info):
    context.rds_store.update_event(context.evs_last_event_id, {"ticket_info": ticket_info})


@given('the event has category "{category}", price text "{price_text}", ticket info "{ticket_info}"')
def step_given_event_category_price_ticket(context, category, price_text, ticket_info):
    context.rds_store.update_event(context.evs_last_event_id, {
        "category": category, "price_text": price_text, "ticket_info": ticket_info,
    })


@given('the event has a description and an attractions list with one act on stage "{stage}"')
def step_given_event_description_and_attractions(context, stage):
    context.rds_store.update_event(context.evs_last_event_id, {
        "description": "Uma noite imperdível de música eletrônica.",
        "attractions": [{"name": "DJ Teste", "type": "dj", "stage": stage, "styles": ["techno"]}],
    })


@given("an accepted event at \"{venue}\" with no resolved start time")
def step_given_event_no_start_time(context, venue):
    _insert_event(context, venue_name=venue, starts_at=None, is_recurring=False)


# ── event fixtures: recurring ──────────────────────────────────────────────
@given('an accepted recurring event "{name}" at "{venue}"')
def step_given_named_recurring_event(context, name, venue):
    # A deliberately STALE starts_at (60 days before "now") — proving
    # expansion ignores the stored DATE entirely and only keeps its
    # clock time, exactly as app.services.event_occurrences documents.
    stale = (_now(context) - timedelta(days=60)).astimezone(RECIFE_TZ)
    _insert_event(
        context, venue_name=venue, title=name, is_recurring=True,
        starts_at=stale.replace(hour=21, minute=0).astimezone(timezone.utc),
        time_known=True,
    )


@given('its recurrence text is "{text}" and its resolved time is {time}')
def step_given_recurrence_text_and_time(context, text, time):
    hour, minute = (int(p) for p in time.strip().split(":"))
    stale = (_now(context) - timedelta(days=60)).astimezone(RECIFE_TZ)
    starts_at = stale.replace(hour=hour, minute=minute, second=0, microsecond=0)
    context.rds_store.update_event(context.evs_last_event_id, {
        "recurrence_text": text, "starts_at": starts_at.astimezone(timezone.utc),
    })


@given('an accepted recurring event at "{venue}" whose recurrence text is "{text}"')
def step_given_recurring_event_with_text(context, venue, text):
    stale = (_now(context) - timedelta(days=60)).astimezone(RECIFE_TZ)
    starts_at = stale.replace(hour=21, minute=0, second=0, microsecond=0)
    _insert_event(
        context, venue_name=venue, is_recurring=True, recurrence_text=text,
        starts_at=starts_at.astimezone(timezone.utc), time_known=True,
    )


# ── event fixtures: disqualifiers ─────────────────────────────────────────
@given('an event at "{venue}" starting {when} that is {disqualifier}')
def step_given_disqualified_event(context, venue, when, disqualifier):
    starts_at = _parse_recife(when).astimezone(timezone.utc)
    fields: dict = {"status": "accepted"}
    no_venue = False
    non_servable_venue = False
    if disqualifier == "still pending review":
        fields["status"] = "pending_review"
    elif disqualifier == "rejected":
        fields["status"] = "rejected"
    elif disqualifier == "superseded by another event":
        other_id = _insert_event(context, venue_name=venue, starts_at=starts_at, time_known=True)
        fields["superseded_by"] = other_id
    elif disqualifier == 'of post type "menu"':
        fields["post_type"] = "menu"
    elif disqualifier == "linked to no venue":
        no_venue = True
    elif disqualifier == "linked to a non-servable venue":
        non_servable_venue = True
    else:
        raise ValueError(f"unrecognised disqualifier: {disqualifier!r}")
    _insert_event(
        context, venue_name=venue, starts_at=starts_at, time_known=True,
        no_venue=no_venue, non_servable_venue=non_servable_venue, **fields,
    )


# ── event fixtures: promoter visibility ───────────────────────────────────
@given('"hide_promoter_events" is true')
def step_given_hide_promoter_events_true(context):
    context.fake_redis.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps(True))


@given('an accepted event at "{venue}" whose every source is a promoter post')
def step_given_promoter_only_event(context, venue):
    starts_at = _now(context) + timedelta(hours=2)
    _insert_event(
        context, venue_name=venue, starts_at=starts_at, time_known=True, source_kind="promoter_post",
    )


@when('an admin sets "hide_promoter_events" to false')
def step_when_admin_unhides_promoter_events(context):
    context.fake_redis.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps(False))


# ── multi-event fixtures ───────────────────────────────────────────────────
@given('two accepted events at "{venue}" and one at another venue')
def step_given_two_at_venue_one_elsewhere(context, venue):
    base = _now(context) + timedelta(hours=1)
    _insert_event(context, venue_name=venue, starts_at=base, time_known=True)
    _insert_event(context, venue_name=venue, starts_at=base + timedelta(hours=1), time_known=True)
    _insert_event(context, venue_name="Some Other Venue", starts_at=base, time_known=True)


@given('three accepted events at "{venue}"')
def step_given_three_at_venue(context, venue):
    base = _now(context) + timedelta(hours=1)
    context.evs_three_event_ids = [
        _insert_event(context, venue_name=venue, starts_at=base + timedelta(minutes=i), time_known=True)
        for i in range(3)
    ]


@given("projecting the second one raises an error")
def step_given_second_raises(context):
    # An invalid `attractions` shape fails EventOccurrence's own Pydantic
    # validation inside project_events' per-event try/except — a REAL
    # failure mode, not a synthetic test-only hook.
    second_id = context.evs_three_event_ids[1]
    context.rds_store.update_event(second_id, {"attractions": "not-a-list-of-dicts"})


@given('four accepted events at "{venue}"')
def step_given_four_at_venue(context, venue):
    base = _now(context) + timedelta(hours=1)
    for i in range(4):
        _insert_event(context, venue_name=venue, starts_at=base + timedelta(minutes=i), time_known=True)


# ── flyer fixtures ─────────────────────────────────────────────────────────
@given('an accepted event at "{venue}" with an archived cover photo')
def step_given_event_with_archived_cover(context, venue):
    _ensure_flyer_service(context)
    starts_at = _now(context) + timedelta(hours=2)
    cover_key = f"retrieved/source=instagram/venue_id=x/media/cover/{uuid.uuid4().hex}.jpg"
    context.evs_fake_s3_archive.objects[cover_key] = (b"fake-flyer-bytes", "image/jpeg")
    _insert_event(context, venue_name=venue, starts_at=starts_at, time_known=True, cover_photo_key=cover_key)


@given('an accepted event at "{venue}" whose flyer has already been copied')
def step_given_event_flyer_already_copied(context, venue):
    _ensure_flyer_service(context)
    starts_at = _now(context) + timedelta(hours=2)
    existing_url = "https://media.vibesense.example/event-flyers/already-copied/abc123.jpg"
    context.evs_existing_flyer_url = existing_url
    _insert_event(
        context, venue_name=venue, starts_at=starts_at, time_known=True,
        cover_photo_key="retrieved/.../irrelevant.jpg", flyer_url=existing_url,
    )


@given('an accepted event at "{venue}" with no archived cover photo')
def step_given_event_no_cover(context, venue):
    _ensure_flyer_service(context)
    starts_at = _now(context) + timedelta(hours=2)
    _insert_event(context, venue_name=venue, starts_at=starts_at, time_known=True)


@given("the media bucket rejects the write with access denied")
def step_given_media_bucket_denies(context):
    _ensure_flyer_service(context)
    context.evs_fake_s3_media.deny = True


# ── neighbourhood fixtures ─────────────────────────────────────────────────
@given('the stored address neighborhood for "{venue}" is "{neighborhood}"')
def step_given_stored_neighborhood(context, venue, neighborhood):
    venue_id = _venue_id_for(context, venue)
    context.rds_store.update_venue_address_components(venue_id, neighborhood=neighborhood)


@given('"{venue}" has no stored address neighborhood')
def step_given_no_stored_neighborhood(context, venue):
    _venue_id_for(context, venue)  # ensure it exists; address neighborhood is null by default


# ── city / geo-fence fixtures ─────────────────────────────────────────────
@given('"{venue}" lies inside the "recife" geo-fence circle')
def step_given_lies_inside_recife(context, venue):
    _venue_id_for(context, venue, lat=_DEFAULT_LAT, lng=_DEFAULT_LNG)


@given("a venue lying inside both the \"recife\" and a neighbouring city circle")
def step_given_overlapping_circles_venue(context):
    from app.services.venue_eligibility import CAPITALS_BY_SLUG

    jp = CAPITALS_BY_SLUG["joao-pessoa"]
    # Nudged toward João Pessoa but still within a widened Recife circle.
    venue_lat, venue_lng = -7.35, -34.85
    context.evs_overlap_venue_id = _venue_id_for(
        context, "Overlap Venue", lat=venue_lat, lng=venue_lng,
    )
    context.rds_store.set_geo_fence({
        "enabled": True,
        "cities": [
            {"slug": "recife", "name": "Recife", "lat": -8.0476, "lng": -34.8770, "radius_km": 150.0},
            {"slug": "joao-pessoa", "name": "João Pessoa", "lat": jp["lat"], "lng": jp["lng"], "radius_km": 150.0},
        ],
    })
    _insert_event(
        context, venue_name="Overlap Venue",
        starts_at=_now(context) + timedelta(hours=1), time_known=True,
    )


@given("the neighbouring circle's centre is closer to the venue")
def step_given_neighbouring_closer(context):
    from app.services.venue_eligibility import haversine_km

    addr = context.rds_store.get_address(context.evs_overlap_venue_id)
    d_recife = haversine_km(addr["lat"], addr["lng"], -8.0476, -34.8770)
    d_jp = haversine_km(addr["lat"], addr["lng"], -7.1195, -34.8450)
    assert d_jp < d_recife, (d_jp, d_recife)  # sanity-check the fixture itself


@given('"{venue}" has one accepted event and the projection has run')
def step_given_one_event_and_run(context, venue):
    _insert_event(context, venue_name=venue, starts_at=_now(context) + timedelta(hours=1), time_known=True)
    context.evs_summary = context.redis_projection_service.project_events(now=_now(context))


@given('the "recife" events index holds one occurrence')
def step_given_recife_index_holds_one(context):
    _insert_event(
        context, venue_name=_DEFAULT_VENUE,
        starts_at=_now(context) + timedelta(hours=1), time_known=True,
    )
    context.evs_summary = context.redis_projection_service.project_events(now=_now(context))


@given("the events projection has run")
def step_given_projection_has_run(context):
    context.evs_summary = context.redis_projection_service.project_events(now=_now(context))


@given("the events projection has run and projected three occurrences")
def step_given_projection_ran_three(context):
    base = _now(context) + timedelta(hours=1)
    for i in range(3):
        _insert_event(
            context, venue_name=_DEFAULT_VENUE, starts_at=base + timedelta(minutes=i), time_known=True,
        )
    context.evs_summary = context.redis_projection_service.project_events(now=_now(context))
    assert context.evs_summary["occurrences"] == 3, context.evs_summary
    context.evs_pre_failure_city_index = list(
        context.redis_only_dao.get_city_events_index("recife")
    )


# ── When ────────────────────────────────────────────────────────────────
@when("the events projection runs")
def step_when_projection_runs(context):
    context.evs_flyer_metric_before = _snapshot_flyer_counters(context)
    context.evs_summary = context.redis_projection_service.project_events(now=_now(context))


@when("the events selection query fails")
def step_when_selection_query_fails(context):
    context.rds_store.set_unavailable(True)


@when("an admin rejects the event")
def step_when_admin_rejects_event(context):
    context.rds_store.update_event(context.evs_last_event_id, {"status": "rejected"})


@when("the event is rejected")
def step_when_the_event_is_rejected(context):
    context.rds_store.update_event(context.evs_last_event_id, {"status": "rejected"})


@when("that occurrence's event is rejected")
def step_when_that_occurrences_event_rejected(context):
    context.rds_store.update_event(context.evs_last_event_id, {"status": "rejected"})


@when('an admin writes a geo-fence city with the slug "{slug}"')
def step_when_admin_writes_bad_slug(context, slug):
    context.evs_geofence_before = context.rds_store.get_geo_fence()
    context.evs_put_response = context.client.put(
        "/admin/config/geofence",
        json={"enabled": True, "cities": [{"slug": slug, "radius_km": 30}]},
    )


# ── Then: general projection outcome ──────────────────────────────────────
def _last_occurrence(context):
    """The (only) occurrence produced for evs_last_event_id — most scenarios
    project exactly one occurrence per fixture event."""
    occ_id = context.evs_last_event_id
    occ = context.redis_only_dao.get_event_occurrence(occ_id)
    if occ is not None:
        return occ
    # Recurring events materialise under a DATED id — find the one carrying
    # this event_id among all cities' indexed occurrences.
    for city in context.rds_store.get_geo_fence().get("cities", []):
        for member in context.redis_only_dao.get_city_events_index(city["slug"]):
            candidate = context.redis_only_dao.get_event_occurrence(member)
            if candidate is not None and candidate.event_id == occ_id:
                return candidate
    return None


@then("no occurrence is projected for that event")
def step_then_no_occurrence_projected(context):
    assert _last_occurrence(context) is None


@then("the occurrence is projected")
@then("the occurrence is projected for that event")
def step_then_occurrence_projected(context):
    assert _last_occurrence(context) is not None


@then("the occurrence is still projected")
def step_then_occurrence_still_projected(context):
    assert _last_occurrence(context) is not None


# ── Then: contract payload fields ─────────────────────────────────────────
@then("the occurrence payload contains the title, description, category, price text and ticket info")
def step_then_payload_core_fields(context):
    occ = _last_occurrence(context)
    assert occ.title, occ
    assert occ.description, occ
    assert occ.category, occ
    assert occ.price_text, occ
    assert occ.ticket_info, occ


@then("the occurrence payload contains the attractions list with the act name, type, stage and styles")
def step_then_payload_attractions(context):
    occ = _last_occurrence(context)
    assert occ.attractions and len(occ.attractions) == 1, occ
    act = occ.attractions[0]
    for field in ("name", "type", "stage", "styles"):
        assert act.get(field), (field, act)


@then("the occurrence payload contains the venue id, venue name, latitude and longitude")
def step_then_payload_venue_fields(context):
    occ = _last_occurrence(context)
    assert occ.venue_id
    assert occ.venue_name
    assert occ.venue_lat is not None
    assert occ.venue_lng is not None


@then("the coordinates come from the venue address record, not from the venue row")
def step_then_coordinates_from_address(context):
    occ = _last_occurrence(context)
    addr = context.rds_store.get_address(occ.venue_id)
    assert occ.venue_lat == addr["lat"]
    assert occ.venue_lng == addr["lng"]
    # Structural proof, not just a matching-value coincidence: the venue ROW
    # itself carries no lat/lng field at all (migration 0007 dropped those
    # columns from venues.venue) — the address table is the ONLY possible
    # source.
    venue_row = context.rds_store.venues[occ.venue_id]
    assert "venue_lat" not in venue_row and "lat" not in venue_row, venue_row


@then("the occurrence payload contains the source permalink and source handle")
def step_then_payload_source_fields(context):
    occ = _last_occurrence(context)
    assert occ.source_permalink
    assert occ.source_handle


@then('the occurrence carries the local occurrence date "{date}"')
def step_then_occurrence_date(context, date):
    occ = _last_occurrence(context)
    assert occ.occurrence_date == date, occ.occurrence_date


@then('the occurrence carries "starts_at" as a UTC timestamp')
def step_then_starts_at_is_utc(context):
    occ = _last_occurrence(context)
    assert occ.starts_at is not None
    assert occ.starts_at.tzinfo is not None
    assert occ.starts_at.utcoffset().total_seconds() == 0


@then('the Recife events index contains the occurrence scored by its "starts_at" epoch')
def step_then_recife_index_scored_by_starts_at(context):
    occ = _last_occurrence(context)
    score = context.redis_only_dao.city_events_index_score("recife", occ.occurrence_id)
    assert score == occ.starts_at.timestamp()


@then("the occurrence payload reports both strings on their own fields")
def step_then_price_and_ticket_independent(context):
    occ = _last_occurrence(context)
    assert occ.price_text == "R$100 em consumação", occ
    assert occ.ticket_info == "Grátis até 23:30", occ
    assert occ.price_text != occ.ticket_info


@then('the occurrence payload reports "time_known" as false')
def step_then_time_known_false(context):
    occ = _last_occurrence(context)
    assert occ.time_known is False


# ── Then: promoter visibility ──────────────────────────────────────────────
# ("no occurrence is projected for that event" / "the occurrence is
#  projected for that event" already cover this scenario's Then steps.)


# ── Then: recurrence expansion ─────────────────────────────────────────────
def _occurrences_for_last_event(context) -> list:
    event_id = context.evs_last_event_id
    seen = []
    for city in context.rds_store.get_geo_fence().get("cities", []):
        for member in context.redis_only_dao.get_city_events_index(city["slug"]):
            occ = context.redis_only_dao.get_event_occurrence(member)
            if occ is not None and occ.event_id == event_id:
                seen.append(occ)
    seen.sort(key=lambda o: o.occurrence_date)
    return seen


@then("an occurrence is projected for every Thursday within the horizon")
def step_then_every_thursday(context):
    occs = _occurrences_for_last_event(context)
    assert occs, "no occurrences projected"
    for occ in occs:
        d = datetime.fromisoformat(occ.occurrence_date)
        assert d.weekday() == 3, occ.occurrence_date  # Thursday
    today = _now(context).astimezone(RECIFE_TZ).date()
    horizon = settings.events_projection_horizon_days
    expected = sum(
        1 for i in range(horizon + 1)
        if (today + timedelta(days=i)).weekday() == 3
    )
    assert len(occs) == expected, (len(occs), expected)
    context.evs_recurring_occurrences = occs


@then("each occurrence carries its own occurrence id ending in its own date")
def step_then_each_id_ends_in_own_date(context):
    for occ in context.evs_recurring_occurrences:
        assert occ.occurrence_id == f"{occ.event_id}_{occ.occurrence_date}", occ


@then("no occurrence id contains a URI-reserved character")
def step_then_no_reserved_char_in_ids(context):
    import string
    unreserved = set(string.ascii_letters + string.digits + "-._~")
    for occ in context.evs_recurring_occurrences:
        bad = set(occ.occurrence_id) - unreserved
        assert not bad, (occ.occurrence_id, bad)
        assert "#" not in occ.occurrence_id


@then("each occurrence id survives being placed in a URL path unchanged")
def step_then_id_survives_url_path(context):
    from urllib.parse import urlparse
    for occ in context.evs_recurring_occurrences:
        url = f"https://api.example.com/events/{occ.occurrence_id}"
        parsed = urlparse(url)
        assert parsed.path.rsplit("/", 1)[-1] == occ.occurrence_id
        assert parsed.fragment == ""


@then("each occurrence starts at {time} Recife time on its own date")
def step_then_each_starts_at_time(context, time):
    hour, minute = (int(p) for p in time.strip().split(":"))
    for occ in context.evs_recurring_occurrences:
        local = occ.starts_at.astimezone(RECIFE_TZ)
        assert (local.hour, local.minute) == (hour, minute), occ


@then("no occurrence is projected beyond the horizon")
def step_then_none_beyond_horizon(context):
    today = _now(context).astimezone(RECIFE_TZ).date()
    horizon = settings.events_projection_horizon_days
    last_allowed = today + timedelta(days=horizon)
    for occ in context.evs_recurring_occurrences:
        d = datetime.fromisoformat(occ.occurrence_date).date()
        assert d <= last_allowed, (d, last_allowed)


@then("an occurrence is projected for every day within the horizon")
def step_then_every_day(context):
    occs = _occurrences_for_last_event(context)
    assert len(occs) == settings.events_projection_horizon_days + 1, len(occs)


@then("exactly one occurrence is projected at its resolved start time")
def step_then_exactly_one_at_resolved_time(context):
    occs = _occurrences_for_last_event(context)
    assert len(occs) == 1, occs
    assert occs[0].occurrence_id == context.evs_last_event_id


# ── Then: indexing ─────────────────────────────────────────────────────────
@then("the Recife events index contains the occurrence")
def step_then_recife_index_contains(context):
    occ = _last_occurrence(context)
    assert occ.occurrence_id in context.redis_only_dao.get_city_events_index("recife")


@then('the venue events index for "{venue}" contains the same occurrence')
def step_then_venue_index_contains_same(context, venue):
    occ = _last_occurrence(context)
    venue_id = _venue_id_for(context, venue)
    assert occ.occurrence_id in context.redis_only_dao.get_venue_events_index(venue_id)


@then("the occurrence carries the same score in both indexes")
def step_then_same_score_both_indexes(context):
    occ = _last_occurrence(context)
    city_score = context.redis_only_dao.city_events_index_score("recife", occ.occurrence_id)
    venue_score = context.redis_only_dao.venue_events_index_score(occ.venue_id, occ.occurrence_id)
    assert city_score == venue_score == occ.starts_at.timestamp()


@then('the venue events index for "{venue}" contains exactly its two occurrences')
def step_then_venue_index_exactly_two(context, venue):
    venue_id = _venue_id_for(context, venue)
    members = context.redis_only_dao.get_venue_events_index(venue_id)
    assert len(members) == 2, members
    for member in members:
        occ = context.redis_only_dao.get_event_occurrence(member)
        assert occ.venue_id == venue_id


# ── Then: removal on disqualification ──────────────────────────────────────
@then("the occurrence key is deleted")
def step_then_occurrence_key_deleted(context):
    assert context.redis_only_dao.get_event_occurrence(context.evs_last_event_id) is None


@then("the occurrence is no longer a member of the Recife events index")
def step_then_not_in_recife_index(context):
    assert context.evs_last_event_id not in context.redis_only_dao.get_city_events_index("recife")


@then("the occurrence is no longer a member of the venue events index")
def step_then_not_in_venue_index(context):
    venue_id = _venue_id_for(context, _DEFAULT_VENUE)
    assert context.evs_last_event_id not in context.redis_only_dao.get_venue_events_index(venue_id)


# ── Then: fail-safe ─────────────────────────────────────────────────────────
@then("the three occurrences are still present in Redis")
def step_then_three_still_present(context):
    current = context.redis_only_dao.get_city_events_index("recife")
    assert set(current) == set(context.evs_pre_failure_city_index), current
    for occ_id in context.evs_pre_failure_city_index:
        assert context.redis_only_dao.get_event_occurrence(occ_id) is not None


@then("the run summary reports an error")
def step_then_summary_reports_error(context):
    assert context.evs_summary["errors"] >= 1, context.evs_summary


@then("the other two occurrences are projected")
def step_then_other_two_projected(context):
    assert context.evs_summary["occurrences"] == 2, context.evs_summary
    good_ids = [context.evs_three_event_ids[0], context.evs_three_event_ids[2]]
    for eid in good_ids:
        assert context.redis_only_dao.get_event_occurrence(eid) is not None


@then("the run summary names the failing event id")
def step_then_summary_names_failing_id(context):
    failing_id = context.evs_three_event_ids[1]
    assert failing_id in context.evs_summary["error_events"], context.evs_summary


# ── Then: flyer media ────────────────────────────────────────────────────
@then("the flyer is stored in the media bucket under a content-addressed key")
def step_then_flyer_stored(context):
    assert len(context.evs_fake_s3_media.puts) == 1, context.evs_fake_s3_media.puts
    put = context.evs_fake_s3_media.puts[0]
    assert put["Key"].startswith(f"event-flyers/{context.evs_last_event_id}/")
    assert put["Key"].endswith(".jpg")


@then("the stored object carries the immutable cache control header")
def step_then_flyer_cache_header(context):
    put = context.evs_fake_s3_media.puts[0]
    assert put["CacheControl"] == "public, max-age=31536000, immutable"


@then("the occurrence payload carries the CloudFront flyer url")
def step_then_flyer_url_in_payload(context):
    occ = _last_occurrence(context)
    assert occ.flyer_url is not None
    assert occ.flyer_url.startswith("https://media.vibesense.example/event-flyers/")


@then("the occurrence payload carries the same flyer url as before")
def step_then_same_flyer_url(context):
    occ = _last_occurrence(context)
    assert occ.flyer_url == context.evs_existing_flyer_url


@then("the occurrence is projected with a null flyer url")
def step_then_null_flyer_url(context):
    occ = _last_occurrence(context)
    assert occ is not None
    assert occ.flyer_url is None


@then('the flyer copy outcome "{outcome}" is recorded')
def step_then_flyer_outcome_recorded(context, outcome):
    before = context.evs_flyer_metric_before.get(outcome, 0)
    after = EVENT_FLYER_COPY_TOTAL.labels(outcome=outcome)._value.get()
    assert after is not None and after > before, (outcome, before, after)


@then("no occurrence payload contains a data-lake key or a presigned url")
def step_then_no_datalake_leak(context):
    occ = _last_occurrence(context)
    dump = occ.model_dump()
    assert "cover_photo_key" not in dump
    flyer_url = dump.get("flyer_url") or ""
    assert "retrieved/" not in flyer_url
    assert "X-Amz-Signature" not in flyer_url
    assert "Expires=" not in flyer_url


# ── Then: neighbourhood ──────────────────────────────────────────────────
@then('the occurrence payload reports the venue neighborhood "{neighborhood}"')
def step_then_payload_neighborhood(context, neighborhood):
    occ = _last_occurrence(context)
    assert occ.venue_neighborhood == neighborhood, occ


@then("the occurrence payload reports a null venue neighborhood")
def step_then_payload_null_neighborhood(context):
    occ = _last_occurrence(context)
    assert occ.venue_neighborhood is None, occ


# ── Then: city / geo-fence ─────────────────────────────────────────────────
@then('the occurrence reports the city "{city}"')
def step_then_occurrence_reports_city(context, city):
    occ = _last_occurrence(context)
    assert occ.city_slug == city, occ.city_slug


@then('the occurrence is a member of the "{city}" events index')
def step_then_occurrence_member_of_city_index(context, city):
    occ = _last_occurrence(context)
    assert occ.occurrence_id in context.redis_only_dao.get_city_events_index(city)


@then("the occurrence reports the neighbouring city")
def step_then_reports_neighbouring_city(context):
    occ = _last_occurrence(context)
    assert occ.city_slug == "joao-pessoa", occ.city_slug


@then("the projection makes the same choice on every subsequent cycle")
def step_then_same_choice_every_cycle(context):
    first = _last_occurrence(context).city_slug
    context.redis_projection_service.project_events(now=_now(context))
    second = _last_occurrence(context).city_slug
    assert first == second == "joao-pessoa"


@then("the write is rejected as non-canonical")
def step_then_write_rejected_non_canonical(context):
    assert context.evs_put_response.status_code == 400, context.evs_put_response.text
    assert "non-canonical" in context.evs_put_response.text


@then("the stored geo-fence cities are unchanged")
def step_then_geofence_unchanged(context):
    assert context.rds_store.get_geo_fence() == context.evs_geofence_before


# ── Then: emptying indexes ─────────────────────────────────────────────────
@then('the venue events index for "{venue}" is empty')
def step_then_venue_index_empty(context, venue):
    venue_id = _venue_id_for(context, venue)
    assert context.redis_only_dao.get_venue_events_index(venue_id) == []


@then("its payload keys are deleted")
def step_then_payload_keys_deleted(context):
    assert context.redis_only_dao.get_event_occurrence(context.evs_last_event_id) is None


@then("the cycle issues no keyspace scan against Redis")
def step_then_no_keyspace_scan(context):
    def _forbidden(*args, **kwargs):
        raise AssertionError("KEYS/SCAN was issued against Redis during the events cycle")

    original = context.geo_redis.keys
    context.geo_redis.keys = _forbidden
    try:
        context.redis_projection_service.project_events(now=_now(context))
    finally:
        context.geo_redis.keys = original


@then('the "{city}" events index is empty')
def step_then_city_index_empty(context, city):
    assert context.redis_only_dao.get_city_events_index(city) == []


# ── Then: observability ─────────────────────────────────────────────────
@then("the run summary reports the projected occurrence count")
def step_then_summary_reports_count(context):
    assert context.evs_summary["occurrences"] == 4, context.evs_summary


@then("the run summary reports the total projected payload bytes")
def step_then_summary_reports_bytes(context):
    assert context.evs_summary["bytes"] > 0, context.evs_summary
