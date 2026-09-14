# Provenance manifest — `venue_link_audit_reviewer/corpus.json`

See `plans/260914_agentic-venue-resolution-fallback.md` (Evidence §A, §D, §E,
§F, and the Test Plan's own "labeled fixture corpus" item). Matches the
per-item provenance convention `tests/fixtures/venue_link_audit/MANIFEST.md`
already established in this repo — which itself follows
`tests/fixtures/dedup_agentic_discovery/MANIFEST.md` — so a future reader
never has to guess which field is real production data and which is a
labeled reconstruction.

**None of this manifest's production evidence was obtained by writing
anything.** Every `venue_name`, `street`, `neighborhood`, `city` and
`location_text` value in `corpus.json` was **re-verified read-only against
live production on 2026-09-14**, against `origin/main` at `408da88`, from the
`vibes_bot-cs-server-1` container on EC2 instance `i-0893fb6d283243480` via
AWS SSM `send-command`. Addresses came from `venues.address`
(`street` / `neighborhood` / `city`); location texts came from the live,
non-superseded `events.post_item` rows under each `source_handle`. Nothing on
that host was mutated, and none of this branch's own new code existed there
at read time.

`venue_id` values in this fixture (`conchittas_bar`, `recife_beer_cervejaria`,
…) are **readable placeholders** for the real, opaque `ven_...` ids, exactly
as the sibling `venue_link_audit` corpus does.

## Why this corpus exists alongside `venue_link_audit/corpus.json`

That corpus pins what the **audit** flags. This one pins what the
**reviewer** must then conclude about a flagged pair once it is also shown
the venue's real **street address** — the signal plan §D measured as decisive
on 8 of the 12 live flagged pairs. The two corpora share no case.

## Per-case provenance

- **`conchittasbar_street_number_confirms`** — `venue_name`
  ("Conchittas Bar"), `street` ("R. Imperatriz Teresa Cristina 218"),
  `neighborhood` ("Boa Vista") and `city` ("Recife") are verbatim from a live
  `venues.address` read. The four distinct `location_text` spellings are
  verbatim from the handle's live events, reproduced in their real observed
  proportions (the bare street line dominates at ×9). The load-bearing detail
  is the **Teresa/Tereza** spelling difference between the catalogue and every
  post: one letter, and the audit's name+neighbourhood check cannot see the
  street at all. Live K=10 result (plan §F): **confirm 10/10**, verbatim-quote
  gate 10/10. Live audit counts: 28 checkable / 20 non-corroborating; this
  fixture is a representative 12-event subset carrying every distinct spelling,
  not the full live set.
- **`downtownbeergarden_street_number_confirms`** — `venue_name`, `street`
  ("Rua Quarenta e Oito, 490"), `neighborhood` (**"Quintal Espinheiro"** — the
  catalogued value, which is why the posts' plain "Espinheiro" does not match
  it as a folded substring) and `city` are verbatim from a live read. The three
  street spellings (title case, ALL CAPS, and the en-dash variant) are the real
  distinct spellings the crawl produced. Live K=10: **confirm 10/10**. Live
  audit counts: 12 checkable / 6 non-corroborating.
- **`lacasarecife_catalogued_neighbourhood_wrong`** — the decisive case for
  plan §D. `street` ("Av. Dezessete de Agosto 1893") and the **erroneous**
  catalogued `neighborhood` ("Parnamirim") are verbatim from a live read; the
  three `location_text` values are verbatim from the handle's 3 live events,
  including the real misspelling "Dezesseste". Measured live: arm A (name +
  neighbourhood only) returned a confident **contradict 5/5**; arm B (+ street
  + city) returned **confirm 10/10** citing the street. Live audit counts:
  3 checkable / 2 non-corroborating — the fixture reproduces the full live set.
- **`rockandribsmarcozero_short_name_form_confirms`** — `venue_name`
  ("Rock & Ribs Lounge Pub - Marco Zero - Recife/PE"), `street`
  ("Avenida Alfredo Lisboa"), `neighborhood` ("Recife") and `city` are verbatim
  from a live read. "Marco Zero" ×6 and "Rock & Ribs Steakhouse — Marco Zero"
  are verbatim live `location_text` values. The point of the case is a pure
  **scoring artefact**: "Marco Zero" is a literal substring of the venue's own
  catalogue name yet scores 0.4186, under `DEFAULT_CONFIDENCE_FLOOR` (0.55).
  Live K=10: **confirm 10/10**. Live audit counts: 8 checkable / 6
  non-corroborating.
- **`botecobeer_jsp_different_street_contradicts`** — the one real defect in
  the live flagged set, and the case this plan must surface without force-fitting.
  The handle is mapped to `Recife Beer Cervejaria` (`R. Abdon Batista, 300`,
  `Santo Amaro`) — both verbatim from a live read — while all four of its live
  `location_text` values give `Rua Madre Rosa, 181 - Jd. São Paulo` (three
  distinct spellings, all verbatim). The place the posts describe,
  "Boteco Beer Bistrô & Bar", is **not in the catalogue at all**; adding it is
  explicitly out of this plan's scope (Non-goals, Open Question 5). Live K=10:
  **contradict 10/10**. Live audit counts: 4 checkable / 4 non-corroborating —
  the fixture reproduces the full live set.
- **`entreamigosobode_double_mapped_siblings`** — the only **double-mapped**
  handle in the live flagged set, and the measurement behind the structural
  gate (plan §F item 3). Both sibling venues, their streets
  (`R. da Hora, 695` / `R. Marquês de Valença, 50`) and their neighbourhoods
  (`Espinheiro` / `Boa Viagem`) are verbatim from a live read; both `venue_id`s
  map to the SAME `instagram.handle` row live. "Amigos Park" ×12,
  "Entre Amigos" ×4 and the one post naming **both** units' addresses are
  verbatim live values. Asked independently per sibling, the live model
  returned **confirm 10/10 for BOTH** on that shared, mutually-exclusive event
  pool — one run citing house number **30** for the venue catalogued at **50**.
  Live audit counts: 13/12 (Espinheiro) and 18/12 (Boa Viagem); this fixture is
  a representative 17-event subset carrying every distinct spelling.
- **`saladerebocorecife_genuine_offsite_gigs`** — the mapping is **correct**
  and the audit is still right to ask. `street` (`R. Gregório Júnior, 264`) and
  `neighborhood` (`Cordeiro`) are verbatim from a live read; the corroborating
  ALL-CAPS address line and the two non-corroborating values
  ("Praia de Boa Viagem – Quiosque 13", "BONITO – PE") are verbatim live
  `location_text` values — two genuine off-site gigs, not a mapping error.
  Live audit counts: 17 checkable / 2 non-corroborating — the fixture
  reproduces those counts exactly (15 corroborating + 2). Live K=10 result:
  **confirm 7 / insufficient 2 / contradict 1** — deliberately non-unanimous.

  `canned_dissenting_answer` exists because the BDD scenario that uses this
  case names a `confirm`/`insufficient` mixture specifically; the steps
  program 7 confirms and 3 insufficients. The real measured distribution
  (7/2/1, above) is recorded here so the reconstruction is never mistaken for
  the measurement.

## `canned_model_answer` — what it is and is not

`canned_model_answer` (and `canned_dissenting_answer`) are **not** production
reads: they are the raw replies the BDD stub returns in place of a real
`VenueLinkReviewClient` call, so no scenario ever touches the network (this
repo's `CLAUDE.md` forbids it outright). Each one reproduces the **verdict and
matched field the live K=10 run actually reached** for that pair, and each
`evidence_quote` is copied character-for-character out of this case's own
`location_text` list so the verbatim-evidence gate is exercised for real
rather than bypassed. `expected` records what the pass must conclude; it is
what the scenarios assert against.

## PII review

**No PII.** Every `location_text` above names either a venue or a street
address — no personal names, no phone numbers, no handles belonging to a
private individual (every `handle` here is a commercial venue's own public
Instagram account, already a `kind='venue'` crawl target in production). No
redaction was needed.

## What this fixture does NOT prove

- It does not prove the live model still returns these verdicts. It pins the
  **pass's behaviour given a verdict**, with the verdict itself canned — the
  live-model side is re-measured by the plan's own manual/integration checks
  (dry-run against production), never by BDD.
- Each case's event list is a representative subset for the three handles
  marked as such above; the remaining live events' `location_text` values were
  not individually re-fetched and are deliberately **not fabricated** here.
  Every distinct spelling that changes an audit or reviewer outcome is present.
- It says nothing about the four live flagged pairs it omits
  (`beerdock_recife`, `casabacurau`, `emporio_universitarioo`,
  `garage66.recife`); those are shapes this corpus's seven cases already
  cover between them (confirm-on-street, and the mixed-evidence hold-back).
