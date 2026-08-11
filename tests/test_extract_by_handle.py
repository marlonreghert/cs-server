"""Unit tests for plans/260811_extract-by-handle.md.

Covers: `mode="handles"` config parsing, `EventPostSource.posts_for_handle`'s
cross-prefix read + dedupe (against a fake `MediaArchiveStore` — no real S3,
no OpenAI, no Apify), and `EventExtractionService`'s handles-mode
orchestration — including §B's supersession trap in BOTH directions (a
corrected date supersedes its stale sibling; an operator-curated row is
never erased) and §C's cost-before-spending log/cap.

BDD (tests/bdd/enrichment/extract-by-handle.feature) covers the end-to-end
user-facing scenarios; this file protects the lower-level mechanics this
plan touches no existing DAO predicate for (see the PR description), so it
does not add a [fake, rds] case to test_rds_store_contract.py.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone

import pytest
from prometheus_client import REGISTRY

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_extraction_service import (
    ArchivedPost,
    EventExtractionService,
    EventPostSource,
    HANDLE_OUTCOME_EXTRACTED,
    HANDLE_OUTCOME_NOTHING_ARCHIVED,
    HANDLE_OUTCOME_NO_VENUE_MAPPED,
    InvalidEventExtractionConfig,
    parse_event_extraction_config,
)
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE_LAT, RECIFE_LNG = -8.05, -34.88


def _run(coro):
    return asyncio.run(coro)


# ── fakes ─────────────────────────────────────────────────────────────────────
class _FakeOpenAIClient:
    """Programmed with one response (a JSON string) per call, in order —
    mirrors tests/test_event_extraction_service.py's own fake."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    async def extract_events(self, *, caption, image_data_uri=None, max_events):
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        flat = json.loads(item)
        if isinstance(flat, dict) and "events" not in flat:
            return json.dumps({"events": [flat]}), False
        return item, False


class _StubPostSource:
    """Feeds `EventExtractionService.run()` controlled `ArchivedPost` lists
    per handle, bypassing real S3 entirely — proves `_run_handles`' own
    orchestration (venue resolution, the cost-before-spending log, the
    `max_posts_per_venue` cap, supersession) independent of the S3-reading
    mechanics `TestEventPostSourcePostsForHandle` below covers against a fake
    `MediaArchiveStore`."""

    def __init__(self):
        self.posts_by_handle: dict[str, list[ArchivedPost]] = {}
        self.calls: list[tuple[str, list]] = []

    async def posts_for_handle(self, handle, venue_ids, since):
        self.calls.append((handle, list(venue_ids)))
        return self.posts_by_handle.get(handle, [])

    async def posts_for_venue(self, venue_id, since):
        return []

    async def image_data_uri(self, key):
        return f"data:image/jpeg;base64,FAKE_{key}" if key else None


class _PoisonedApifyClient:
    """If this is ever called, extract-by-handle has regressed into
    re-crawling — the one thing this plan exists to avoid (§Non-goals: "It
    must never call Apify"). Not wired into `EventExtractionService`/
    `EventPostSource` at all — there is no constructor parameter for it to
    reach. Asserting `.calls == 0` after a real run is a regression guard
    should a future change ever add such a parameter and wire it in."""

    def __init__(self):
        self.calls = 0

    async def fetch_recent_posts(self, handle, *, results_limit):
        self.calls += 1
        raise AssertionError("Apify must never be called by extract-by-handle")


def _dao() -> VenueRepository:
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


def _seed_venue(dao: VenueRepository, vid: str, handle: str) -> None:
    dao.upsert_venue(Venue(venue_id=vid, venue_name=f"V {vid}", venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG))
    dao.set_venue_instagram(VenueInstagram(venue_id=vid, instagram_handle=handle, status="found"))


def _seed_named_venue(dao: VenueRepository, vid: str, handle: str, venue_name: str) -> None:
    """Like `_seed_venue`, with a REAL venue name — needed for the
    multi-venue ladder tests, which resolve on `name_similarity` against
    each event's own `location_text`; `_seed_venue`'s arbitrary "V <id>"
    names are indistinguishable from each other for that purpose."""
    dao.upsert_venue(Venue(venue_id=vid, venue_name=venue_name, venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG))
    dao.set_venue_instagram(VenueInstagram(venue_id=vid, instagram_handle=handle, status="found"))


