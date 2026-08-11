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


class TestOperatorRunPaidTiers:
    """The whole-catalogue Run dialog must not silently buy Google searches.

    Once the Google-search collaborator is built on credential presence alone
    (plans/260811_instagram-discovery-admin-flags.md), the ONLY thing standing
    between a routine operator run and a paid search per catalogue venue is
    this default. `_source_enabled` falls through to the PAID_SOURCES master
    switch when a per-source key is absent, and this run leaves that master
    True for the Instagram user search — so an absent key here means "on".
    """

    def _instagram_defaults(self):
        from app.routers.admin_trigger_router import JOB_REGISTRY

        return JOB_REGISTRY["instagram"]["default_config"]

    def test_google_search_is_explicitly_disabled_by_default(self):
        defaults = self._instagram_defaults()
        assert "tier_google_search_enabled" in defaults, (
            "an absent key falls through to the paid master switch, which this "
            "run leaves True — the tier must be disabled EXPLICITLY"
        )
        assert defaults["tier_google_search_enabled"] is False

    def test_the_default_run_does_not_reach_google_search(self):
        from app.services.instagram_cascade_service import InstagramCascadeService
        from app.services.instagram_handle_sources import SOURCE_GOOGLE_SEARCH

        service = InstagramCascadeService(venue_dao=None)
        assert not service._source_enabled(
            SOURCE_GOOGLE_SEARCH, dict(self._instagram_defaults())
        )

    def test_an_operator_can_still_opt_in(self):
        from app.services.instagram_cascade_service import InstagramCascadeService
        from app.services.instagram_handle_sources import SOURCE_GOOGLE_SEARCH

        config = dict(self._instagram_defaults())
        config["tier_google_search_enabled"] = True
        service = InstagramCascadeService(venue_dao=None)
        assert service._source_enabled(SOURCE_GOOGLE_SEARCH, config)
