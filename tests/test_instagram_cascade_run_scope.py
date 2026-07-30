"""The venue subset an operator gives the Instagram cascade is a COST CONTROL.

`run()` previously read the whole servable catalogue and never looked at
`config["venue_ids"]`, so a run scoped to two venues in the admin modal silently
became a full-catalogue run — with `force_refresh`, ~451 paid Apify searches
instead of two. The load-bearing assertion here is the negative one: the cascade
must not be *invoked* for a venue outside the subset.
"""
import asyncio

from app.services.instagram_cascade_service import InstagramCascadeService


class _Dao:
    def __init__(self, ids):
        self._ids = list(ids)

    def list_servable_venue_ids(self):
        return list(self._ids)


class _Res:
    accepted = False


def _run(catalogue, config):
    service = InstagramCascadeService(venue_dao=_Dao(catalogue))
    touched = []

    async def _discover(venue_id, cfg=None):
        touched.append(venue_id)
        return _Res()

    service.discover = _discover
    summary = asyncio.run(service.run(config))
    return touched, summary


class TestScoping:
    def test_restricts_to_the_requested_ids(self):
        touched, summary = _run(
            ["ven_a", "ven_b", "ven_c"], {"venue_ids": "ven_a, ven_c"}
        )
        assert touched == ["ven_a", "ven_c"]
        assert summary["considered"] == 2

    def test_does_not_touch_venues_outside_the_subset(self):
        """The cost guarantee, stated negatively."""
        touched, _ = _run(["ven_a", "ven_b", "ven_c"], {"venue_ids": "ven_b"})
        assert "ven_a" not in touched and "ven_c" not in touched

    def test_accepts_a_list_as_well_as_a_string(self):
        touched, _ = _run(["ven_a", "ven_b"], {"venue_ids": ["ven_b"]})
        assert touched == ["ven_b"]

    def test_de_duplicates_a_repeated_id(self):
        touched, _ = _run(["ven_a", "ven_b"], {"venue_ids": "ven_a, ven_a"})
        assert touched == ["ven_a"]


class TestFullCatalogue:
    def test_no_config_runs_everything(self):
        touched, _ = _run(["ven_a", "ven_b"], None)
        assert touched == ["ven_a", "ven_b"]

    def test_absent_field_runs_everything(self):
        touched, _ = _run(["ven_a", "ven_b"], {"force_refresh": True})
        assert touched == ["ven_a", "ven_b"]

    def test_blank_string_runs_everything(self):
        touched, _ = _run(["ven_a", "ven_b"], {"venue_ids": "  "})
        assert touched == ["ven_a", "ven_b"]


class TestUnknownIds:
    def test_unknown_ids_are_reported_not_fatal(self):
        touched, summary = _run(["ven_a"], {"venue_ids": "ven_a, ven_ghost"})
        assert touched == ["ven_a"]
        assert summary["unknown_venue_ids"] == 1

    def test_all_unknown_touches_nothing_and_still_returns_a_summary(self):
        """A typo must cost zero, and must not read as a successful full run."""
        touched, summary = _run(["ven_a"], {"venue_ids": "ven_ghost"})
        assert touched == []
        assert summary["considered"] == 0
        assert summary["unknown_venue_ids"] == 1