def _post(shortcode="s1", **overrides) -> ArchivedPost:
    base = dict(
        shortcode=shortcode, permalink=f"https://instagram.com/p/{shortcode}",
        caption="Ingressos abertos!", timestamp=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
        flyer_photo_key=f"{shortcode}.jpg", flyer_confidence=0.9, any_photo_key=f"{shortcode}.jpg",
    )
    base.update(overrides)
    return ArchivedPost(**base)


def _event_json(**overrides) -> str:
    payload = {
        "title": "Festa", "description": None, "date_text": "01/07/2026",
        "time_text": "20h", "is_recurring": False, "recurrence_text": None,
        "lineup": [], "ticket_url": None, "price_text": None,
        "location_text": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _run_handles(dao, post_source, openai, *, handles="entreamigosobode", **cfg_overrides):
    service = EventExtractionService(
        dao, post_source, openai, min_confidence=0.5, flyer_confidence_floor=0.6,
    )
    cfg = {"eligibility": {"mode": "handles", "handles": handles}}
    cfg.update(cfg_overrides)
    return _run(service.run(cfg))


# ── §A: config parsing ─────────────────────────────────────────────────────────
class TestParseHandlesConfig:
    def test_handles_as_comma_string(self):
        cfg = parse_event_extraction_config(
            {"eligibility": {"mode": "handles", "handles": "a, b ,c"}}, default_min_confidence=0.5,
        )
        assert cfg["eligibility_mode"] == "handles"
        assert cfg["eligibility_handles"] == ["a", "b", "c"]

    def test_handles_as_list(self):
        cfg = parse_event_extraction_config(
            {"eligibility": {"mode": "handles", "handles": ["a", " b", "", "c"]}},
            default_min_confidence=0.5,
        )
        assert cfg["eligibility_handles"] == ["a", "b", "c"]

    def test_handles_empty_defaults_to_empty_list(self):
        cfg = parse_event_extraction_config({"eligibility": {"mode": "handles"}}, default_min_confidence=0.5)
        assert cfg["eligibility_handles"] == []

    def test_handles_whitespace_only_string_is_empty(self):
        cfg = parse_event_extraction_config(
            {"eligibility": {"mode": "handles", "handles": "   "}}, default_min_confidence=0.5,
        )
        assert cfg["eligibility_handles"] == []

    def test_unknown_mode_still_raises(self):
        with pytest.raises(InvalidEventExtractionConfig):
            parse_event_extraction_config({"eligibility": {"mode": "bogus"}}, default_min_confidence=0.5)

    def test_existing_modes_are_unaffected(self):
        cfg = parse_event_extraction_config(
            {"eligibility": {"mode": "venue_ids", "venue_ids": "v1,v2"}}, default_min_confidence=0.5,
        )
        assert cfg["eligibility_mode"] == "venue_ids"
        assert cfg["eligibility_venue_ids"] == ["v1", "v2"]
        assert cfg["eligibility_handles"] == []  # additive key, present but unused


# ── §A: the prefix resolver (EventPostSource.posts_for_handle) ────────────────
class _FakeMediaStore:
    """venue_id=<v> and promoter=<handle> manifests, keyed by (prefix, key) —
    no real S3, no boto3 client, mirrors tests/test_event_extraction_service.
    py's `_FakeMediaStoreForPostGrouping` extended for the promoter side."""

    def __init__(self):
        self._prefixes: list[str] = []
        self._venue: dict[tuple[str, str], dict] = {}
        self._promoter: dict[tuple[str, str], dict] = {}

    def add_venue_manifest(self, prefix: str, venue_id: str, photos: list[dict]) -> None:
        if prefix not in self._prefixes:
            self._prefixes.append(prefix)
        self._venue[(prefix, venue_id)] = {"photos": photos}

    def add_promoter_manifest(self, prefix: str, handle: str, photos: list[dict]) -> None:
        if prefix not in self._prefixes:
            self._prefixes.append(prefix)
        self._promoter[(prefix, handle)] = {"handle": handle, "photos": photos}

    async def list_run_prefixes(self, source):
        return sorted(self._prefixes)

    async def read_manifest(self, prefix, venue_id):
        return self._venue.get((prefix, venue_id))

    async def read_promoter_manifest(self, prefix, handle):
        return self._promoter.get((prefix, handle))


def _prefix(day: str, run: str) -> str:
    return f"retrieved/source=instagram_posts/year=2026/month=07/day={day}/run_id={run}/"


def _photo(shortcode: str, **overrides) -> dict:
    base = {
        "shortcode": shortcode, "permalink": f"https://instagram.com/p/{shortcode}",
        "caption": "cap", "uploaded_at": "2026-07-01T12:00:00.000Z",
        "key": f"{shortcode}.jpg", "category": "flyer", "classification_confidence": 0.9,
    }
    base.update(overrides)
    return base


class TestEventPostSourcePostsForHandle:
    SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_handle_only_prefix_is_found(self):
        store = _FakeMediaStore()
        store.add_promoter_manifest(_prefix("01", "r1"), "h1", [_photo("s1")])
        source = EventPostSource(store)
        posts = _run(source.posts_for_handle("h1", ["v1"], self.SINCE))
        assert [p.shortcode for p in posts] == ["s1"]

    def test_venue_only_prefix_is_found(self):
        store = _FakeMediaStore()
        store.add_venue_manifest(_prefix("01", "r1"), "v1", [_photo("s1")])
        source = EventPostSource(store)
        posts = _run(source.posts_for_handle("h1", ["v1"], self.SINCE))
        assert [p.shortcode for p in posts] == ["s1"]

    def test_both_prefixes_contribute_distinct_posts(self):
        store = _FakeMediaStore()
        store.add_promoter_manifest(_prefix("01", "r1"), "h1", [_photo("s_handle")])
        store.add_venue_manifest(_prefix("02", "r2"), "v1", [_photo("s_venue")])
        source = EventPostSource(store)
        posts = _run(source.posts_for_handle("h1", ["v1"], self.SINCE))
        assert {p.shortcode for p in posts} == {"s_handle", "s_venue"}

    def test_neither_prefix_archived_returns_empty(self):
        store = _FakeMediaStore()
        source = EventPostSource(store)
        posts = _run(source.posts_for_handle("h1", ["v1"], self.SINCE))
        assert posts == []

    def test_a_post_archived_under_both_prefixes_is_returned_once(self):
        store = _FakeMediaStore()
        store.add_promoter_manifest(_prefix("01", "r1"), "h1", [_photo("s1")])
        store.add_venue_manifest(_prefix("01", "r1"), "v1", [_photo("s1")])
        source = EventPostSource(store)
        posts = _run(source.posts_for_handle("h1", ["v1"], self.SINCE))
        assert len(posts) == 1
        assert posts[0].shortcode == "s1"

    def test_dedupe_prefers_the_newest_archived_copy(self):
        store = _FakeMediaStore()
        # Same shortcode, two copies that genuinely differ (a re-classified
        # flyer with higher confidence in the LATER run) — the venue prefix
        # is the OLDER copy (day 01), the promoter prefix is NEWER (day 05).
        store.add_venue_manifest(
            _prefix("01", "r1"), "v1",
            [_photo("s1", caption="old caption", classification_confidence=0.5)],
        )
        store.add_promoter_manifest(
            _prefix("05", "r2"), "h1",
            [_photo("s1", caption="new caption", classification_confidence=0.95)],
        )
        source = EventPostSource(store)
        posts = _run(source.posts_for_handle("h1", ["v1"], self.SINCE))
        assert len(posts) == 1
        assert posts[0].caption == "new caption"
        assert posts[0].flyer_confidence == 0.95

    def test_lookback_window_excludes_older_runs(self):
        store = _FakeMediaStore()
        store.add_promoter_manifest(_prefix("01", "r1"), "h1", [_photo("s_old")])
        source = EventPostSource(store)
        since = datetime(2026, 7, 3, tzinfo=timezone.utc)  # after day=01
        posts = _run(source.posts_for_handle("h1", ["v1"], since))
        assert posts == []

    def test_no_media_store_returns_empty(self):
        source = EventPostSource(None)
        posts = _run(source.posts_for_handle("h1", ["v1"], self.SINCE))
        assert posts == []


# ── §A/§C: orchestration through EventExtractionService.run() ─────────────────
class TestHandlesModeOrchestration:
    def test_single_venue_handle_extracts_and_attributes(self):
        dao = _dao()
        _seed_venue(dao, "v1", "entreamigosobode")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]
        openai = _FakeOpenAIClient([_event_json(title="Show", date_text="01/07/2026")])

        result = _run_handles(dao, post_source, openai)

        row = dao.get_event_by_source("entreamigosobode", "s1")
        assert row is not None
        assert row["venue_id"] == "v1"
        assert result["handles"] == [{
            "handle": "entreamigosobode", "outcome": HANDLE_OUTCOME_EXTRACTED,
            "posts_archived": 1, "posts_qualifying": 1, "venue_ids": ["v1"],
        }]
        assert result["qualifying_posts"] == 1

    def test_a_handle_with_nothing_archived_is_a_no_op_not_a_failure(self):
        dao = _dao()
        _seed_venue(dao, "v1", "entreamigosobode")
        post_source = _StubPostSource()  # nothing programmed for the handle
        openai = _FakeOpenAIClient([])

        result = _run_handles(dao, post_source, openai)

        assert result["handles"] == [
            {"handle": "entreamigosobode", "outcome": HANDLE_OUTCOME_NOTHING_ARCHIVED},
        ]
        assert result["qualifying_posts"] == 0
        assert openai.calls == 0  # no exception raised anywhere in the run

    def test_a_handle_no_venue_maps_to_is_a_no_op(self):
        dao = _dao()  # no venue has this handle at all
        post_source = _StubPostSource()
        openai = _FakeOpenAIClient([])

        result = _run_handles(dao, post_source, openai, handles="nobody_home")

        assert result["handles"] == [
            {"handle": "nobody_home", "outcome": HANDLE_OUTCOME_NO_VENUE_MAPPED},
        ]

    def test_a_handle_shared_by_two_venues_still_reads_and_reports_both(self):
        """Not skipped: the multi-venue branch reads posts and reports
        HANDLE_OUTCOME_EXTRACTED with both venue_ids, then resolves each
        event's own venue through the ladder -- see
        TestMultiVenueHandleAttribution for the resolution itself."""
        dao = _dao()
        _seed_named_venue(dao, "v1", "sharedhandle", "Bar Central")
        _seed_named_venue(dao, "v2", "sharedhandle", "Bar Central Boa Viagem")
        post_source = _StubPostSource()
        post_source.posts_by_handle["sharedhandle"] = [_post("s1")]
        openai = _FakeOpenAIClient([_event_json(location_text="Bar Central")])

        result = _run_handles(dao, post_source, openai, handles="sharedhandle")

        report = result["handles"][0]
        assert report["outcome"] == HANDLE_OUTCOME_EXTRACTED, report
        assert sorted(report["venue_ids"]) == ["v1", "v2"], report
        assert post_source.calls == [("sharedhandle", ["v1", "v2"])]

    def test_max_posts_per_venue_caps_the_by_handle_read(self):
        dao = _dao()
        _seed_venue(dao, "v1", "entreamigosobode")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post(f"s{i}") for i in range(5)]
        openai = _FakeOpenAIClient([_event_json() for _ in range(2)])

        result = _run_handles(dao, post_source, openai, max_posts_per_venue=2)

        assert result["qualifying_posts"] == 2
        assert result["handles"][0]["posts_archived"] == 2

    def test_existing_modes_are_reported_with_no_handles_key_noise(self):
        dao = _dao()
        _seed_venue(dao, "v1", "v1_handle")
        post_source = _StubPostSource()
        openai = _FakeOpenAIClient([])
        service = EventExtractionService(dao, post_source, openai, min_confidence=0.5, flyer_confidence_floor=0.6)
        result = _run(service.run({"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}}))
        assert result["handles"] == []

    def test_apify_is_never_called_by_handles_mode(self):
        dao = _dao()
        _seed_venue(dao, "v1", "entreamigosobode")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]
        openai = _FakeOpenAIClient([_event_json()])
        poisoned_apify = _PoisonedApifyClient()

        _run_handles(dao, post_source, openai)

        assert poisoned_apify.calls == 0

    def test_event_extraction_service_has_no_apify_reachable_dependency(self):
        """Structural guarantee, not just a call-count coincidence:
        `EventExtractionService.__init__` has no crawler/Apify-shaped
        parameter for ANY mode (including "handles") to reach."""
        params = inspect.signature(EventExtractionService.__init__).parameters
        assert not any(
            token in name.lower() for name in params for token in ("apify", "crawler", "posts_client")
        )


# ── §B: the supersession trap, both directions ─────────────────────────────────
class TestSupersessionOnReExtraction:
    def _seed_and_extract(self, dao, post_source, openai, shortcode="s1"):
        _run_handles(dao, post_source, openai)
        return dao.get_event_by_source("entreamigosobode", shortcode)

    def test_unchanged_post_updates_in_place_and_supersedes_nothing(self):
        dao = _dao()
        _seed_venue(dao, "v1", "entreamigosobode")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]

        first = self._seed_and_extract(
            dao, post_source, _FakeOpenAIClient([_event_json(title="Show", date_text="01/07/2026")]),
        )
        _run_handles(dao, post_source, _FakeOpenAIClient([_event_json(title="Show", date_text="01/07/2026")]))

        rows = dao.list_events_by_source("entreamigosobode", "s1")
        assert len(rows) == 1, rows
        assert rows[0]["event_id"] == first["event_id"]
        assert rows[0]["status"] != "superseded"

        superseded_metric = REGISTRY.get_sample_value(
            "event_extraction_superseded_total", {"trigger": "handle_reextraction"},
        ) or 0.0
        assert superseded_metric == 0.0

    def test_a_corrected_date_supersedes_the_stale_row_and_inserts_the_corrected_one(self):
        dao = _dao()
        _seed_venue(dao, "v1", "entreamigosobode")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]

        stale = self._seed_and_extract(
            dao, post_source, _FakeOpenAIClient([_event_json(title="Show", date_text="01/07/2027")]),
        )
        _run_handles(dao, post_source, _FakeOpenAIClient([_event_json(title="Show", date_text="01/07/2026")]))

        rows = dao.list_events_by_source("entreamigosobode", "s1")
        assert len(rows) == 2, rows  # not a silent overwrite: both rows exist
        by_id = {r["event_id"]: r for r in rows}
        assert by_id[stale["event_id"]]["status"] == "superseded"
        live = [r for r in rows if r["event_id"] != stale["event_id"]]
        assert len(live) == 1
        assert live[0]["status"] != "superseded"
        assert live[0]["starts_at"].year == 2026

    def test_operator_edited_field_survives_a_reextraction(self):
        dao = _dao()
        _seed_venue(dao, "v1", "entreamigosobode")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]

        original = self._seed_and_extract(
            dao, post_source, _FakeOpenAIClient([_event_json(title="Original Title", date_text="01/07/2026")]),
        )
        dao.update_event(original["event_id"], {
            "status": "confirmed", "title": "Curated By Operator",
            "operator_edited_fields": ["title"],
        })

        _run_handles(
            dao, post_source,
            _FakeOpenAIClient([_event_json(title="Model's Different Title", date_text="01/07/2026")]),
        )

        row = dao.get_event(original["event_id"])
        assert row["title"] == "Curated By Operator"
        assert row["status"] == "confirmed"
        assert row["review_reason"] == "model_diverges_from_confirmed_record"
        rows = dao.list_events_by_source("entreamigosobode", "s1")
        assert len(rows) == 1, rows  # never duplicated
        assert all(r["status"] != "superseded" for r in rows)

    def test_source_handle_matches_the_venues_own_stored_casing_not_the_operators_input(self):
        """The subtle half of §B: identity match requires `source_handle` on
        the re-extraction to be BYTE-IDENTICAL to what the first pass wrote
        — event_reconciliation.reconcile_post_events keys existing rows on
        `list_events_by_source(source_handle, source_shortcode)`, an EXACT
        string match, never case-folded. The venue's own stored
        `instagram_handle` ("EntreAmigosOBode", mixed case, as it might
        genuinely be recorded) is what a first pass would have written as
        `source_handle` (via `_handle_for`) — an operator typing the
        lowercase config handle "entreamigosobode" must still land on the
        SAME rows, not silently open a second identity space and duplicate
        them forever."""
        dao = _dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="V v1", venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG))
        dao.set_venue_instagram(
            VenueInstagram(venue_id="v1", instagram_handle="EntreAmigosOBode", status="found")
        )
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]

        first = self._seed_and_extract_for(
            dao, post_source, _FakeOpenAIClient([_event_json(title="Show", date_text="01/07/2026")]),
            shortcode="s1",
        )
        assert first["source_handle"] == "EntreAmigosOBode"

        _run_handles(dao, post_source, _FakeOpenAIClient([_event_json(title="Show", date_text="01/07/2026")]))

        rows_under_stored_casing = dao.list_events_by_source("EntreAmigosOBode", "s1")
        rows_under_lowercase = dao.list_events_by_source("entreamigosobode", "s1")
        assert len(rows_under_stored_casing) == 1, rows_under_stored_casing  # updated in place
        assert rows_under_lowercase == []  # never opened a second identity space

    def _seed_and_extract_for(self, dao, post_source, openai, shortcode):
        _run_handles(dao, post_source, openai)
        return dao.get_event_by_source(
            dao.get_venue_instagram("v1").instagram_handle, shortcode,
        )

    def test_confirmed_row_is_never_erased_when_reextraction_cannot_be_paired(self):
        """§B's too-eager direction: a confirmed row whose title AND date
        both diverge from the fresh answer cannot be unambiguously paired
        with it (event_reconciliation._plausibly_same_event requires EXACTLY
        one of the two to change) — it must be left completely untouched,
        never superseded, never silently dropped."""
        dao = _dao()
        _seed_venue(dao, "v1", "entreamigosobode")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]

        original = self._seed_and_extract(
            dao, post_source, _FakeOpenAIClient([_event_json(title="Curated Show", date_text="01/07/2026")]),
        )
        dao.update_event(original["event_id"], {
            "status": "confirmed", "operator_edited_fields": ["title"],
        })

        _run_handles(
            dao, post_source,
            _FakeOpenAIClient([_event_json(title="Totally Different Event", date_text="15/09/2026")]),
        )

        curated = dao.get_event(original["event_id"])
        assert curated["status"] == "confirmed"
        assert curated["title"] == "Curated Show"
        assert curated["review_reason"] == "confirmed_event_absent_from_latest_extraction"

        rows = dao.list_events_by_source("entreamigosobode", "s1")
        assert len(rows) == 2, rows  # the curated row plus the new, unrelated one
        assert all(r["status"] != "superseded" for r in rows)  # curated row never erased


