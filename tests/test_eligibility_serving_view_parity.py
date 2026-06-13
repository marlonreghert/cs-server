"""Parity guard for the eligibility serving view (SQL view <-> evaluate()).

The whole plan rests on the serving.eligible_venue SQL view applying the SAME
eligibility predicate as app/services/venue_eligibility.evaluate(). This test runs
the documented eligibility fixtures through BOTH list_servable_venue_ids() and
evaluate() and asserts agreement, for the fake store (always) AND the real
SQL store (only when RDS_TEST_URL points at a migrated scratch Postgres — there is
no local Postgres in CI). Offline this pins the fake mirror to evaluate(); the
real-store run is what actually validates the SQL.

It also guards the seeded admin.category_good_type table against drift from the
Python resolve_category maps (the seed source).
"""
import os
import uuid

import pytest

from app.models import Venue
from app.models.venue_category import (
    _BESTTIME_TO_CATEGORY,
    _GOOGLE_TO_CATEGORY,
    resolve_category,
)
from app.services.venue_eligibility import EligibilityConfig, evaluate
from tests.rds_fake import InMemoryRdsVenueStore

_VA = "google_places.vibe_attributes"


def _store_kinds():
    kinds = ["fake"]
    if os.environ.get("RDS_TEST_URL"):
        kinds.append("rds")
    return kinds


@pytest.fixture(params=_store_kinds())
def store(request):
    if request.param == "fake":
        return InMemoryRdsVenueStore()
    from app.dao.rds_venue_store import RdsVenueStore
    return RdsVenueStore(os.environ["RDS_TEST_URL"])


def _vid() -> str:
    return f"pv_{uuid.uuid4().hex[:12]}"


# (label, venue_name, besttime_type, google_type, expected_eligible)
# Every branch of evaluate(): empty name, blocked google/besttime, hard/ambiguous
# keyword +/- good-category, labeled vs unlabeled, and plain-eligible.
_FIXTURES = [
    ("empty_name",            "",                  None,   None,             False),
    ("blocked_google_type",   "Drogasil",          None,   "drugstore",      False),
    ("blocked_besttime_type", "Some Parish",       "CHURCH", None,           False),
    ("hard_kw_no_category",   "Drogaria Central",  None,   None,             False),
    ("hard_kw_good_google",   "Farmácia Bar",      None,   "bar",            True),
    ("ambig_kw_unlabeled",    "Bar da Praça",      None,   None,             True),
    ("ambig_kw_labeled_bad",  "Mercado Central",   None,   "amusement_park", False),
    ("ambig_kw_good_besttime","Bar do Mercado",    "BAR",  None,             True),
    ("plain_bar",             "Boteco do Zé",      "BAR",  None,             True),
    ("unlabeled_unknown",     "Cantina XYZ",       None,   None,             True),
]


def _seed(store, vid, name, btype, gtype):
    store.upsert_venue(Venue(
        venue_id=vid, venue_name=name, venue_address="a",
        venue_lat=-8.05, venue_lng=-34.88, venue_type=btype,
    ))
    if gtype is not None:
        store.upsert_enrichment(
            _VA, vid, {"venue_id": vid, "google_primary_type": gtype, "google_place_id": "p"},
            history=False, promoted={"google_primary_type": gtype, "google_place_id": "p"},
        )


@pytest.mark.parametrize("label,name,btype,gtype,expected", _FIXTURES, ids=[f[0] for f in _FIXTURES])
def test_view_matches_evaluate(store, label, name, btype, gtype, expected):
    # The reference: evaluate() with the default block-list. The real view reads
    # the migration-seeded default rules (equivalent to defaults); the fake derives
    # defaults from its empty rule set.
    reference_eligible = not evaluate(name, btype, gtype, EligibilityConfig.defaults()).soft_deletable
    assert reference_eligible is expected, f"fixture {label} mislabeled vs evaluate()"

    vid = _vid()
    _seed(store, vid, name, btype, gtype)
    servable = set(store.list_servable_venue_ids())
    assert (vid in servable) is expected, (
        f"{label}: view says servable={vid in servable}, expected {expected}"
    )


def test_view_excludes_deprecated_regardless_of_eligibility(store):
    vid = _vid()
    _seed(store, vid, "Boteco do Zé", "BAR", None)   # eligible if active
    store.soft_delete_venue(vid, "google_places_closed_permanently", "google_places")
    assert vid not in set(store.list_servable_venue_ids())


# ── good-type table drift guard (the seed source == resolve_category non-OTHER) ──
def test_good_type_seed_matches_resolve_category():
    """admin.category_good_type is seeded from the resolve_category map keys; assert
    those keys are EXACTLY the tokens resolve_category classifies as non-OTHER, so
    the EXISTS lookup in the view is equivalent to `_has_good_category`."""
    google_seed = set(_GOOGLE_TO_CATEGORY)
    besttime_seed = set(_BESTTIME_TO_CATEGORY)

    google_nonother = {t for t in _GOOGLE_TO_CATEGORY if resolve_category(google_type=t) != "OTHER"}
    besttime_nonother = {t for t in _BESTTIME_TO_CATEGORY if resolve_category(besttime_type=t) != "OTHER"}

    assert google_seed == google_nonother
    assert besttime_seed == besttime_nonother


@pytest.mark.skipif(not os.environ.get("RDS_TEST_URL"), reason="requires migrated Postgres")
def test_seeded_good_type_table_matches_maps():
    """When run against the migrated scratch DB, the persisted admin.category_good_type
    rows must equal the resolve_category map keys (google lower, besttime upper)."""
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["RDS_TEST_URL"], future=True)
    with engine.connect() as conn:
        rows = {(r[0], r[1]) for r in conn.execute(
            text("SELECT token, kind FROM admin.category_good_type")
        )}
    expected = {(t.lower(), "google") for t in _GOOGLE_TO_CATEGORY}
    expected |= {(t.upper(), "besttime") for t in _BESTTIME_TO_CATEGORY}
    assert rows == expected
