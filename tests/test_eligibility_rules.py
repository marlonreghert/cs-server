"""Unit tests for Ex2: eligibility rows <-> blob + EligibilityRuleService."""
import fakeredis

from app.services.admin_config_service import AdminConfigService
from app.services.eligibility_rules import EligibilityRuleService
from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    DEFAULT_BLOCKED_GOOGLE_TYPES,
    EligibilityConfig,
    assemble_eligibility_blob,
    decompose_eligibility_blob,
    eligibility_config_from_rules,
    evaluate,
)
from tests.rds_fake import InMemoryRdsVenueStore


def _service():
    store = InMemoryRdsVenueStore()
    redis = fakeredis.FakeRedis(decode_responses=True)

    def _validate(value):
        EligibilityConfig.from_dict(value, from_admin_override=True)
        return value

    acs = AdminConfigService(
        redis_client=redis, rds_store=store,
        validators={"venue_eligibility": _validate},
    )
    return EligibilityRuleService(store, acs), store, redis


# ── pure functions ───────────────────────────────────────────────────────────
def test_decompose_normalizes_and_assemble_round_trips():
    blob = {
        "blocked_venue_types": ["bar", "Club"],        # -> upper
        "blocked_google_types": ["Casino", "BAR"],     # -> lower
        "blocked_name_keywords": ["FooBar"],           # alias -> hard, lower
    }
    rows = decompose_eligibility_blob(blob)
    assert ("blocked_venue_type", "BAR") in rows
    assert ("blocked_venue_type", "CLUB") in rows
    assert ("blocked_google_type", "casino") in rows
    assert ("hard_blocked_name_keyword", "foobar") in rows
    # Effective config from rows equals from_dict over the original blob. Compared
    # order-insensitively: the block-lists are membership sets, so list element
    # order is irrelevant (rows reassemble in sorted order).
    def _norm(d):
        return {k: sorted(v) if isinstance(v, list) else v for k, v in d.items()}

    assert _norm(eligibility_config_from_rules(rows).to_public_dict()) == _norm(
        EligibilityConfig.from_dict(blob).to_public_dict()
    )


def test_assemble_omits_absent_categories():
    blob = assemble_eligibility_blob([("blocked_google_type", "casino")])
    assert blob == {"blocked_google_types": ["casino"]}


def test_empty_rows_is_defaults():
    cfg = eligibility_config_from_rules([])
    assert cfg.from_admin_override is False
    assert cfg.blocked_google_types == frozenset(DEFAULT_BLOCKED_GOOGLE_TYPES)


# ── service ──────────────────────────────────────────────────────────────────
def test_add_rule_reassembles_mirror_and_blocks():
    svc, store, redis = _service()
    svc.add_rule("blocked_google_type", "Casino", updated_by="admin")  # normalizes
    assert store.list_eligibility_rules() == [("blocked_google_type", "casino")]
    assert redis.get(ADMIN_CONFIG_ELIGIBILITY_KEY) is not None
    cfg = svc.effective_config()
    assert not evaluate("X", google_type="casino", config=cfg).eligible


def test_remove_last_rule_drops_override_to_defaults():
    svc, store, redis = _service()
    svc.add_rule("blocked_google_type", "casino")
    svc.remove_rule("blocked_google_type", "casino")
    assert store.list_eligibility_rules() == []
    assert redis.get(ADMIN_CONFIG_ELIGIBILITY_KEY) is None  # override dropped
    assert svc.effective_config().from_admin_override is False


def test_set_full_config_replaces_rows():
    svc, store, _ = _service()
    svc.add_rule("blocked_google_type", "casino")
    svc.set_full_config({"blocked_venue_types": ["ARCADE"]})
    assert store.list_eligibility_rules() == [("blocked_venue_type", "ARCADE")]


def test_unknown_rule_type_rejected():
    svc, _, _ = _service()
    try:
        svc.add_rule("not_a_type", "x")
        assert False, "expected ValueError"
    except ValueError:
        pass