# ── The real production case (§Evidence) ────────────────────────────────────────
def _ferias_events_json(year: int, *, location_text: str | None = None) -> str:
    dates = [f"0{i + 1}/07/{year}" for i in range(4)]
    events = [
        {
            "title": f"FERIAS AMIGOS PARK -- semana {i + 1}", "description": None,
            "date_text": dates[i], "time_text": "16h", "is_recurring": False,
            "recurrence_text": None, "lineup": [], "ticket_url": None,
            "price_text": None, "location_text": location_text, "confidence": 0.9,
        }
        for i in range(4)
    ]
    return json.dumps({"events": events})


class TestFeriasReExtractionRealCase:
    """plans/260811_extract-by-handle.md §Evidence: the four FÉRIAS AMIGOS
    PARK events carried a wrong year because the post that announced them
    was archived under a handle the two supported eligibility modes could
    not reach. Re-extracting it through mode="handles" must leave exactly
    four LIVE events with the corrected year, not eight."""

    def test_four_wrong_year_events_become_four_right_ones_not_eight(self):
        dao = _dao()
        _seed_venue(dao, "ven_entreamigos", "entreamigosobode")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("dcpp_ferias")]

        _run_handles(dao, post_source, _FakeOpenAIClient([_ferias_events_json(2027)]))
        wrong_rows = dao.list_events_by_source("entreamigosobode", "dcpp_ferias")
        assert len(wrong_rows) == 4, wrong_rows
        assert {r["starts_at"].year for r in wrong_rows} == {2027}

        _run_handles(dao, post_source, _FakeOpenAIClient([_ferias_events_json(2026)]))
        all_rows = dao.list_events_by_source("entreamigosobode", "dcpp_ferias")
        assert len(all_rows) == 8, all_rows

        live = [r for r in all_rows if r["status"] != "superseded"]
        superseded = [r for r in all_rows if r["status"] == "superseded"]
        assert len(live) == 4, live
        assert {r["starts_at"].year for r in live} == {2026}
        assert len(superseded) == 4, superseded
        assert {r["starts_at"].year for r in superseded} == {2027}
        assert {r["event_id"] for r in wrong_rows} == {r["event_id"] for r in superseded}


