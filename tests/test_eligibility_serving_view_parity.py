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
from app.services.venue_eligibility import (
    DEFAULT_GEO_FENCE,
    EligibilityConfig,
    evaluate,
    geo_excluded,
)
from tests.rds_fake import InMemoryRdsVenueStore

_VA = "google_places.vibe_attributes"

# Inside the default fence (the 40 km Recife circle); outside every circle
# (São Paulo).
_IN_LAT, _IN_LNG = -8.05, -34.88
_OUT_LAT, _OUT_LNG = -23.55, -46.63


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


# (label, venue_name, besttime_type, google_type, lat, lng, expected_eligible)
# Every branch of evaluate(): empty name, blocked google/besttime, hard/ambiguous
# keyword +/- good-category, labeled vs unlabeled, plain-eligible — PLUS the geo
# dimension: an eligible venue outside every fence circle is excluded; missing
# coords are fail-open (still servable). Serving membership = (not
# soft_deletable) AND (not geo_excluded); the two axes are orthogonal, so the
# geo fixtures pair plain-eligible names with out-of-fence / null coords.
_FIXTURES = [
    ("empty_name",            "",                  None,   None,             _IN_LAT,  _IN_LNG,  False),
    ("blocked_google_type",   "Drogasil",          None,   "drugstore",      _IN_LAT,  _IN_LNG,  False),
    ("blocked_besttime_type", "Some Parish",       "CHURCH", None,           _IN_LAT,  _IN_LNG,  False),
    ("hard_kw_no_category",   "Drogaria Central",  None,   None,             _IN_LAT,  _IN_LNG,  False),
    ("hard_kw_good_google",   "Farmácia Bar",      None,   "bar",            _IN_LAT,  _IN_LNG,  True),
    ("ambig_kw_unlabeled",    "Bar da Praça",      None,   None,             _IN_LAT,  _IN_LNG,  True),
    ("ambig_kw_labeled_bad",  "Mercado Central",   None,   "amusement_park", _IN_LAT,  _IN_LNG,  False),
    ("ambig_kw_good_besttime","Bar do Mercado",    "BAR",  None,             _IN_LAT,  _IN_LNG,  True),
    ("plain_bar",             "Boteco do Zé",      "BAR",  None,             _IN_LAT,  _IN_LNG,  True),
    ("unlabeled_unknown",     "Cantina XYZ",       None,   None,             _IN_LAT,  _IN_LNG,  True),
    # ── geo dimension ────────────────────────────────────────────────────────
    ("plain_bar_outside_fence", "Boteco Fora",     "BAR",  None,             _OUT_LAT, _OUT_LNG, False),
    ("good_google_outside",   "Bar Paulista",      None,   "bar",            _OUT_LAT, _OUT_LNG, False),
    ("plain_bar_no_coords",   "Boteco Sem Coord",  "BAR",  None,             None,     None,     True),
]


def _seed(store, vid, name, btype, gtype, lat=_IN_LAT, lng=_IN_LNG):
    # The Venue model requires float coords; venues.address.lat/lng can be NULL. To
    # seed a coord-less venue, upsert placeholder coords then null the address row
    # (mirrors the real LEFT JOIN yielding NULL lat/lng — fail-open).
    store.upsert_venue(Venue(
        venue_id=vid, venue_name=name, venue_address="a",
        venue_lat=lat if lat is not None else 0.0,
        venue_lng=lng if lng is not None else 0.0,
        venue_type=btype,
    ))
    if lat is None or lng is None:
        addr = store.get_address(vid)
        if addr is not None:
            addr["lat"] = lat
            addr["lng"] = lng
    if gtype is not None:
        store.upsert_enrichment(
            _VA, vid, {"venue_id": vid, "google_primary_type": gtype, "google_place_id": "p"},
            history=False, promoted={"google_primary_type": gtype, "google_place_id": "p"},
        )


@pytest.mark.parametrize(
    "label,name,btype,gtype,lat,lng,expected", _FIXTURES, ids=[f[0] for f in _FIXTURES]
)
def test_view_matches_evaluate(store, label, name, btype, gtype, lat, lng, expected):
    # venues.address.lat/lng are NOT NULL in real Postgres, so a coord-less venue
    # cannot be represented there — the view's `lat IS NULL` fail-open branch is a
    # defensive path (real data always has coords). Exercise it on the fake only.
    if (lat is None or lng is None) and hasattr(store, "engine"):
        pytest.skip("missing-coords fixture is fake-store only (address lat/lng NOT NULL in RDS)")

    # The reference is the FULL serving predicate: (not soft_deletable) AND (not
    # geo_excluded) against the default fence (recife @ 40 km). The real view
    # reads the migration-seeded default rules + geo-fence tables; the fake
    # derives both from its defaults. Geo is a separate axis, never folded into
    # evaluate().soft_deletable.
    not_soft_deletable = not evaluate(
        name, btype, gtype, EligibilityConfig.defaults()
    ).soft_deletable
    reference_eligible = not_soft_deletable and not geo_excluded(lat, lng, DEFAULT_GEO_FENCE)
    assert reference_eligible is expected, f"fixture {label} mislabeled vs evaluate()+geo"

    vid = _vid()
    _seed(store, vid, name, btype, gtype, lat, lng)
    servable = set(store.list_servable_venue_ids())
    assert (vid in servable) is expected, (
        f"{label}: view says servable={vid in servable}, expected {expected}"
    )


def test_view_excludes_deprecated_regardless_of_eligibility(store):
    vid = _vid()
    _seed(store, vid, "Boteco do Zé", "BAR", None)   # eligible if active
    store.soft_delete_venue(vid, "google_places_closed_permanently", "google_places")
    assert vid not in set(store.list_servable_venue_ids())


def test_servable_by_priority_orders_filters_and_excludes(store):
    """The bounded-refresh selection source: servable (serving-view) venues only,
    ordered by priority asc then reviews desc. Against real Postgres (RDS_TEST_URL)
    this is the only test that exercises the serving.eligible_venue ⋈ venues.venue
    JOIN + ORDER BY; offline it pins the fake mirror to the same contract."""
    p_hi, p_mid, p_lo, blocked, dead = (_vid() for _ in range(5))

    def _bar(vid, priority, reviews):
        store.upsert_venue(Venue(
            venue_id=vid, venue_name=f"Bar {vid}", venue_address="a",
            venue_lat=-8.05, venue_lng=-34.88, venue_type="BAR",
            priority=priority, reviews=reviews,
        ))

    # Seed scrambled so insertion order != the required (priority asc, reviews desc).
    _bar(p_lo, 1, 999)    # higher priority number -> sorts last despite most reviews
    _bar(p_mid, 0, 100)
    _bar(p_hi, 0, 500)    # same priority as p_mid, more reviews -> ahead of it
    _seed(store, blocked, "Drogasil", None, "drugstore")  # active but ineligible
    _bar(dead, 0, 999)
    store.soft_delete_venue(dead, "google_places_closed_permanently", "google_places")

    # Large limit so the global selection isn't truncated before our seeded ids
    # (the real scratch DB may hold rows from other tests; assert on our slice).
    ordered = store.list_servable_venue_ids_by_priority(10_000_000)
    mine = [v for v in ordered if v in {p_hi, p_mid, p_lo, blocked, dead}]
    assert mine == [p_hi, p_mid, p_lo], mine  # ineligible + deprecated excluded
    assert store.list_servable_venue_ids_by_priority(0) == []


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
