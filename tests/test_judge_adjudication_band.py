"""Who gets sent to the judge, and who does not.

The judge costs money per call, so the band matters in both directions: too
narrow and the paid tier stays dead weight (it tops out at 0.60 while the probe
is blocked, below the 0.65 bar); too wide and every hopeless guess is billed.
"""
import asyncio

import pytest

from app.api.instagram_profile_probe import EXIST_ABSENT, ProfileProbeResult
from app.api.openai_instagram_judge_client import OpenAIInstagramJudgeClient
from app.services.instagram_cascade_service import (
    DEFAULT_JUDGE_FLOOR,
    InstagramCascadeService,
)
from app.services.instagram_judge import (
    MODE_TEXT_ONLY,
    MODE_UNAVAILABLE,
    TEXT_ONLY_CONFIDENCE_CEILING,
    InstagramJudge,
)

PROD_ACCEPT = 0.8
BAR = 0.65


class _Venue:
    venue_name = "Subway"
    venue_address = "Recife"


class _Dao:
    saved = None

    def list_servable_venue_ids(self):
        return ["ven_1"]

    def get_venue(self, venue_id):
        return _Venue()

    def get_venue_instagram(self, venue_id):
        return None

    def set_venue_instagram(self, record):
        self.saved = record


class _Listing:
    def __init__(self, site=None):
        self.site = site

    async def website_for(self, venue_id, venue=None):
        return self.site


class _Paid:
    def __init__(self, username):
        self.username = username

    async def search(self, venue):
        return [{"username": self.username, "display_name": None}]


class _Probe:
    def __init__(self, existence):
        self.existence = existence

    async def fetch(self, handle):
        return ProfileProbeResult(existence=self.existence)


class _Spy:
    def __init__(self, is_match=True, confidence=0.95):
        self.calls = 0
        self.is_match = is_match
        self.confidence = confidence

    async def judge_instagram_match(self, **kwargs):
        self.calls += 1
        return {"is_match": self.is_match, "confidence": self.confidence, "reason": "ok"}


def _run(*, listing=None, paid="subwayoficialbr", probe=None, spy=None, config=None):
    service = InstagramCascadeService(
        venue_dao=_Dao(),
        google_listing=_Listing(listing),
        paid_search=_Paid(paid),
        probe=probe,
        judge=InstagramJudge(spy) if spy else None,
        accept_threshold=PROD_ACCEPT,
        ambiguous_low=0.5,
    )
    cfg = {"force_refresh": True}
    cfg.update(config or {})
    return asyncio.run(service.discover("ven_1", cfg))


class TestWhoIsJudged:
    def test_a_paid_candidate_under_the_old_floor_is_now_judged(self):
        """Subway scored 0.428 in production and was discarded unseen."""
        spy = _Spy()
        result = _run(spy=spy)
        assert spy.calls == 1
        assert result.accepted

    def test_a_candidate_above_the_bar_is_never_judged(self):
        spy = _Spy()
        result = _run(listing="https://instagram.com/subway", spy=spy)
        assert spy.calls == 0, "paid to settle something already decided"
        assert result.accepted

    def test_a_confirmed_absent_profile_is_never_judged(self):
        spy = _Spy()
        result = _run(probe=_Probe(EXIST_ABSENT), spy=spy)
        assert spy.calls == 0
        assert not result.accepted

    def test_a_hopeless_candidate_below_the_floor_is_not_judged(self):
        spy = _Spy()
        _run(paid="zzzcompletelyunrelatedzzz", spy=spy)
        assert spy.calls == 0, "billed for a candidate with no chance"

    def test_the_floor_sits_below_the_weak_result_threshold(self):
        """They mean different things; if the floor were higher the paid tier
        could never be adjudicated at all."""
        assert DEFAULT_JUDGE_FLOOR < 0.5


class TestPerRunOverride:
    def test_a_run_can_turn_the_judge_off(self):
        spy = _Spy()
        _run(spy=spy, config={"judge_enabled": False})
        assert spy.calls == 0

    def test_omitting_the_flag_leaves_the_judge_on(self):
        spy = _Spy()
        _run(spy=spy)
        assert spy.calls == 1


class TestVerdictHandling:
    def test_a_rejection_does_not_accept(self):
        result = _run(spy=_Spy(is_match=False))
        assert not result.accepted

    def test_a_text_only_verdict_is_capped(self):
        result = _run(spy=_Spy(is_match=True, confidence=1.0))
        assert result.confidence <= TEXT_ONLY_CONFIDENCE_CEILING
        assert result.judge_mode == MODE_TEXT_ONLY

    def test_no_judge_records_unavailable_and_changes_nothing(self):
        result = _run(spy=None)
        assert result.judge_mode == MODE_UNAVAILABLE
        assert not result.accepted


class TestTheJudgeNeverFailsAVenue:
    class _Boom:
        async def judge_instagram_match(self, **kwargs):
            raise RuntimeError("openai down")

    class _Garbage:
        async def judge_instagram_match(self, **kwargs):
            return "not json at all"

    class _Refusal:
        async def judge_instagram_match(self, **kwargs):
            return {}

    @pytest.mark.parametrize("client", [_Boom(), _Garbage(), _Refusal()])
    def test_a_broken_judge_degrades_rather_than_raises(self, client):
        service = InstagramCascadeService(
            venue_dao=_Dao(),
            google_listing=_Listing(None),
            paid_search=_Paid("subwayoficialbr"),
            judge=InstagramJudge(client),
            accept_threshold=PROD_ACCEPT,
            ambiguous_low=0.5,
        )
        result = asyncio.run(service.discover("ven_1", {"force_refresh": True}))
        assert result is not None and not result.accepted


class TestClientShape:
    def test_it_exposes_the_method_the_judge_calls(self):
        assert hasattr(OpenAIInstagramJudgeClient, "judge_instagram_match")

    def test_it_can_be_built_from_a_key(self):
        assert OpenAIInstagramJudgeClient("sk-test") is not None