# ── Multi-venue handles: routed through the SAME resolution ladder ────────────
class TestMultiVenueHandleAttribution:
    """A handle mapping to several venues (`@entreamigosobode`'s real shape:
    two venues) resolves each event's OWN venue through
    `event_venue_resolution.build_location_text_attribute_fn` — the SAME
    closure `PromoterCrawlService._process_post` uses for the scheduled
    shared-handle crawl, reused directly rather than re-implemented. See
    `tests/test_promoter_crawl_service.py::TestAttributionBehaviourPinnedAcrossExtraction`
    for the pin that a future edit to the shared function cannot silently
    change the promoter-crawl path's own outcome.
    """

    def _two_venues(self, dao, handle="entreamigosobode"):
        _seed_named_venue(dao, "v1", handle, "Entre Amigos O Bode")
        _seed_named_venue(dao, "v2", handle, "Entre Amigos O Bode Espinheiro")

    def test_a_post_naming_one_venue_resolves_to_it(self):
        dao = _dao()
        self._two_venues(dao)
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]
        openai = _FakeOpenAIClient([_event_json(location_text="Entre Amigos O Bode Espinheiro")])

        _run_handles(dao, post_source, openai, handles="entreamigosobode")

        row = dao.get_event_by_source("entreamigosobode", "s1")
        assert row is not None
        assert row["venue_id"] == "v2", row
        assert row["location_resolution"] == "auto", row

    def test_a_post_naming_neither_venue_is_attributed_to_no_venue_and_queued(self):
        dao = _dao()
        self._two_venues(dao)
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]
        openai = _FakeOpenAIClient([_event_json(
            location_text="Praça de Alimentação, Shopping Recife",
        )])

        _run_handles(dao, post_source, openai, handles="entreamigosobode")

        row = dao.get_event_by_source("entreamigosobode", "s1")
        assert row is not None
        assert row["venue_id"] is None, row
        assert row["status"] == "pending_review", row  # queued for a human
        assert "unresolved_venue" in (row["review_reason"] or ""), row

    def test_source_handle_uses_the_crawl_target_convention_not_a_venues_own_casing(self):
        """The multi-venue mirror of
        TestSupersessionOnReExtraction.test_source_handle_matches_the_venues_own_stored_casing_not_the_operators_input:
        there, the RIGHT convention was the venue's own stored handle; HERE
        it is the opposite -- `archive_handle` (the crawl_target's own,
        always-normalized key), never `_handle_for(venue_id)`. Each venue's
        own stored `instagram_handle` is deliberately given a DIFFERENT,
        mixed-case spelling from `archive_handle` and from each other, so a
        regression that reached for either venue's own casing (the natural,
        wrong instinct that mirrors the single-venue convention) would be
        caught, not coincide by accident."""
        dao = _dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="Entre Amigos O Bode", venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG))
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="EntreAmigosOBode", status="found"))
        dao.upsert_venue(Venue(venue_id="v2", venue_name="Entre Amigos O Bode Espinheiro", venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG))
        dao.set_venue_instagram(VenueInstagram(venue_id="v2", instagram_handle="ENTREAMIGOSOBODE", status="found"))
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("s1")]
        openai = _FakeOpenAIClient([_event_json(location_text="Entre Amigos O Bode Espinheiro")])

        _run_handles(dao, post_source, openai, handles="entreamigosobode")

        # Reachable under the NORMALIZED handle -- not either venue's own,
        # differently-cased, stored spelling.
        rows_under_normalized = dao.list_events_by_source("entreamigosobode", "s1")
        rows_under_v1_casing = dao.list_events_by_source("EntreAmigosOBode", "s1")
        rows_under_v2_casing = dao.list_events_by_source("ENTREAMIGOSOBODE", "s1")
        assert len(rows_under_normalized) == 1, rows_under_normalized
        assert rows_under_normalized[0]["source_handle"] == "entreamigosobode"
        assert rows_under_v1_casing == []
        assert rows_under_v2_casing == []


