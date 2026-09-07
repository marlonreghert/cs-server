"""Unit coverage for app.services.venue_address_components (mapping) and the
precedence-aware write path (RdsVenueStore.update_venue_address_components via
the InMemoryRdsVenueStore fake — the real store's SQL uses the identical
CASE-guarded precedence logic, verified against real Postgres separately).

plans/260905_events-serving-projection.md, Phase 2 pytest list: "each
fallback rung, and the never-clobber rule." The hard constraint this repo's
execution brief calls out explicitly: "A stored non-null value must NEVER be
overwritten with null or with an empty string, and raw_text/lat/lng must
never be touched by this path" — every one of those clauses gets its own
assertion below, not just the headline case.

plans/260906_address-components-backfill.md, Phase 2 superseded the blind
COALESCE with a per-field provenance precedence (operator > google > parsed):
the original tests below now pass `source="google"` explicitly (their
original intent — "Google's own re-enrichment" — is unchanged), and
`TestCrossSourcePrecedence` below exercises every ordered pair the new
precedence rule must enforce, not just the happy path.
"""
from __future__ import annotations

import pytest

from app.models.venue import Venue
from app.services.venue_address_components import map_address_components
from tests.rds_fake import InMemoryRdsVenueStore


def _component(text: str, *types: str) -> dict:
    return {"longText": text, "shortText": text, "types": list(types)}


# ── mapping: fallback rungs ──────────────────────────────────────────────
def test_neighborhood_prefers_sublocality_level_1():
    components = [
        _component("Boa Vista", "sublocality", "political"),
        _component("Santo Amaro", "sublocality_level_1", "political"),
    ]
    assert map_address_components(components)["neighborhood"] == "Santo Amaro"


def test_neighborhood_falls_back_to_sublocality():
    components = [_component("Boa Vista", "sublocality", "political")]
    assert map_address_components(components)["neighborhood"] == "Boa Vista"


def test_neighborhood_falls_back_to_administrative_area_level_2():
    components = [_component("Igarassu", "administrative_area_level_2", "political")]
    assert map_address_components(components)["neighborhood"] == "Igarassu"


def test_city_prefers_administrative_area_level_2_over_locality():
    components = [
        _component("Recife", "locality", "political"),
        _component("Recife Metro", "administrative_area_level_2", "political"),
    ]
    assert map_address_components(components)["city"] == "Recife Metro"


def test_city_falls_back_to_locality():
    components = [_component("Recife", "locality", "political")]
    assert map_address_components(components)["city"] == "Recife"


def test_street_from_route():
    components = [_component("Rua Abdon Batista", "route")]
    assert map_address_components(components)["street"] == "Rua Abdon Batista"


def test_postal_code_component():
    components = [_component("50100-460", "postal_code")]
    assert map_address_components(components)["postal_code"] == "50100-460"


def test_full_response_populates_every_field_at_once():
    components = [
        _component("Rua Abdon Batista", "route"),
        _component("Santo Amaro", "sublocality_level_1", "political"),
        _component("Recife", "locality", "political"),
        _component("50100-460", "postal_code"),
    ]
    mapped = map_address_components(components)
    assert mapped == {
        "street": "Rua Abdon Batista",
        "neighborhood": "Santo Amaro",
        "city": "Recife",
        "postal_code": "50100-460",
    }


def test_no_components_at_all_maps_every_field_to_none():
    assert map_address_components(None) == {
        "street": None, "neighborhood": None, "city": None, "postal_code": None,
    }
    assert map_address_components([]) == {
        "street": None, "neighborhood": None, "city": None, "postal_code": None,
    }


def test_a_component_with_an_empty_longtext_is_not_treated_as_an_answer():
    """An empty string is not a legitimate bairro; the mapper must keep
    trying the next fallback rung rather than accepting "" as an answer."""
    components = [
        _component("", "sublocality_level_1", "political"),
        _component("Boa Vista", "sublocality", "political"),
    ]
    assert map_address_components(components)["neighborhood"] == "Boa Vista"


def test_unrelated_component_types_are_ignored():
    components = [_component("BR", "country", "political")]
    mapped = map_address_components(components)
    assert mapped["street"] is None
    assert mapped["neighborhood"] is None
    assert mapped["city"] is None
    assert mapped["postal_code"] is None


# ── never-clobber write path (fake store; real SQL is COALESCE-mirrored) ──
def _seeded_store(venue_id="addr_venue_1") -> InMemoryRdsVenueStore:
    store = InMemoryRdsVenueStore()
    store.upsert_venue(
        Venue(
            venue_id=venue_id, venue_name="Address Test Venue",
            venue_address="R. Test, 1 - Recife PE", venue_lat=-8.05, venue_lng=-34.88,
        )
    )
    return store


