"""Unit tests for the reusable Instagram caption event-marker matcher
(app/services/event_caption_matcher.py) — shared by the event venue targeting
evidence gate and (later) event extraction. See
plans/260804_event-venue-targeting.md.
"""
from app.services.event_caption_matcher import (
    MARKER_DATE,
    MARKER_WEEKDAY_TIME,
    find_event_markers,
    matches_event_marker,
)


class TestPtBrDateForms:
    def test_numeric_date_matches(self):
        assert MARKER_DATE in find_event_markers("Show dia 25/12 na casa!")

    def test_numeric_date_with_year_matches(self):
        assert MARKER_DATE in find_event_markers("Reservas para 03.10.2026 já abertas")

    def test_textual_date_matches(self):
        assert MARKER_DATE in find_event_markers("Vem aí, 25 de dezembro, edição especial")

    def test_textual_date_without_de_matches(self):
        assert MARKER_DATE in find_event_markers("Grande festa 3 agosto, não perca")


class TestWeekdayPlusTime:
    def test_weekday_with_h_notation_matches(self):
        assert MARKER_WEEKDAY_TIME in find_event_markers("Sexta tem samba! 22h a casa abre")

    def test_weekday_with_colon_time_matches(self):
        assert MARKER_WEEKDAY_TIME in find_event_markers("Sábado às 20:00 abertura dos portões")

    def test_weekday_alone_does_not_match_weekday_time(self):
        markers = find_event_markers("Aberto todos os dias, inclusive sábado e domingo")
        assert MARKER_WEEKDAY_TIME not in markers


class TestTicketingTerms:
    def test_ingressos_matches(self):
        assert matches_event_marker("Ingressos à venda na Sympla!")

    def test_lineup_matches(self):
        assert matches_event_marker("Line-up completo já disponível")

    def test_open_bar_matches(self):
        assert matches_event_marker("Hoje é open bar até meia-noite")

    def test_pre_venda_matches_with_or_without_accent(self):
        assert matches_event_marker("Pré-venda liberada")
        assert matches_event_marker("Pre-venda liberada")


class TestNonMatches:
    def test_menu_announcement_does_not_match(self):
        assert not matches_event_marker(
            "Cardápio novo disponível! Peça já o nosso hambúrguer artesanal."
        )

    def test_holiday_greeting_does_not_match(self):
        assert not matches_event_marker("Feliz Natal e próspero ano novo a todos!")

    def test_empty_caption_does_not_match(self):
        assert not matches_event_marker("")

    def test_none_caption_does_not_match(self):
        assert not matches_event_marker(None)
        assert find_event_markers(None) == []
