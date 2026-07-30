# The Instagram Probe Must Fail Open

## Branch
fix/instagram-probe-fails-open

## Goal
Stop reporting "this profile does not exist" when the truth is "Instagram would
not tell us". Absence must require positive evidence.

## Non-goals
- **Restoring probe reachability from production.** Instagram blocks the
  datacenter IP; getting real answers again needs an egress change (a proxy)
  and is a separate, costed decision. This plan makes the pipeline behave
  correctly *while* the probe cannot see anything.
- The website-scrape tier, the paid sweep, and the judge.

## Evidence

Measured today against the live site, same two handles from two IPs:

| handle | from a residential IP | from the production box |
|---|---|---|
| `tasquinhadotio` (real) | `og:title` present | 302 → `/accounts/login/`, no og tags |
| `thisaccountdoesnotexist99xq` | no `og:title` | 302 → `/accounts/login/`, no og tags |

From production every response is ~622KB of login wall, HTTP 200, no Open Graph
tags, for real and fake handles alike.

`parse_profile_body` treats a missing `og:title` as proof of non-existence:

    if not title or (og_type and og_type != "profile"):
        return ProfileProbeResult(existence=EXIST_ABSENT)

So in production the probe answers ABSENT for **every** handle. `discover()`
then discards the candidate outright:

    if probe_result is not None and probe_result.existence == EXIST_ABSENT:
        continue

Every candidate, from every tier, is thrown away before it can be scored. This
is why the cascade wrote 312 `not_found` stamps and zero handles, and it will
silently defeat the 291 handles now resolvable from Google websites.

The design already states the intended contract — `app/metrics.py`:

    # `unknown` is a probe failure, NOT evidence of absence

The parser does not honour it.

## Current Behavior
Any page without profile Open Graph tags is read as a confirmed absence,
including login walls, challenges, redirects, rate-limit pages, timeouts that
return a body, and empty responses. A blocked probe is indistinguishable from a
deleted account, and it deletes handles: a venue that finalizes `not_found` has
its stored handle overwritten with NULL.

## Desired Behavior
1. Report ABSENT only on positive evidence that Instagram served a real page
   for a handle that has no profile.
2. Report UNKNOWN when Instagram did not answer the question — login wall,
   challenge, redirect away from the profile, empty body, or a body with no
   recognisable Instagram markers.
3. Keep reporting PRESENT unchanged for a genuine profile page.
4. Make the blocked state VISIBLE rather than silent: a distinct metric label,
   so "we are being blocked" never again looks like "these venues have no
   Instagram".
5. Never let an UNKNOWN probe discard a candidate — the cascade must fall back
   to provenance and name similarity, which is what tier 1's 0.75 weight is for.

## Implementation Approach

The parser gains one distinction: did Instagram serve us a profile page at all?

- A body carrying profile Open Graph tags -> PRESENT (unchanged).
- A body that is recognisably Instagram's own "no such profile" response ->
  ABSENT.
- Anything else -> UNKNOWN.

The absence marker is taken from what the site actually returns to a crawler for
a missing handle: a 200 with Instagram's page shell and no profile og tags, and
critically NOT redirected to `/accounts/login/`. The login redirect is the block
signal, and the fetch must observe it — the final URL, not just the body.

`discover()` already treats UNKNOWN as "no bonus, keep going"; the only change
there is that the skip must fire on ABSENT alone, which it already does. No
cascade change is expected — this plan asserts that behavior with a scenario so
it cannot regress.

## Data, Config, And API Impact
- Persistence, API, migrations: none.
- Behavioral change: venues whose candidate previously finalized `not_found`
  because of a blocked probe will now be accepted on provenance. That is the
  point.

## Error Handling And Observability
- A transport error stays UNKNOWN, as today.
- `INSTAGRAM_PROFILE_PROBE_TOTAL{result}` gains a `blocked` label so a blocked
  egress is a visible spike rather than a slow leak of false absences.
- The first block in a run logs once at WARNING with the final URL; per-handle
  logging would flood a 1,400-venue run.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-probe-fails-open.feature`

Scenarios:
- Report a real profile as present.
- Report a login wall as unknown, not absent.
- Report a challenge page as unknown.
- Report a redirect away from the profile as unknown.
- Report an empty body as unknown.
- Report Instagram's genuine no-such-profile page as absent.
- Keep a candidate when the probe is unknown.
- Discard a candidate only when the profile is confirmed absent.
- Count a blocked probe distinctly from an absent one.

Pytest unit tests:
- `tests/test_instagram_probe_fail_open.py` — parses the REAL captured login-wall
  body shape (200, no og tags, `/accounts/login/` final URL) as unknown; the real
  profile body as present; empty/whitespace/HTML-fragment bodies as unknown; and
  asserts no input shape other than the confirmed-absent one yields ABSENT.

Manual or integration checks:
- Re-run the scoped dry run on the box and confirm the 38 venues now finalize
  with a handle instead of `not_found`.

## Acceptance Criteria
- A login wall never yields ABSENT.
- A genuine missing profile still yields ABSENT.
- A real profile still yields PRESENT with its display name and image.
- An UNKNOWN probe never discards a cascade candidate.
- Blocked probes are counted under their own label.
- `make test-bdd` and `make test-unit` pass; `@wip` removed.

## Open Questions
- None. The behavior is reproduced from production against the live site.
