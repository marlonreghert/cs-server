"""Name matching, asserted through the real scoring at the real weights.

Every pair here was measured against a production venue during the top-250
Recife run. The true cases had already FOUND the correct link and were rejected
on the name check alone; the noise cases are links that really do appear on a
venue's website and really do belong to somebody else.
"""
import difflib

import pytest

from app.services.instagram_cascade_service import (
    NAME_WEIGHT,
    PROVENANCE_WEIGHT,
    name_similarity,
    venue_core,
)
from app.services.instagram_handle_sources import SOURCE_VENUE_WEBSITE

BAR = 0.65  # production accept (0.8) minus the uncollectable existence bonus


def _confidence(venue, handle):
    prov = PROVENANCE_WEIGHT[SOURCE_VENUE_WEBSITE]
    return min(prov + NAME_WEIGHT * name_similarity(venue, None, handle), 1.0)


TRUE_PAIRS = [
    ("Pizzaria Atlântico Graças", "pizzariaatlantico"),
    ("Restaurante Parraxaxá Boa Viagem", "parraxaxaoficial"),
    ("Casa da Cultura de Pernambuco", "casadaculturape"),
    ("Camarada Camarão RioMar Recife", "camaradacamarao"),
    ("Armazém Guimarães RioMar Recife", "armazemguimaraes"),
    ("Bode do Nô Boa Viagem - Restaurante", "bodedono"),
    ("Ponte Nova", "ponte_nova"),
    ("Buca Trattoria", "bucatrattoria"),
    ("Don Francesco Trattoria", "donfrancescotrattoria"),
    ("Club Metrópole", "clubmetropole"),
]

NOISE_PAIRS = [
    ("Ordinário Bar e Música", "marketingpararestaurante"),
    ("The Fisherman", "smartfit"),
    ("University Theater Paschoal", "ufcinforma"),
    ("Lower Deck Bar & Nightclub", "parkelanzacbe"),
]


class TestRealVenues:
    @pytest.mark.parametrize("venue,handle", TRUE_PAIRS)
    def test_true_pairs_clear_the_bar(self, venue, handle):
        assert _confidence(venue, handle) >= BAR

    @pytest.mark.parametrize("venue,handle", NOISE_PAIRS)
    def test_noise_pairs_stay_below_the_bar(self, venue, handle):
        assert _confidence(venue, handle) < BAR

    def test_the_two_cohorts_are_actually_separated(self):
        """Not just individually right — separated, with the bar between them."""
        worst_true = min(_confidence(v, h) for v, h in TRUE_PAIRS)
        best_noise = max(_confidence(v, h) for v, h in NOISE_PAIRS)
        assert best_noise < BAR <= worst_true, (
            f"noise reaches {best_noise:.3f}, weakest true match {worst_true:.3f}"
        )


class TestVenueCore:
    @pytest.mark.parametrize("name,expected", [
        ("Bode do Nô Boa Viagem - Restaurante", "bodeno"),
        ("Restaurante Parraxaxá Boa Viagem", "parraxaxa"),
        ("Camarada Camarão RioMar Recife", "camaradacamarao"),
        ("Pizzaria Atlântico Graças", "atlantico"),
    ])
    def test_strips_category_and_locality(self, name, expected):
        assert venue_core(name) == expected

    def test_a_name_that_is_all_generic_words_keeps_something(self):
        """Never return empty — an empty core would match everything."""
        assert venue_core("Restaurante Bar") != ""

    def test_handles_none_and_empty(self):
        assert venue_core(None) == ""
        assert venue_core("") == ""


class TestItOnlyAddsEvidence:
    @pytest.mark.parametrize("venue,handle", TRUE_PAIRS + NOISE_PAIRS)
    def test_never_scores_below_the_plain_comparison(self, venue, handle):
        plain = difflib.SequenceMatcher(
            None, venue.strip().lower(), handle.replace("_", " ").replace(".", " ").lower()
        ).ratio()
        assert name_similarity(venue, None, handle) >= plain - 1e-9

    def test_display_name_still_wins_when_present(self):
        assert name_similarity("Bar do Cuscuz", "Bar do Cuscuz", "unrelated") == 1.0

    def test_no_handle_and_no_display_name_is_zero(self):
        assert name_similarity("Bar do Cuscuz", None, None) == 0.0


class TestContainmentIsBounded:
    def test_a_short_core_does_not_match_an_unrelated_handle(self):
        """Without a minimum length, a 3-letter core matches half of Instagram."""
        assert name_similarity("Bar", None, "barbeariadojoao") < 0.95

    def test_folding_alone_never_manufactures_a_match(self):
        assert _confidence("Açaí", "completelyunrelatedhandle") < BAR
