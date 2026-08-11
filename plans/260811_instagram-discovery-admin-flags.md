# Instagram Discovery Switches As Admin Config

## Branch
feature/instagram-discovery-admin-flags

## Goal
Move the two switches that decide whether add-time Instagram discovery can use
its strongest tier — the Google-search source and the LLM judge — out of
deploy-time environment settings and into admin config, so an operator can turn
them on or off from the admin panel and have it take effect on the next add,
with no redeploy and no restart.

These are spend switches. The operator who owns the budget must be able to stop
the spend without waiting on a deploy.

## Non-goals
- Changing what discovery does when a tier is enabled. Scoring, thresholds,
  source order, and the `PROVENANCE_WEIGHT` values stay exactly as they are.
- Making the whole-catalogue operator run admin-configurable. It already takes
  per-run config from the trigger dialog; this plan is about the add-time path.
- Moving any other Instagram setting (timeouts, thresholds, candidate counts)
  into admin config.
- Removing the environment settings. They stay as the fallback and as the
  bootstrap value for a fresh environment.
- Making `add_venue_instagram_enabled` or the deadline admin-managed. The kill
  switch for the hook itself is deliberately a deploy-time setting.

## Evidence
- `app/container.py:50` — `_build_google_search_source()` returns `None` unless
  `settings.instagram_google_search_enabled` is true **and** an Apify token is
  present. `_build_instagram_judge()` (`app/container.py:74`) does the same for
  `settings.instagram_judge_enabled`. Both run **once, at container construction**,
  so today the only way to flip either is an env change plus a restart.
- `app/handlers/add_venue_handler.py:91` — `ADD_VENUE_INSTAGRAM_CASCADE_CONFIG`
  is a module-level constant with `tier_google_search_enabled: True` and
  `judge_enabled: True` baked in. It cannot vary per add.
- `app/handlers/add_venue_handler.py:735` — `_google_only_enabled()` is the
  precedent to copy exactly: read the admin key fresh on every request, the
  admin value wins whenever present (including an explicit `false`), the
  Settings value is the fallback only when the key is absent or the read fails,
  and **any read failure degrades to disabled** because failing open would spend
  money during a config outage.
- `app/services/admin_config_service.py` — `AdminConfigService.set` validates,
  writes RDS (system of record), then mirrors Redis in the same request; `get`
  reads the Redis mirror and falls back to RDS. Config keys are global and need
  immediate read-back, which is exactly this use case.
- `app/container.py:584` — the validator registry. Recent keys
  (`menu_expiry_days`, `post_category_vocabulary`) are registered here and
  written through the **generic** `/admin/config/{key}` CRUD route, with their
  own comments stating that no dedicated endpoint should be added. This key
  follows that same path.
- `app/services/instagram_cascade_service.py:402` — `_source_enabled` already
  honors an explicit per-source key, so a per-add config can enable or disable
  `google_search` with no further change to the cascade.
- `app/services/instagram_cascade_service.py:422` — `_should_adjudicate` already
  honors `judge_enabled: False` per run.
- The judge and the search source each also require a credential
  (`OPENAI_API_KEY`, `APIFY_API_TOKEN`). A flag can enable a tier; it cannot
  conjure a key.

## Current Behavior
`instagram_google_search_enabled` and `instagram_judge_enabled` are read once at
startup. When either is false the corresponding collaborator is never
constructed, so the cascade receives `None` and the tier is permanently absent
for the life of the process. Turning a tier on means editing the environment on
EC2 and redeploying; turning it off in a hurry means the same. An operator
watching spend has no lever.

## Desired Behavior
Both switches must be readable from admin config at add time and take effect on
the next add.

Introduce one admin-config key, `instagram_discovery`, shaped:

```
{"google_search_enabled": bool, "judge_enabled": bool}
```

Resolution order for each field, matching `_google_only_enabled()` exactly:

1. The admin value when the key is present and the field is set — including an
   explicit `false`.
2. Otherwise the corresponding Settings value.
3. On **any** read failure, disabled. A config outage must never start spend.

The collaborators must be constructed whenever their **credential** is present,
independently of the flag, so a runtime enable has something to enable. A
missing credential still means the tier is absent — and when a flag is on but
its credential is missing, that must be logged clearly at startup, because it is
the one state where the panel says "on" and nothing happens.

The add-time cascade config must be built per add from the resolved values
rather than read from a module-level constant. Everything else in that config
(the three free tiers on, `apify_search` off, `suppress_not_found_cache` on)
stays fixed and is not operator-editable — the paid Instagram user search must
not become reachable from the panel by accident.

## Implementation Approach

