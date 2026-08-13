"""Unit tests for scripts/backfill_source_provenance.py.

See plans/260813_backfill-source-provenance.md's Test Plan.

`tests/bdd/enrichment/backfill-source-provenance.feature` covers the same
behavior end-to-end through the real `run_backfill` against the in-memory
RDS fake and a deterministic manifest-reader double; this file adds two more
granular layers:
  - `manifest_key_for_cover_photo_key`/`find_manifest_entry`/`decide_one`
    exercised as PURE functions (no DAO, no reader implementation at all —
    a plain dict-backed stub) for every disposition branch, including the
    ones the BDD feature does not enumerate one-by-one (unparseable,
    matched-no-data, media type passthrough, a shortcode shared by several
    manifest entries);
  - `run_backfill`/`check_balance` against `tests.rds_fake.
    InMemoryRdsVenueStore` for resumability, idempotency, and the
    write-failure/arithmetic hard-stops.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.dao.venue_repository import VenueRepository
from scripts.backfill_source_provenance import (
    ArithmeticImbalance,
    DISPOSITION_ALREADY_PRESENT,
    DISPOSITION_FILLED,
    DISPOSITION_MATCHED_NO_DATA,
    DISPOSITION_UNMATCHED,
    DISPOSITION_UNPARSEABLE,
    ManifestNotFound,
    Report,
    UNMATCHED_MANIFEST_NOT_FOUND,
    UNMATCHED_MANIFEST_UNREADABLE,
    UNMATCHED_NO_COVER_PHOTO_KEY,
    UNMATCHED_NO_ENTRY_FOR_SHORTCODE,
    UNMATCHED_NO_SHORTCODE,
    UNMATCHED_UNRECOGNIZED_KEY_SHAPE,
    WriteAffectedNoRows,
    build_manifest_reader,
    check_balance,
    decide_one,
    find_manifest_entry,
    manifest_key_for_cover_photo_key,
    run_backfill,
)
from tests.rds_fake import InMemoryRdsVenueStore

_CPK_VENUE = (
    "retrieved/source=instagram_posts/year=2026/month=08/day=12/"
    "run_id=01ABCDEFGH/venue_id=v1/media/uncategorised/sc1.jpg"
)
_CPK_PROMOTER = (
    "retrieved/source=instagram_posts/year=2026/month=08/day=12/"
    "run_id=01ABCDEFGH/promoter=somehandle/media/uncategorised/sc1.jpg"
)


class _DictReader:
    """A minimal, dict-backed double for ArchiveManifestReader — no
    boto3 client anywhere in these tests. Raises `ManifestNotFound` for an
    absent key (mirroring the real reader's contract) and re-raises
    anything registered under `broken`."""

    def __init__(self, manifests: dict | None = None, broken: set | None = None):
        self.manifests = manifests or {}
        self.broken = broken or set()
        self.calls: list[str] = []

    def get(self, key: str) -> dict:
        self.calls.append(key)
        if key in self.broken:
            raise RuntimeError("simulated transport error")
        if key not in self.manifests:
            raise ManifestNotFound(key)
        return self.manifests[key]


def _source_row(
    *, source_id="evsrc_1", event_id="evt_1", shortcode="sc1",
    cover_photo_key=_CPK_VENUE, uploaded_at=None, media_type=None,
) -> dict:
    return {
        "id": source_id, "event_id": event_id, "source_shortcode": shortcode,
        "cover_photo_key": cover_photo_key,
        "source_uploaded_at": uploaded_at, "source_media_type": media_type,
    }


# ── manifest_key_for_cover_photo_key ──────────────────────────────────────────
class TestManifestKeyForCoverPhotoKey:
    def test_venue_id_layout(self):
        assert manifest_key_for_cover_photo_key(_CPK_VENUE) == (
            "retrieved/source=instagram_posts/year=2026/month=08/day=12/"
            "run_id=01ABCDEFGH/venue_id=v1/info/_manifest.json"
        )

    def test_promoter_layout(self):
        assert manifest_key_for_cover_photo_key(_CPK_PROMOTER) == (
            "retrieved/source=instagram_posts/year=2026/month=08/day=12/"
            "run_id=01ABCDEFGH/promoter=somehandle/info/_manifest.json"
        )

    def test_none_input(self):
        assert manifest_key_for_cover_photo_key(None) is None

    def test_empty_string(self):
        assert manifest_key_for_cover_photo_key("") is None

    def test_unrecognized_shape(self):
        assert manifest_key_for_cover_photo_key("some/other/key/with/no/entity.jpg") is None


# ── find_manifest_entry ────────────────────────────────────────────────────────
class TestFindManifestEntry:
    def test_finds_matching_shortcode(self):
        manifest = {"photos": [{"shortcode": "a"}, {"shortcode": "b", "uploaded_at": "x"}]}
        assert find_manifest_entry(manifest, "b") == {"shortcode": "b", "uploaded_at": "x"}

    def test_no_match_returns_none(self):
        manifest = {"photos": [{"shortcode": "a"}]}
        assert find_manifest_entry(manifest, "zzz") is None

    def test_empty_manifest(self):
        assert find_manifest_entry({}, "a") is None
        assert find_manifest_entry(None, "a") is None

    def test_no_shortcode_returns_none(self):
        manifest = {"photos": [{"shortcode": "a"}]}
        assert find_manifest_entry(manifest, None) is None
        assert find_manifest_entry(manifest, "") is None

    def test_shortcode_shared_by_several_entries_takes_the_first(self):
        """A carousel's child images share one shortcode within ONE
        manifest — plan §A: 'take the record that produced this source row,
        and say how you decide'. Every entry for the same shortcode carries
        identical post-level facts by construction (the same post's
        uploaded_at/post_type, copied onto every child image), so the first
        match is taken deterministically and never loses information — this
        test pins that both the CHOICE (first) and the CONSEQUENCE (either
        would have answered identically) hold."""
        manifest = {"photos": [
            {"shortcode": "carousel1", "uploaded_at": "2026-08-01T10:00:00.000Z", "post_type": "Sidecar"},
            {"shortcode": "carousel1", "uploaded_at": "2026-08-01T10:00:00.000Z", "post_type": "Sidecar"},
        ]}
        entry = find_manifest_entry(manifest, "carousel1")
        assert entry is manifest["photos"][0]
        assert entry == manifest["photos"][1]  # equal in substance either way


# ── decide_one ────────────────────────────────────────────────────────────────
class TestDecideOne:
    def test_already_present_skips_the_reader_entirely(self):
        """Both columns already set -- decide_one must not even attempt the
        join. A reader whose `.get` raises proves it: any call would fail
        this test."""
        class _ExplodingReader:
            def get(self, key):
                raise AssertionError("must not read a manifest for an already-present row")

        row = _source_row(
            uploaded_at=datetime(2025, 1, 1, tzinfo=timezone.utc), media_type="Image",
        )
        decision = decide_one(row, _ExplodingReader())
        assert decision.action == DISPOSITION_ALREADY_PRESENT
        assert decision.new_uploaded_at == row["source_uploaded_at"]
        assert decision.new_media_type == "Image"

    def test_no_shortcode_is_unmatched(self):
        row = _source_row(shortcode=None)
        decision = decide_one(row, _DictReader())
        assert decision.action == DISPOSITION_UNMATCHED
        assert decision.unmatched_reason == UNMATCHED_NO_SHORTCODE

    def test_no_cover_photo_key_is_unmatched(self):
        row = _source_row(cover_photo_key=None)
        decision = decide_one(row, _DictReader())
        assert decision.action == DISPOSITION_UNMATCHED
        assert decision.unmatched_reason == UNMATCHED_NO_COVER_PHOTO_KEY

    def test_unrecognized_key_shape_is_unmatched(self):
        row = _source_row(cover_photo_key="not/a/recognisable/key.jpg")
        decision = decide_one(row, _DictReader())
        assert decision.action == DISPOSITION_UNMATCHED
        assert decision.unmatched_reason == UNMATCHED_UNRECOGNIZED_KEY_SHAPE

    def test_manifest_not_found_is_unmatched(self):
        row = _source_row()
        decision = decide_one(row, _DictReader(manifests={}))
        assert decision.action == DISPOSITION_UNMATCHED
        assert decision.unmatched_reason == UNMATCHED_MANIFEST_NOT_FOUND

    def test_manifest_unreadable_is_a_distinct_unmatched_reason(self):
        """A transport/parse error must not be folded into 'not found' --
        plan Error Handling: 'a single unreadable manifest must not abort
        the run -- log, count, continue'. Distinct reason so an operator can
        tell 'archive genuinely has nothing here' from 'something is
        actually broken'."""
        key = manifest_key_for_cover_photo_key(_CPK_VENUE)
        row = _source_row()
        decision = decide_one(row, _DictReader(broken={key}))
        assert decision.action == DISPOSITION_UNMATCHED
        assert decision.unmatched_reason == UNMATCHED_MANIFEST_UNREADABLE

    def test_no_entry_for_shortcode_is_unmatched(self):
        key = manifest_key_for_cover_photo_key(_CPK_VENUE)
        row = _source_row(shortcode="sc1")
        reader = _DictReader(manifests={key: {"photos": [{"shortcode": "other"}]}})
        decision = decide_one(row, reader)
        assert decision.action == DISPOSITION_UNMATCHED
        assert decision.unmatched_reason == UNMATCHED_NO_ENTRY_FOR_SHORTCODE

    def test_fills_both_fields_from_a_matched_entry(self):
        key = manifest_key_for_cover_photo_key(_CPK_VENUE)
        row = _source_row()
        reader = _DictReader(manifests={key: {"photos": [
            {"shortcode": "sc1", "uploaded_at": "2026-08-12T15:26:00.000Z", "post_type": "Video"},
        ]}})
        decision = decide_one(row, reader)
        assert decision.action == DISPOSITION_FILLED
        assert decision.new_uploaded_at == datetime(2026, 8, 12, 15, 26, tzinfo=timezone.utc)
        assert decision.new_media_type == "Video"
        assert decision.write_fields() == {
            "source_uploaded_at": datetime(2026, 8, 12, 15, 26, tzinfo=timezone.utc),
            "source_media_type": "Video",
        }

    def test_never_overwrites_an_existing_media_type_even_when_only_uploaded_at_is_missing(self):
        key = manifest_key_for_cover_photo_key(_CPK_VENUE)
        row = _source_row(media_type="Image")  # uploaded_at still None
        reader = _DictReader(manifests={key: {"photos": [
            # Manifest disagrees -- must not matter, the column is already set.
            {"shortcode": "sc1", "uploaded_at": "2026-08-12T15:26:00.000Z", "post_type": "Video"},
        ]}})
        decision = decide_one(row, reader)
        assert decision.action == DISPOSITION_FILLED  # uploaded_at DID fill
        assert decision.new_uploaded_at == datetime(2026, 8, 12, 15, 26, tzinfo=timezone.utc)
        assert decision.new_media_type == "Image"  # untouched

    def test_media_type_passes_through_verbatim_including_unrecognised_value(self):
        """No canonicalisation, no allow-list -- migration 0036's own
        docstring: `source_media_type` stores Apify's item type verbatim."""
        key = manifest_key_for_cover_photo_key(_CPK_VENUE)
        row = _source_row()
        reader = _DictReader(manifests={key: {"photos": [
            {"shortcode": "sc1", "post_type": "SomeFutureAPifyType"},
        ]}})
        decision = decide_one(row, reader)
        assert decision.new_media_type == "SomeFutureAPifyType"

    def test_absent_uploaded_at_leaves_it_null_and_is_not_unparseable(self):
        key = manifest_key_for_cover_photo_key(_CPK_VENUE)
        row = _source_row()
        reader = _DictReader(manifests={key: {"photos": [{"shortcode": "sc1"}]}})
        decision = decide_one(row, reader)
        assert decision.new_uploaded_at is None
        assert decision.action == DISPOSITION_MATCHED_NO_DATA

    def test_empty_uploaded_at_leaves_it_null_and_is_not_unparseable(self):
        key = manifest_key_for_cover_photo_key(_CPK_VENUE)
        row = _source_row()
        reader = _DictReader(manifests={key: {"photos": [
            {"shortcode": "sc1", "uploaded_at": ""},
        ]}})
        decision = decide_one(row, reader)
        assert decision.new_uploaded_at is None
        assert decision.action == DISPOSITION_MATCHED_NO_DATA

    def test_malformed_uploaded_at_is_unparseable_and_stays_null(self):
        key = manifest_key_for_cover_photo_key(_CPK_VENUE)
        row = _source_row()
        reader = _DictReader(manifests={key: {"photos": [
            {"shortcode": "sc1", "uploaded_at": "not-a-real-timestamp"},
        ]}})
        decision = decide_one(row, reader)
        assert decision.new_uploaded_at is None
        assert decision.action == DISPOSITION_UNPARSEABLE

    def test_never_substitutes_first_seen_or_now(self):
        """decide_one never even receives first_seen_at/now -- this test
        pins that the function signature itself makes the substitution
        impossible, not merely that a particular fixture avoids it."""
        import inspect

        params = inspect.signature(decide_one).parameters
        assert "first_seen_at" not in params
        assert "now" not in params


# ── check_balance ────────────────────────────────────────────────────────────
def test_check_balance_raises_on_mismatch():
    report = Report(selected=5)
    report.dispositions[DISPOSITION_FILLED] = 2
    report.dispositions[DISPOSITION_ALREADY_PRESENT] = 1
    with pytest.raises(ArithmeticImbalance):
        check_balance(report)
    assert report.balanced is False


def test_check_balance_passes_when_totals_match():
    report = Report(selected=3)
    report.dispositions[DISPOSITION_FILLED] = 1
    report.dispositions[DISPOSITION_UNMATCHED] = 2
    check_balance(report)
    assert report.balanced is True


# ── run_backfill / idempotency / resumability (against the in-memory RDS fake) ─
def _insert(dao, *, source_id_hint, shortcode, uploaded_at=None, media_type=None,
            cover_photo_key=None):
    n = source_id_hint
    cover_photo_key = cover_photo_key or (
        f"retrieved/source=instagram_posts/year=2026/month=08/day=12/"
        f"run_id=01RUN{n:06d}/venue_id=v1/media/uncategorised/{shortcode}.jpg"
    )
    dao.insert_event({
        "event_id": f"evt_{n}", "venue_id": None, "title": f"t{n}", "status": "accepted",
        "source_kind": "venue_post", "source_handle": "h", "source_shortcode": shortcode,
        "source_permalink": "p", "cover_photo_key": cover_photo_key,
        "source_media_type": media_type, "source_uploaded_at": uploaded_at,
        "raw_extraction": None,
        "first_seen_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "last_seen_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
    })
    return cover_photo_key


class TestRunBackfill:
    def _dao(self):
        store = InMemoryRdsVenueStore()
        return VenueRepository(client=None, rds_store=store)

    def test_idempotent_second_pass_is_an_empty_change_set(self):
        dao = self._dao()
        cpk = _insert(dao, source_id_hint=1, shortcode="sc1")
        key = manifest_key_for_cover_photo_key(cpk)
        reader = _DictReader(manifests={key: {"photos": [
            {"shortcode": "sc1", "uploaded_at": "2026-08-01T10:00:00.000Z", "post_type": "Image"},
        ]}})

        first = run_backfill(dao, reader, apply=True)
        assert first.filled_count == 1

        second = run_backfill(dao, reader, apply=True)
        assert second.filled_count == 0
        assert second.dispositions.get(DISPOSITION_ALREADY_PRESENT, 0) == 1

    def test_dry_run_writes_nothing(self):
        dao = self._dao()
        cpk = _insert(dao, source_id_hint=1, shortcode="sc1")
        key = manifest_key_for_cover_photo_key(cpk)
        reader = _DictReader(manifests={key: {"photos": [
            {"shortcode": "sc1", "uploaded_at": "2026-08-01T10:00:00.000Z", "post_type": "Image"},
        ]}})
        report = run_backfill(dao, reader, apply=False)
        assert report.filled_count == 1  # disposition still reports what WOULD fill
        row = dao.get_event("evt_1")
        assert row["source_uploaded_at"] is None
        assert row["source_media_type"] is None

    def test_since_id_resumes_only_later_rows(self):
        dao = self._dao()
        _insert(dao, source_id_hint=1, shortcode="sc1")
        _insert(dao, source_id_hint=2, shortcode="sc2")
        all_sources = dao.list_all_event_sources()
        assert len(all_sources) == 2
        first_id = all_sources[0]["id"]

        reader = _DictReader()  # nothing matches -- fine, only selection matters here
        report = run_backfill(dao, reader, apply=False, since_id=first_id)
        assert report.selected == 1

    def test_write_failure_raises_and_carries_the_partial_report(self):
        dao = self._dao()
        cpk = _insert(dao, source_id_hint=1, shortcode="sc1")
        key = manifest_key_for_cover_photo_key(cpk)
        reader = _DictReader(manifests={key: {"photos": [
            {"shortcode": "sc1", "uploaded_at": "2026-08-01T10:00:00.000Z", "post_type": "Image"},
        ]}})
        dao.update_event_source_provenance = lambda *a, **kw: False  # simulate row vanished

        with pytest.raises(WriteAffectedNoRows) as excinfo:
            run_backfill(dao, reader, apply=True)
        assert excinfo.value.report is not None
        assert excinfo.value.report.selected == 1

    def test_never_overwrites_across_a_real_dao_round_trip(self):
        dao = self._dao()
        existing_ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
        cpk = _insert(
            dao, source_id_hint=1, shortcode="sc1",
            uploaded_at=existing_ts, media_type="Image",
        )
        key = manifest_key_for_cover_photo_key(cpk)
        reader = _DictReader(manifests={key: {"photos": [
            {"shortcode": "sc1", "uploaded_at": "2026-08-12T15:26:00.000Z", "post_type": "Video"},
        ]}})
        report = run_backfill(dao, reader, apply=True)
        assert report.dispositions.get(DISPOSITION_ALREADY_PRESENT, 0) == 1
        assert report.filled_count == 0
        row = dao.get_event("evt_1")
        assert row["source_uploaded_at"] == existing_ts
        assert row["source_media_type"] == "Image"


# ── build_manifest_reader ──────────────────────────────────────────────────────
class _StubSettings:
    def __init__(self, *, media_archive_bucket="", datalake_bucket=""):
        self.media_archive_bucket = media_archive_bucket
        self.datalake_bucket = datalake_bucket
        self.datalake_region = "us-east-1"
        self.datalake_access_key_id = ""
        self.datalake_secret_access_key = ""


def test_build_manifest_reader_returns_none_without_a_bucket():
    assert build_manifest_reader(_StubSettings()) is None


def test_build_manifest_reader_falls_back_to_datalake_bucket(monkeypatch):
    """Mirrors app.container's own `media_archive_bucket or datalake_bucket`
    fallback -- this script reads the SAME archive that fallback names, so
    it must resolve the bucket the same way. No real boto3 client is built:
    `_build_s3_client` is monkeypatched so this test never touches AWS."""
    import app.dao.datalake_writer as datalake_writer

    captured = {}

    def _fake_build_s3_client(*, region, access_key_id, secret_access_key):
        captured["region"] = region
        return object()

    monkeypatch.setattr(datalake_writer, "_build_s3_client", _fake_build_s3_client)
    reader = build_manifest_reader(_StubSettings(datalake_bucket="my-lake-bucket"))
    assert reader is not None
    assert reader.bucket == "my-lake-bucket"
    assert captured["region"] == "us-east-1"