def test_a_null_response_component_never_overwrites_a_stored_value():
    store = _seeded_store()
    store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Santo Amaro")
    assert store.get_address("addr_venue_1")["neighborhood"] == "Santo Amaro"

    # A later enrichment response with NO neighborhood answer (None, the
    # mapper's own "not answered" value) must leave the stored value intact.
    store.update_venue_address_components("addr_venue_1", source="google", neighborhood=None)
    assert store.get_address("addr_venue_1")["neighborhood"] == "Santo Amaro"


def test_an_empty_string_component_never_overwrites_a_stored_value():
    """The hard constraint names BOTH null and empty-string clobbering
    explicitly — map_address_components itself never produces "" (see
    test_a_component_with_an_empty_longtext_is_not_treated_as_an_answer),
    but the write path is tested directly against "" too so the guarantee
    does not rest solely on the mapper's own good behaviour."""
    store = _seeded_store()
    store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Santo Amaro")
    store.update_venue_address_components("addr_venue_1", source="google", neighborhood="")
    assert store.get_address("addr_venue_1")["neighborhood"] == "Santo Amaro"


def test_a_fresh_non_null_answer_does_update_a_previously_stored_value():
    """Never-clobber protects against a NULL response erasing knowledge; it
    does not freeze the first answer forever — Google's own answer can
    legitimately change (e.g. a corrected sublocality), and re-enrichment
    already overwrites every other vibe attribute wholesale. Same-source
    (`google` -> `google`) always refreshes: `>=`, not `>`."""
    store = _seeded_store()
    store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Santo Amaro")
    store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Boa Viagem")
    assert store.get_address("addr_venue_1")["neighborhood"] == "Boa Viagem"


def test_every_structured_field_is_independently_never_clobbered():
    store = _seeded_store()
    store.update_venue_address_components(
        "addr_venue_1", source="google", street="Rua X", neighborhood="Santo Amaro",
        city="Recife", postal_code="50100-460",
    )
    store.update_venue_address_components(
        "addr_venue_1", source="google",
        street=None, neighborhood=None, city=None, postal_code=None,
    )
    addr = store.get_address("addr_venue_1")
    assert addr["street"] == "Rua X"
    assert addr["neighborhood"] == "Santo Amaro"
    assert addr["city"] == "Recife"
    assert addr["postal_code"] == "50100-460"


def test_raw_text_lat_lng_are_never_touched_by_this_write_path():
    store = _seeded_store()
    before = dict(store.get_address("addr_venue_1"))
    store.update_venue_address_components(
        "addr_venue_1", source="google", street="Rua X", neighborhood="Santo Amaro",
        city="Recife", postal_code="50100-460",
    )
    after = store.get_address("addr_venue_1")
    assert after["raw_text"] == before["raw_text"]
    assert after["lat"] == before["lat"]
    assert after["lng"] == before["lng"]


def test_a_missing_address_row_is_a_silent_no_op():
    """Should not occur in practice (every venue gets a 1:1 address row at
    upsert_venue time) but must never raise — mirrors the real UPDATE ...
    WHERE's zero-rows-affected behaviour."""
    store = InMemoryRdsVenueStore()
    store.update_venue_address_components("no_such_venue", source="google", neighborhood="X")  # must not raise
    assert store.get_address("no_such_venue") is None


def test_an_invalid_source_raises_value_error():
    """A future caller cannot forget to state its provenance silently —
    both the real store and this fake require `source` and reject anything
    outside the three known tiers."""
    store = _seeded_store()
    with pytest.raises(ValueError):
        store.update_venue_address_components("addr_venue_1", source="bestguess", neighborhood="X")