### 1. The key and its validator
Add `INSTAGRAM_DISCOVERY_CONFIG_KEY = "instagram_discovery"`. Write
`validate_instagram_discovery_config` in `app/services/config_validation.py`
alongside the existing validators: accept a dict whose known fields are
booleans, reject unknown fields and non-boolean values, and return the value to
persist. Register it in the `validators` map in `app/container.py:584`.

No new route. The generic `/admin/config/{key}` CRUD already serves it, which is
what every recent key does.

### 2. Resolution helper
Add a small helper on `AddVenueHandler` next to `_google_only_enabled` that
resolves both fields with the rules above and returns them together, so one
admin read serves both flags per add rather than two.

### 3. Build collaborators on credentials, not flags
Change `_build_google_search_source()` and `_build_instagram_judge()` to
construct whenever their credential is configured, dropping the settings flag
from the construction condition. The flag now gates **use**, not existence.

Log at startup when a flag is on but its credential is missing — the one
silent-failure state this change introduces.

### 4. Per-add config
Replace the module-level `ADD_VENUE_INSTAGRAM_CASCADE_CONFIG` with a builder
that takes the two resolved booleans and returns the config dict. Keep the fixed
entries exactly as they are today, including `tier_apify_search_enabled: False`
as the defence-in-depth guard the existing comment describes.

## Data, Config, And API Impact

**New admin-config key:** `instagram_discovery`, shape above. Written through the
existing generic `/admin/config/{key}` route (RDS system of record + synchronous
Redis mirror). No new endpoint.

**Settings:** `instagram_google_search_enabled` and `instagram_judge_enabled`
remain, demoted to fallback/bootstrap. Their comments must say so, or the next
reader will assume the env var is still authoritative.

**API:** no change to any request or response shape.

**Behavior change on deploy:** none by itself. With the key absent, both fields
fall back to the Settings values, which is exactly today's behavior.

**Persistence:** one new row in `admin.admin_config` once an operator saves.

## Error Handling And Observability
An admin-config read failure disables both tiers for that add and logs a warning
naming the key — the same fail-closed choice `_google_only_enabled` makes, for
the same reason: failing open spends money during an outage.

A flag that is on with its credential missing logs a warning at startup naming
the missing credential. Without this, the panel shows a tier as enabled and
nothing happens, with no way to tell why.

The existing `ADD_VENUE_INSTAGRAM_TOTAL` and the cascade's own tier/paid-call
metrics already attribute what ran; the resolved flag values go in the existing
per-add INFO log line so a past add's behavior can be explained without
reconstructing what the config was at the time.

## Test Plan
Feature file: `tests/bdd/api/instagram-discovery-admin-flags.feature`

Scenarios:
- An add with the admin key absent uses the Settings values, so behavior is
  unchanged from before the key existed.
- An add with `google_search_enabled: true` in admin config attempts the
  Google-search tier even though the Settings value is false.
- An add with `google_search_enabled: false` in admin config does not attempt
  the Google-search tier even though the Settings value is true — an explicit
  admin `false` beats an enabled Setting.
- The same two cases for `judge_enabled`, asserted through whether a
  Google-search candidate is adjudicated.
- An admin-config read failure disables both tiers for that add and the add
  still succeeds.
- Turning a flag on in admin config takes effect on the **next add** with no
  restart — two adds in one run, config changed between them, different tiers
  attempted.
- Enabling `google_search_enabled` never enables the paid `apify_search` tier.
- An invalid write (a non-boolean field, or an unknown field) is rejected and
  the stored value is unchanged.
- A flag on with its credential missing leaves the tier absent and the add still
  succeeds.

Pytest unit tests:
- `tests/test_config_validation.py` — the validator's accept/reject matrix.
- `tests/test_add_venue_handler.py` — the resolution order (admin present wins,
  including explicit false; absent falls back to Settings; read failure
  disables), and that the built per-add config keeps every fixed entry.
- `tests/test_container.py` (or equivalent) — the collaborators are built on
  credential presence rather than on the settings flag.

Manual or integration checks:
- Write the key through the admin panel against a running cs-server, add a
  venue, and confirm from the per-add log line that the tiers matched the panel
  without a restart.

## Acceptance Criteria
- Both switches are settable from admin config and take effect on the next add
  with no redeploy or restart.
- An explicit admin `false` beats an enabled Setting.
- The key being absent reproduces today's behavior exactly.
- Any admin-config read failure disables both tiers rather than spending.
- The paid `apify_search` tier cannot be enabled through this key.
- A flag on with a missing credential is logged at startup and degrades safely.
- An invalid write is rejected without changing the stored value.

## Open Questions
None.
