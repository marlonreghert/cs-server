"""Unit tests for plans/260811_menu-item-lifecycle.md.

Menu identity is `(venue_id, normalize_title(title))` — deliberately WITHOUT
a date, because a dish is the same dish whenever it is posted (see
app.services.event_merge.compute_menu_identity's own docstring for the full
contrast with event identity). That is a genuine departure from
`compute_event_identity`'s `(venue_id, starts_at::date, normalize_title(title))`,
and the whole risk of this feature is the two rules leaking into each other:
a date-less identity applied to an EVENT would merge "Karaoke" on Friday with
"Karaoke" on Saturday into one event, silently losing one of two real nights.

`TestEventAndPromotionIdentityUnchangedByMenuDispatch` is written FIRST and
run against the UNMODIFIED codebase before any production code for this
plan exists — it pins `merge_touched_events`'s pre-existing venue-identity
behaviour (the exact dispatch point the menu path hooks into) for BOTH
`post_type="event"` and `post_type="promotion"` rows, so introducing the
menu branch can be proven, by this same file, to have touched neither.
Mirrors `tests/test_event_merge_handle_identity.py`'s own
`TestResolvedToResolvedPathIsByteForByteUnchanged`, which pinned the
identical concern for the handle-identity feature.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.menu_lifecycle import (
    DEFAULT_MENU_EXPIRY_DAYS,
    is_menu_item_current,
    load_menu_expiry_days,
    validate_menu_expiry_days_config,
)
from app.models.venue import Venue
from app.services.event_merge import compute_menu_identity, merge_touched_events
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

_FRIDAY = datetime(2026, 8, 7, 22, 0, tzinfo=timezone.utc)
_SATURDAY = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)


def _store_with_venue(venue_id="v1", venue_name="Club Metropole") -> InMemoryRdsVenueStore:
    store = InMemoryRdsVenueStore()
    store.upsert_venue(
        Venue(venue_id=venue_id, venue_name=venue_name, venue_lat=-8.05, venue_lng=-34.88)
    )
    return store


def _insert(store: InMemoryRdsVenueStore, shortcode: str, **fields) -> str:
    event_id = fields.pop("event_id", None) or new_event_id()
    base = {
        "event_id": event_id, "source_shortcode": shortcode,
        "source_event_key": f"{shortcode}_key",
        "source_handle": "venue_1_ig",
        "source_permalink": f"https://instagram.com/p/{shortcode}",
        "raw_extraction": {"time_known": True},
    }
    base.update(fields)
    store.insert_event(base)
    return event_id


class TestEventAndPromotionIdentityUnchangedByMenuDispatch:
    """Pinned against the codebase BEFORE compute_menu_identity/the menu
    merge branch existed — see this module's own docstring. Every assertion
    here must still hold, unedited, once the menu path is implemented."""

    def test_karaoke_friday_and_saturday_stay_two_events(self):
        """The exact danger named in the plan: same venue, same (normalized)
        title, DIFFERENT dates. A date-less identity would wrongly merge
        these into one event, losing a real night. compute_event_identity
        keeps the date, so they must stay apart."""
        store = _store_with_venue()
        friday_id = _insert(
            store, "friday", venue_id="v1", title="Karaoke", starts_at=_FRIDAY,
            status="pending_review", post_type="event",
        )
        saturday_id = _insert(
            store, "saturday", venue_id="v1", title="Karaoke", starts_at=_SATURDAY,
            status="pending_review", post_type="event",
        )
        merge_touched_events(store, [friday_id, saturday_id], datetime.now(timezone.utc))

        remaining = {r["event_id"] for r in store.list_events(venue_id="v1")}
        assert remaining == {friday_id, saturday_id}, (
            "a date-less identity leaked into the event path and merged two "
            "distinct nights into one"
        )

    def test_two_posts_about_the_same_night_still_merge_exactly_as_before(self):
        """The positive case, pinned alongside the negative one above: two
        EVENT posts genuinely sharing venue+date+title still collapse to one
        row, unchanged — proving the menu dispatch did not accidentally make
        events HARDER to merge either."""
        store = _store_with_venue()
        older_id = _insert(
            store, "older", venue_id="v1", title="Noite da Patroa", starts_at=_FRIDAY,
            status="pending_review", post_type="event", lineup=["DJ A"],
        )
        newer_id = _insert(
            store, "newer", venue_id="v1", title="NOITE DA PATROA", starts_at=_FRIDAY,
            status="pending_review", post_type="event", lineup=["DJ B"],
        )
        merge_touched_events(store, [older_id, newer_id], datetime.now(timezone.utc))

        assert store.get_event(newer_id) is None
        survivor = store.get_event(older_id)
        assert survivor is not None
        assert survivor["event_id"] == older_id  # oldest ULID wins, exactly as before
        assert survivor["title"] == "Noite da Patroa"
        assert survivor["lineup"] == ["DJ A", "DJ B"]

    def test_promotions_on_different_days_stay_two_promotions(self):
        """Promotions keep the SAME dated identity as events (compute_event_
        identity does not branch on post_type at all) — a happy-hour promo
        on two different Tuesdays must stay two rows, exactly like Karaoke
        Friday/Saturday above."""
        store = _store_with_venue()
        week1_id = _insert(
            store, "promo_week1", venue_id="v1", title="Terça do Chopp em Dobro",
            starts_at=_FRIDAY, status="pending_review", post_type="promotion",
        )
        week2_id = _insert(
            store, "promo_week2", venue_id="v1", title="Terça do Chopp em Dobro",
            starts_at=_SATURDAY, status="pending_review", post_type="promotion",
        )
        merge_touched_events(store, [week1_id, week2_id], datetime.now(timezone.utc))

        remaining = {r["event_id"] for r in store.list_events(venue_id="v1")}
        assert remaining == {week1_id, week2_id}

    def test_two_posts_about_the_same_promotion_still_merge_exactly_as_before(self):
        store = _store_with_venue()
        older_id = _insert(
            store, "promo_older", venue_id="v1", title="Terça do Chopp em Dobro",
            starts_at=_FRIDAY, status="pending_review", post_type="promotion",
            price_text="R$20",
        )
        newer_id = _insert(
            store, "promo_newer", venue_id="v1", title="Terça do Chopp em Dobro",
            starts_at=_FRIDAY, status="pending_review", post_type="promotion",
            price_text=None,
        )
        merge_touched_events(store, [older_id, newer_id], datetime.now(timezone.utc))

        assert store.get_event(newer_id) is None
        survivor = store.get_event(older_id)
        assert survivor is not None
        assert survivor["event_id"] == older_id
        assert survivor["price_text"] == "R$20"


class TestComputeMenuIdentity:
    def test_same_venue_and_title_are_the_same_identity_regardless_of_date(self):
        """The defining departure from compute_event_identity: two dishes at
        the same venue with the same name are the SAME dish whether they
        were posted a year apart or on the same day — no date participates
        at all."""
        a = compute_menu_identity("v1", "Especial do dia")
        b = compute_menu_identity("v1", "Especial do dia")
        assert a == b

    def test_case_and_accent_differences_do_not_change_identity(self):
        a = compute_menu_identity("v1", "PRATO EXECUTIVO")
        b = compute_menu_identity("v1", "Prato Executivo")
        assert a == b

    def test_different_titles_are_different_dishes(self):
        a = compute_menu_identity("v1", "Prato Executivo")
        b = compute_menu_identity("v1", "Feijoada")
        assert a != b

    def test_same_dish_name_at_different_venues_are_different_identities(self):
        a = compute_menu_identity("v1", "Prato Executivo")
        b = compute_menu_identity("v2", "Prato Executivo")
        assert a != b

    def test_null_venue_never_computes_an_identity(self):
        assert compute_menu_identity(None, "Prato Executivo") is None

    def test_a_missing_title_still_computes_an_identity_from_the_venue_alone(self):
        """Mirrors `compute_event_identity`, which never gates on title
        either — ONLY `venue_id` (here) / `venue_id` + `starts_at` (there)
        decide whether an identity exists at all."""
        assert compute_menu_identity("v1", None) == ("v1", "")


class TestMenuMergeOrchestration:
    """`merge_touched_events` end-to-end against the fake DAO — the level a
    pure-function test alone cannot prove: rows actually collapse, provenance
    survives, and the existing field-merge rules (null never overwrites, an
    operator's edit wins) apply unmodified to a dish."""

    def test_two_posts_about_the_same_dish_collapse_to_one_row(self):
        store = _store_with_venue()
        older_id = _insert(store, "dish_older", venue_id="v1", title="Especial do Dia", post_type="menu")
        newer_id = _insert(store, "dish_newer", venue_id="v1", title="ESPECIAL DO DIA", post_type="menu")
        merge_touched_events(store, [older_id, newer_id], datetime.now(timezone.utc))

        assert store.get_event(newer_id) is None
        survivor = store.get_event(older_id)
        assert survivor is not None
        assert survivor["event_id"] == older_id  # oldest ULID wins, same rule as events

    def test_dishes_posted_months_apart_still_merge_and_last_seen_at_refreshes(self):
        """The defining proof: NO date gates this merge, and the back-fill
        the plan describes ("existing menu items take their newest source's
        last_seen_at") is not a separate step — `last_seen_at` is already
        the aggregate MAX across every attached source (see app.services.
        event_merge's own module docstring), so reattaching the newer
        post's source and recomputing the aggregate IS the back-fill."""
        store = _store_with_venue()
        old_seen = datetime.now(timezone.utc) - timedelta(days=200)
        new_seen = datetime.now(timezone.utc)
        older_id = _insert(
            store, "old_post", venue_id="v1", title="Feijoada", post_type="menu",
            first_seen_at=old_seen, last_seen_at=old_seen,
        )
        newer_id = _insert(
            store, "new_post", venue_id="v1", title="Feijoada", post_type="menu",
            first_seen_at=new_seen, last_seen_at=new_seen,
        )
        merge_touched_events(store, [older_id, newer_id], datetime.now(timezone.utc))

        remaining = store.list_events(venue_id="v1")
        assert len(remaining) == 1
        assert remaining[0]["event_id"] == older_id
        assert remaining[0]["last_seen_at"] == new_seen

    def test_a_null_in_a_newer_post_never_overwrites_a_known_value(self):
        store = _store_with_venue()
        older_id = _insert(store, "p1", venue_id="v1", title="Feijoada", post_type="menu", price_text="R$30")
        newer_id = _insert(store, "p2", venue_id="v1", title="FEIJOADA", post_type="menu", price_text=None)
        merge_touched_events(store, [older_id, newer_id], datetime.now(timezone.utc))

        survivor = store.get_event(older_id)
        assert survivor["price_text"] == "R$30"

    def test_operator_edited_fields_survive_a_reposting(self):
        """§B: "operator_edited_fields wins" — reuses merge_event_fields's
        existing confirmed+edited-field branch UNCHANGED for a dish."""
        store = _store_with_venue()
        older_id = _insert(
            store, "p1", venue_id="v1", title="Feijoada", post_type="menu",
            status="confirmed", operator_edited_fields=["price_text"],
            price_text="R$45 promocional",
        )
        newer_id = _insert(store, "p2", venue_id="v1", title="FEIJOADA", post_type="menu", price_text="R$99")
        merge_touched_events(store, [older_id, newer_id], datetime.now(timezone.utc))

        survivor = store.get_event(older_id)
        assert survivor["price_text"] == "R$45 promocional"

    def test_different_dishes_at_one_venue_never_merge(self):
        store = _store_with_venue()
        a_id = _insert(store, "p1", venue_id="v1", title="Feijoada", post_type="menu")
        b_id = _insert(store, "p2", venue_id="v1", title="Moqueca", post_type="menu")
        merge_touched_events(store, [a_id, b_id], datetime.now(timezone.utc))

        remaining = {r["event_id"] for r in store.list_events(venue_id="v1")}
        assert remaining == {a_id, b_id}

    def test_same_dish_at_two_venues_never_merges(self):
        store = _store_with_venue("v1", "Restaurante A")
        store.upsert_venue(Venue(venue_id="v2", venue_name="Restaurante B", venue_lat=-8.05, venue_lng=-34.88))
        a_id = _insert(store, "p1", venue_id="v1", title="Feijoada", post_type="menu")
        b_id = _insert(store, "p2", venue_id="v2", title="Feijoada", post_type="menu")
        merge_touched_events(store, [a_id, b_id], datetime.now(timezone.utc))

        assert store.get_event(a_id) is not None
        assert store.get_event(b_id) is not None

    def test_an_unresolved_menu_item_never_merges(self):
        """No venue_id, no identity — stays unmerged and stays queued, the
        same posture an unresolved event already takes."""
        store = _store_with_venue()
        a_id = _insert(store, "p1", venue_id=None, title="Feijoada", post_type="menu")
        b_id = _insert(store, "p2", venue_id=None, title="Feijoada", post_type="menu")
        merge_touched_events(store, [a_id, b_id], datetime.now(timezone.utc))

        assert store.get_event(a_id) is not None
        assert store.get_event(b_id) is not None


class TestIsMenuItemCurrent:
    """Boundary behaviour: one day inside, exactly on, one day outside —
    inclusive at the boundary (seen EXACTLY `expiry_days` ago is still
    current)."""

    _NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    def test_one_day_inside_the_window_is_current(self):
        last_seen = self._NOW - timedelta(days=364)
        assert is_menu_item_current(last_seen, expiry_days=365, now=self._NOW) is True

    def test_exactly_on_the_boundary_is_current(self):
        last_seen = self._NOW - timedelta(days=365)
        assert is_menu_item_current(last_seen, expiry_days=365, now=self._NOW) is True

    def test_one_day_outside_the_window_is_expired(self):
        last_seen = self._NOW - timedelta(days=366)
        assert is_menu_item_current(last_seen, expiry_days=365, now=self._NOW) is False

    def test_a_missing_last_seen_at_is_never_current(self):
        assert is_menu_item_current(None, expiry_days=365, now=self._NOW) is False

    def test_a_shortened_window_expires_a_dish_the_default_would_still_show_as_current(self):
        last_seen = self._NOW - timedelta(days=40)
        assert is_menu_item_current(last_seen, expiry_days=365, now=self._NOW) is True
        assert is_menu_item_current(last_seen, expiry_days=30, now=self._NOW) is False


class _FakeRedis:
    """Mirrors tests/test_post_category.py's own minimal fake — a `.get()`
    only, matching exactly what `load_menu_expiry_days` calls."""

    def __init__(self, store=None):
        self.store = dict(store or {})

    def get(self, key):
        return self.store.get(key)


class TestValidateMenuExpiryDaysConfig:
    def test_a_positive_integer_is_accepted(self):
        assert validate_menu_expiry_days_config(30) == 30

    def test_zero_is_rejected(self):
        with pytest.raises(ValueError):
            validate_menu_expiry_days_config(0)

    def test_a_negative_integer_is_rejected(self):
        with pytest.raises(ValueError):
            validate_menu_expiry_days_config(-10)

    def test_a_bool_is_rejected(self):
        """`bool` is a subclass of `int` in Python — `True`/`False` must
        never silently pass as 1/0 day windows."""
        with pytest.raises(TypeError):
            validate_menu_expiry_days_config(True)

    def test_a_non_integer_is_rejected(self):
        with pytest.raises(TypeError):
            validate_menu_expiry_days_config("365")


class TestLoadMenuExpiryDays:
    def test_none_redis_falls_back_to_the_default(self):
        days, reason = load_menu_expiry_days(None)
        assert days == DEFAULT_MENU_EXPIRY_DAYS
        assert reason is None

    def test_missing_key_falls_back_to_the_default_without_a_fallback_reason(self):
        days, reason = load_menu_expiry_days(_FakeRedis())
        assert days == DEFAULT_MENU_EXPIRY_DAYS
        assert reason is None  # missing key is the expected pre-first-write state

    def test_reads_the_admin_override(self):
        from app.models.menu_lifecycle import ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY
        import json

        redis = _FakeRedis({ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY: json.dumps(30)})
        days, reason = load_menu_expiry_days(redis)
        assert days == 30
        assert reason is None

    def test_invalid_json_falls_back_to_the_default_with_a_reason(self):
        from app.models.menu_lifecycle import ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY

        redis = _FakeRedis({ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY: "{not json"})
        days, reason = load_menu_expiry_days(redis)
        assert days == DEFAULT_MENU_EXPIRY_DAYS
        assert reason == "invalid_json"

    def test_invalid_shape_falls_back_to_the_default_with_a_reason(self):
        from app.models.menu_lifecycle import ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY
        import json

        redis = _FakeRedis({ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY: json.dumps(-5)})
        days, reason = load_menu_expiry_days(redis)
        assert days == DEFAULT_MENU_EXPIRY_DAYS
        assert reason == "invalid_shape"