# ── cross-source precedence: every ordered pair, not just the happy path ──
class TestCrossSourcePrecedence:
    """plans/260906_address-components-backfill.md Phase 2's whole point:
    operator > google > parsed, `>=` within the same source. A parsed value
    must never block a later google answer; nothing may overwrite an
    operator value; a source may still refresh its own prior answer."""

    def test_parsed_then_google_upgrades(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", neighborhood="Santa Rosa")
        store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Santa Rosa Baixa")
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Santa Rosa Baixa"
        assert addr["neighborhood_source"] == "google"

    def test_google_then_parsed_is_refused(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Santo Amaro")
        store.update_venue_address_components("addr_venue_1", source="parsed", neighborhood="Wrong Guess")
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Santo Amaro"
        assert addr["neighborhood_source"] == "google"

    def test_operator_then_google_is_refused(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="operator", neighborhood="Recife Antigo")
        store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Bairro do Recife")
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Recife Antigo"
        assert addr["neighborhood_source"] == "operator"

    def test_operator_then_parsed_is_refused(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="operator", neighborhood="Recife Antigo")
        store.update_venue_address_components("addr_venue_1", source="parsed", neighborhood="Wrong Guess")
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Recife Antigo"
        assert addr["neighborhood_source"] == "operator"

    def test_parsed_then_operator_upgrades(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", neighborhood="Santa Rosa")
        store.update_venue_address_components("addr_venue_1", source="operator", neighborhood="Santa Rosa Baixa")
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Santa Rosa Baixa"
        assert addr["neighborhood_source"] == "operator"

    def test_google_then_operator_upgrades(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Santo Amaro")
        store.update_venue_address_components("addr_venue_1", source="operator", neighborhood="Corrected")
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Corrected"
        assert addr["neighborhood_source"] == "operator"

    def test_same_source_always_refreshes_google(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="google", neighborhood="First")
        store.update_venue_address_components("addr_venue_1", source="google", neighborhood="Second")
        assert store.get_address("addr_venue_1")["neighborhood"] == "Second"

    def test_same_source_always_refreshes_parsed(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", neighborhood="First")
        store.update_venue_address_components("addr_venue_1", source="parsed", neighborhood="Second")
        assert store.get_address("addr_venue_1")["neighborhood"] == "Second"

    def test_same_source_always_refreshes_operator(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="operator", neighborhood="First")
        store.update_venue_address_components("addr_venue_1", source="operator", neighborhood="Second")
        assert store.get_address("addr_venue_1")["neighborhood"] == "Second"

    def test_parsed_onto_a_never_before_written_field_writes(self):
        """No stored source yet (NULL) always loses to a real source,
        regardless of how low that source ranks."""
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", city="Recife")
        addr = store.get_address("addr_venue_1")
        assert addr["city"] == "Recife"
        assert addr["city_source"] == "parsed"

    def test_precedence_is_independent_per_field(self):
        """One field can be operator-locked while a SIBLING field on the
        SAME venue is still open to a google upgrade — precedence is
        evaluated per column, not per row."""
        store = _seeded_store()
        store.update_venue_address_components(
            "addr_venue_1", source="operator", neighborhood="Recife Antigo",
        )
        store.update_venue_address_components(
            "addr_venue_1", source="google", neighborhood="Bairro do Recife", city="Recife",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Recife Antigo"  # untouched
        assert addr["city"] == "Recife"  # sibling field DID write
        assert addr["city_source"] == "google"


# ══════════════════════════════════════════════════════════════════════════
# neighborhood-equals-city suppression at the SHARED write boundary
# (plans/260906_events-venue-bairro-and-ticket-url.md §2 — F09/R08/R11)
# ══════════════════════════════════════════════════════════════════════════
class TestNeighborhoodEqualsCitySuppression:
    """The guard lives at `update_venue_address_components`, the ONE
    boundary every writer passes through — `google` (write_mapped_
    components), `parsed` (apply_parser_fallback) and any future
    `operator` writer — rather than inside `map_address_components`, which
    only the Google half reaches. 3,452 production rows are parser-sourced,
    so a Google-only guard would have covered almost none of them."""

    # ── the Google half: administrative_area_level_2 is BOTH rungs ───────
    def test_a_google_write_of_the_city_as_the_bairro_is_dropped(self):
        """`_NEIGHBORHOOD_TYPES` ends with `administrative_area_level_2`
        and `_CITY_TYPES` begins with it, so any municipality publishing no
        `sublocality*` maps to neighborhood == city."""
        store = _seeded_store()
        mapped = map_address_components(
            [_component("Igarassu", "administrative_area_level_2", "political")]
        )
        assert mapped["neighborhood"] == mapped["city"] == "Igarassu"  # the mapper is pure
        store.update_venue_address_components("addr_venue_1", source="google", **mapped)
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] is None
        assert addr["city"] == "Igarassu"  # the sibling column still writes

    # ── the parser half: the one revision 1 missed ───────────────────────
    def test_a_parsed_write_of_the_city_as_the_bairro_is_dropped(self):
        store = _seeded_store()
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", city="Igarassu", neighborhood="Igarassu",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] is None
        assert addr["city"] == "Igarassu"

    def test_it_compares_against_the_stored_city_when_the_write_carries_none(self):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", city="Igarassu")
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", neighborhood="Igarassu",
        )
        assert store.get_address("addr_venue_1")["neighborhood"] is None

    @pytest.mark.parametrize("stored_city,incoming", [
        ("São Paulo", "SAO PAULO"),
        ("São Paulo", "sao paulo"),
        ("Recife", "  recife  "),
        ("Igarassu", "IGARASSU"),
    ])
    def test_the_comparison_folds_accents_case_and_whitespace(self, stored_city, incoming):
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", city=stored_city)
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", neighborhood=incoming,
        )
        assert store.get_address("addr_venue_1")["neighborhood"] is None

    # ── it must not become a blanket drop ────────────────────────────────
    def test_a_bairro_that_differs_from_the_city_still_writes(self):
        store = _seeded_store()
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", city="Recife", neighborhood="Espinheiro",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Espinheiro"
        assert addr["neighborhood_source"] == "parsed"

    def test_the_other_three_columns_are_unaffected_when_the_bairro_is_dropped(self):
        """The suppression is per-COLUMN, not per-write: a caption that
        happens to produce a city-named bairro must not cost the street."""
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", city="Igarassu")
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", neighborhood="Igarassu",
            street="Rua do Sol", postal_code="53600-000",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] is None
        assert addr["street"] == "Rua do Sol"
        assert addr["postal_code"] == "53600-000"
        assert addr["city"] == "Igarassu"

    def test_it_can_only_prevent_a_write_never_erase_a_stored_value(self):
        """A None never clobbers under the precedence-guarded write, so a
        good stored bairro survives a later city-named write attempt."""
        store = _seeded_store()
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", city="Recife", neighborhood="Espinheiro",
        )
        store.update_venue_address_components(
            "addr_venue_1", source="google", city="Recife", neighborhood="Recife",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Espinheiro"
        assert addr["neighborhood_source"] == "parsed"

    # ── R08: the operator exemption ──────────────────────────────────────
    def test_an_operator_may_assert_a_bairro_named_after_its_city(self):
        """Bairro do Recife (Recife Antigo) is a real, central neighbourhood
        OF the city of Recife. The guard is a heuristic over machine-written
        values; a human write is trusted verbatim, and is the documented
        repair path when the heuristic drops a correct value."""
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", city="Recife")
        store.update_venue_address_components(
            "addr_venue_1", source="operator", neighborhood="Recife",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] == "Recife"
        assert addr["neighborhood_source"] == "operator"

    def test_the_exemption_is_operator_only(self):
        for source in ("google", "parsed"):
            store = _seeded_store()
            store.update_venue_address_components("addr_venue_1", source="parsed", city="Recife")
            store.update_venue_address_components(
                "addr_venue_1", source=source, neighborhood="Recife",
            )
            assert store.get_address("addr_venue_1")["neighborhood"] is None, source

    # ── R11: the EFFECTIVE post-write city, in both directions ───────────
    def test_it_drops_when_the_incoming_city_loses_and_the_kept_city_matches(self):
        """The shape this plan's own upgrade sweep creates: `city_source`
        is already 'google' while `neighborhood_source` is NULL/'parsed',
        and `apply_parser_fallback` then runs against it. The city column
        does not write (parsed < google), so comparing only against the
        INCOMING city would leave the row holding neighborhood == city."""
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="google", city="Recife")
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", city="Igarassu", neighborhood="Recife",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] is None
        assert addr["city"] == "Recife"
        assert addr["city_source"] == "google"

    def test_it_keeps_the_bairro_when_the_incoming_city_wins_and_differs(self):
        """The mirror case, and the false positive R11's literal
        "incoming OR STORED city" rule would have caused: the row commits
        with city="Igarassu" and neighborhood="Recife", no equality at all,
        so a correct bairro must survive."""
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="parsed", city="Recife")
        store.update_venue_address_components(
            "addr_venue_1", source="google", city="Igarassu", neighborhood="Recife",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["city"] == "Igarassu"
        assert addr["neighborhood"] == "Recife"
        assert addr["neighborhood_source"] == "google"

    def test_it_drops_when_the_incoming_city_matches_even_though_it_loses(self):
        """Both comparisons matter independently: here the incoming city
        loses precedence AND equals the bairro, and the row's kept city is
        something else entirely."""
        store = _seeded_store()
        store.update_venue_address_components("addr_venue_1", source="google", city="Recife")
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", city="Olinda", neighborhood="Olinda",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] is None
        assert addr["city"] == "Recife"

    def test_a_row_with_no_stored_city_at_all_still_compares_the_incoming_one(self):
        store = _seeded_store()
        store.update_venue_address_components(
            "addr_venue_1", source="google", city="Olinda", neighborhood="Olinda",
        )
        addr = store.get_address("addr_venue_1")
        assert addr["neighborhood"] is None
        assert addr["city"] == "Olinda"

    def test_a_write_with_neither_city_nor_stored_city_is_untouched(self):
        store = _seeded_store()
        store.update_venue_address_components(
            "addr_venue_1", source="parsed", neighborhood="Espinheiro",
        )
        assert store.get_address("addr_venue_1")["neighborhood"] == "Espinheiro"
