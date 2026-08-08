"""Reconciliation must find same-place duplicate rows (to deprecate the extras)
without merging genuinely different venues that sit close together."""

from app.services.venue_reconciliation import find_duplicate_groups


def _row(vid, name, lat=-8.05, lng=-34.90, created="2026-01-01", status="active"):
    return {
        "venue_id": vid,
        "venue_name": name,
        "venue_lat": lat,
        "venue_lng": lng,
        "created_at": created,
        "lifecycle_status": status,
    }


def test_same_place_two_ids_is_a_group_keeping_earliest_created():
    groups = find_duplicate_groups(
        [
            _row("B", "Adega do Futuro", created="2026-05-01"),
            _row(
                "A",
                "Adega do Futuro - Gastronomia",
                -8.05005,
                -34.90005,
                created="2026-01-01",
            ),
        ]
    )
    assert len(groups) == 1
    assert groups[0].canonical["venue_id"] == "A"  # earliest created kept
    assert [d["venue_id"] for d in groups[0].duplicates] == ["B"]


def test_different_names_close_together_are_not_a_group():
    groups = find_duplicate_groups(
        [
            _row("A", "Galetus | DuGalego Bar"),
            _row("B", "Restaurante Varanda do Rock", -8.05005, -34.90005),
        ]
    )
    assert groups == []


def test_same_name_far_apart_is_not_a_group():
    groups = find_duplicate_groups(
        [
            _row("A", "Boteco do Zé", -8.0500, -34.9000),
            _row("B", "Boteco do Zé", -8.0530, -34.9000),  # ~330m
        ]
    )
    assert groups == []


def test_deprecated_rows_are_ignored():
    groups = find_duplicate_groups(
        [
            _row("A", "Adega do Futuro"),
            _row("B", "Adega do Futuro", -8.05005, -34.90005, status="deprecated"),
        ]
    )
    assert groups == []


def test_three_way_group_keeps_one_deprecates_two():
    groups = find_duplicate_groups(
        [
            _row("A", "Bar Central", created="2026-03-01"),
            _row("B", "Bar Central", -8.05002, -34.90002, created="2026-01-01"),
            _row("C", "Bar Central", -8.05003, -34.90003, created="2026-02-01"),
        ]
    )
    assert len(groups) == 1
    assert groups[0].canonical["venue_id"] == "B"
    assert sorted(d["venue_id"] for d in groups[0].duplicates) == ["A", "C"]


def test_rows_without_coords_or_name_are_never_grouped():
    groups = find_duplicate_groups(
        [
            {
                "venue_id": "A",
                "venue_name": "X",
                "venue_lat": None,
                "venue_lng": None,
                "created_at": "2026-01-01",
            },
            {
                "venue_id": "B",
                "venue_name": "",
                "venue_lat": -8.05,
                "venue_lng": -34.90,
                "created_at": "2026-01-01",
            },
        ]
    )
    assert groups == []