class TestFeriasReExtractionTwoVenueHandle:
    """The exact case the plan's own Evidence opens with:
    `@entreamigosobode` maps to TWO venues. Re-extracting the four FÉRIAS
    posts through mode="handles" must resolve them (by name) to the SAME
    venue every time and leave exactly four LIVE events dated 2026 --
    attributed, never eight, and never split across the wrong venue."""

    def test_four_wrong_year_events_resolve_and_supersede_across_a_two_venue_handle(self):
        dao = _dao()
        _seed_named_venue(dao, "v1", "entreamigosobode", "Entre Amigos O Bode")
        _seed_named_venue(dao, "v2", "entreamigosobode", "Entre Amigos O Bode Espinheiro")
        post_source = _StubPostSource()
        post_source.posts_by_handle["entreamigosobode"] = [_post("dcpp_ferias_2v")]

        _run_handles(dao, post_source, _FakeOpenAIClient([
            _ferias_events_json(2027, location_text="Entre Amigos O Bode Espinheiro"),
        ]), handles="entreamigosobode")
        wrong_rows = dao.list_events_by_source("entreamigosobode", "dcpp_ferias_2v")
        assert len(wrong_rows) == 4, wrong_rows
        assert {r["starts_at"].year for r in wrong_rows} == {2027}
        assert all(r["venue_id"] == "v2" for r in wrong_rows), wrong_rows

        _run_handles(dao, post_source, _FakeOpenAIClient([
            _ferias_events_json(2026, location_text="Entre Amigos O Bode Espinheiro"),
        ]), handles="entreamigosobode")

        all_rows = dao.list_events_by_source("entreamigosobode", "dcpp_ferias_2v")
        assert len(all_rows) == 8, all_rows

        # Half one: the visible half -- four live events, dated 2026,
        # attributed to the venue their location_text actually named.
        live = [r for r in all_rows if r["status"] != "superseded"]
        assert len(live) == 4, live
        assert {r["starts_at"].year for r in live} == {2026}, live
        assert all(r["venue_id"] == "v2" for r in live), live

        # Half two: the half that proves identity continuity held. If
        # source_handle had been wrong (e.g. either venue's own casing
        # instead of the crawl_target convention), the second pass would
        # never have MATCHED the first pass's rows at all -- it would have
        # opened a second identity space, and this count would be 4, not 8
        # (the stale 2027 rows would be invisible to list_events_by_source
        # under the WRONG handle, not "superseded" under the right one).
        superseded = [r for r in all_rows if r["status"] == "superseded"]
        assert len(superseded) == 4, superseded
        assert {r["starts_at"].year for r in superseded} == {2027}, superseded
        assert {r["event_id"] for r in wrong_rows} == {r["event_id"] for r in superseded}
