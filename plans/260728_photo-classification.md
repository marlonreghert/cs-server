# Classify archived photos into our own categories

## Branch
feature/photo-classification

## Goal

Label every archived photo with **our** taxonomy and per-category attributes,
using the vision model we already run — instead of paying a scraper for Google's
four tabs, which do not include the signals worth most to this product.

Google can tell us `menu / food_drink / vibe / latest`. It cannot tell us who is
in the room, whether there are children, whether the menu is even legible, or
whether that terrace has a roof when it rains.

**The design principle for attributes:** wherever a photo can answer a question
`app/models/taxonomy.py` already asks, emit **that taxonomy's exact labels** —
not a parallel vocabulary that has to be translated later. Everything else is a
photo-native fact the taxonomy has no home for.

## Revised after review (2026-07-28)

Three decisions changed after this plan was written; the sections below are kept
for the reasoning that still holds, but **`app/models/photo_taxonomy.py` is the
source of truth for the vocabulary.**

1. **Generic and coarse, not precise.** A model reading a thumbnail cannot
   reliably tell a caipirinha from a batida, and a label it gets wrong is worse
   than one it never emitted. Every attribute list was cut to 3-5 values plus
   `other`, the values are English, and the schema went from ~45 fields to ~18.
2. **One pass, not two.** With a schema this small a focused per-category prompt
   buys little, and two passes send every image twice — image tokens are the
   bill. Category, attributes and the people block now come back from one call,
   halving the cost to ~$1 for the catalogue.
3. **No taxonomy-aligned `[T]` fields.** `estetica`, `publico`, `dress_code`,
   `clima_social` were dropped and `music_format` became a coarse
   `music_setup`. The photo vocabulary no longer speaks the venue vocabulary;
   `vibe_classifier` gets a mapping layer if it ever consumes photo labels.

And one rule this plan did not have:

4. **Per-attribute confidence.** The model reports a confidence for each
   attribute, not one for the photo. Below `photo_attribute_confidence` (0.8,
   deliberately higher than the category's 0.6) the value becomes
   `not_classified` on its own, without dragging its neighbours down.
   `not_classified` is **stored**, not omitted: it means "asked, could not
   tell", which an absent key does not say. A second non-answer,
   `not_applicable`, marks the question that does not arise at all.


## Non-goals

- **Replacing the vibe classifier.** `vibe_classifier_service` derives the
  venue-level 8-category profile. This produces the per-photo evidence it should
  later read.
- **Re-classifying SearchApi photos.** That source returns Google's real tab, so
  its category is authoritative and must not be overwritten by a guess.
- **Aggregating to venue level.** Per-photo facts only.
- **Inferring race, ethnicity, or individual gender** from photographs. See
  *What we deliberately do not label*.

## Evidence

- `app/models/taxonomy.py:7` — the fixed 8-category vocabulary. Six of its
  categories are visible in a photograph: `estetica`, `clima_social`,
  `dress_code`, `publico`, `music_format`, `intencao`. `validate_category_labels`
  already filters model output against it, so aligned attributes need no new
  validation code.
- `app/services/vibe_classifier_service.py:40` —
  `PHOTO_PRIMARY_CATEGORIES = {"estetica", "estilo_do_lugar", "dress_code",
  "clima_social"}`: **four of the eight are already photo-derived**, so aligned
  labels feed an existing consumer as a vote count rather than a mapping layer.
- `app/api/openai_menu_client.py:169` — `classify_menu_photos(photo_urls, model,
  confidence_threshold)` already batches images to `gpt-4o-mini` and returns a
  per-image JSON verdict with a confidence. The shape to reuse.
- `vibes_bot/app/services/vibe_modes_service.py` — the 8 modes. `familia` gates
  on `requires_family_signal`, which today can only come from venue type;
  `role_agitado` wants `estilo_do_lugar: [Balada, Pista de dança]`; `role_calmo`
  and `date` want `clima_social: [Tranquilo, Intimista]`.
- `app/services/venue_photo_archive_service.py` — `_archive_venue` fetches, then
  `_store_photo` files each photo under `media/<category>/`.
- `app/api/apify_gmaps_extractor_client.py` — `authorship` (`by_owner` /
  `by_visitor` / `unknown`) is already a separate field from `category`, so a
  classifier may set the category without destroying who took the photo.
- `tests/test_photo_metadata_fidelity.py:25` — there is already a test asserting
  authorship survives a category reassignment. This feature is what it was
  written against.
- `infra/datalake/iam.tf` — the writer role now has `GetObject` on `retrieved/*`,
  so a re-run can read archived photos back **without re-fetching from the
  provider**. That is what makes extending the attribute schema cheap later.
- Provider photo urls are **public keyless CDN links**
  (`lh3.googleusercontent.com`), so live classification needs no presigning.

## The six categories

| Category | Rule |
|---|---|
| `menu` | the subject is a menu, price board, or drinks list |
| `food_drinks` | the subject is a dish, a drink, or a laid table |
| `interior` | an enclosed space of the venue — **a roof overhead** |
| `exterior` | **open air: the sky is visible.** Facade, terrace, rooftop, quintal, calçada, beira-mar |
| `crowd` | people are the subject — you can read who is there |
| `other` | none of the above |

`interior` replaces the earlier `ambiente`: it is the true opposite of
`exterior`, and it avoids colliding with "vibe", which already means the venue
profile, the modes, and the tags.

**`exterior` is decided by the sky, not by the subject.** A facade shot and a
rooftop shot are both exterior; a covered varanda is interior. That keeps the
call objective — "is there open air overhead" is something a model gets right,
"is this outdoorsy" is not. The product distinction between *how do I find the
door* and *is there an open-air area* is then carried by `exterior_kind`, below.

**Precedence, since these are not naturally exclusive:** a photo is `crowd` only
when people are the subject. People at the edges of a room shot leave it
`interior` — but the people attributes are still extracted from it (see
*The people block*).

## Attributes, per category — SUPERSEDED, see the revision above

The tables that were here listed ~45 fields in pt-BR, several of them aligned to
`app/models/taxonomy.py`. What shipped is coarser and generic. Current schema,
generated from `app/models/photo_taxonomy.py`:

| Category | Attributes |
|---|---|
| *(interior, exterior, crowd only)* | `time_of_day`: day · night |
| `menu` | `legible`: yes · partial · no — **gates menu extraction**<br>`has_prices`: yes · no — gates the price tier<br>`covers`: food · drinks · both |
| `food_drinks` | `subject`: food · drinks · both<br>`drink_type`: beer · cocktails · wine · non_alcoholic · other<br>`food_type`: snacks · main_dish · dessert · other<br>`portion`: individual · shareable |
| `interior` | `space_type`: dining · bar · dance_floor · stage · other<br>`lighting`: bright · dim · dark<br>`has_screens`: yes · no<br>`music_setup`: live_music · dj · none_visible |
| `exterior` | `exterior_kind`: facade · open_air_area · other<br>`covered`: covered · partial · uncovered<br>`view`: sea · city · nature · none |
| `crowd` | the people block, below |
| `other` | `other_kind`: logo_art · event_flyer · document · irrelevant |

**The people block** — `crowd_level`: empty · some · busy · packed ·
`has_kids`: yes · no · `group_type`: couples · friends · families · mixed ·
`activity`: dancing · eating_drinking · watching · other.

The `crowd` **category** means people are the subject. The block is extracted
from **any** photo with visible people, whatever its category — otherwise every
person in every interior shot is thrown away, and `has_kids`, the one thing
`familia` needs, is the field most likely to appear in a photo filed as
something else. A photo with nobody in it has no people block at all, which is
different from one whose people could not be read.

Every attribute also accepts `not_applicable` ("the question does not arise for
this photo") and `not_classified` ("asked, could not tell"), and every field of
the category's schema is written on every classified photo. The two are kept
apart because conflating them makes an accurate classifier read as a failing
one. Cardinality is single-valued
throughout: an array invites the model to hedge by listing everything plausible,
which is the imprecision this schema exists to avoid.

Top-level on the entry, beside the attributes: `category`, `confidence`,
`quality` (good · poor · not_classified), `source_category`, `authorship`,
`likely_authorship`.

## Who took the photo

This must survive, and it is worth more than it looks: **owner photos are
marketing and visitor photos are evidence.** An owner's empty, well-lit,
professionally-plated room tells you what the venue wants to be; a visitor's
handheld Friday-night shot tells you what it is. For crowd and vibe signals the
visitor photos should eventually outweigh the owner's; for menus the owner's are
usually the legible ones.

Two fields, never conflated:

- **`authorship`** — the provider's fact (`by_owner` / `by_visitor` / `unknown`),
  set at fetch time from `authorName` vs the venue title. Classification **never
  writes this field**. `tests/test_photo_metadata_fidelity.py:25` already guards
  it.
- **`likely_authorship`** — the model's read, written **only when the provider
  said `unknown`** (Google Places gives us no author for most photos). Staged,
  empty, evenly-lit, professionally plated reads as owner; handheld, candid, with
  people, mixed light reads as visitor. Stored under a different name so a guess
  can never be mistaken for the fact.

## What we deliberately do not label

- **Race, ethnicity, and individual gender.** The model is unreliable at it, the
  product has no question that needs it, and building a venue-recommendation
  system that profiles crowds by those attributes is a line worth not crossing.
  `publico` and `dress_scene` read the *scene* — what a place is, from how people
  chose to present — not who individuals are.
- **`LGBTQ+` / `queer` is read as a scene signal**, from aggregate cues (pride
  decor, the venue's own presentation, the scene), never as a claim about any
  individual in the frame. It stays in because it is already in `publico`, and
  because "is this place queer-friendly" is a question people actively search
  for, not one imposed on them.
- **Cleanliness, hygiene, and attractiveness.** Subjective, and a defamatory
  read of a real business is a liability with no upside.

## Implementation approach

### 1. Taxonomy in code

`app/models/photo_taxonomy.py`, mirroring `taxonomy.py`: `PHOTO_CATEGORIES`,
plus `PHOTO_ATTRIBUTES: dict[category, dict[field, spec]]` where a spec carries
cardinality, the allowed values, and — for **[T]** fields — the
`app/models/taxonomy.py` key to defer to, so the vocabulary lives in exactly one
place.

### 2. One pass — SUPERSEDED, see the revision above

This plan called for two passes: categorize, then a focused per-category
attribute prompt. What shipped is **one call** returning the category, that
category's attributes and the people block together.

The argument for two was that a focused prompt is measurably more accurate than
one carrying every schema at once. That holds for the ~45-field schema this plan
described; it does not hold for the ~18-field coarse one that shipped, and two
passes send every image twice when image tokens are the bill. `other` costing
nothing in pass 2 stopped mattering once `other_kind` moved into the single
verdict.

What survives: the classifier takes a list of image urls and does not care
whether they are provider CDN links (live) or presigned S3 keys (backfill).
Since the writer role can `GetObject` on `retrieved/*`, extending the schema
re-runs over archived photos and never re-pays the provider — that re-run
**discards the category the model returns**, because the category is in the S3
key of an object that already exists.

### 3. Client

`app/api/openai_photo_classifier_client.py`, modelled on `classify_menu_photos`:
batch image urls, one JSON verdict per image, every label validated against the
fixed vocabulary — **[T]** fields through the existing
`validate_category_labels` — so an invented label is dropped, not stored.

### 4. Where it runs

In `_archive_venue`, between fetch and store. It runs only when the source does
not provide real categories: `ArchiveSource` gains `provides_categories` —
`True` for `searchapi_gmaps_photos`, `False` for Apify and the Places API. A run
may force it off.

### 5. Degrading

Classification is an **enhancement, not a dependency.** If the model fails, is
disabled, or returns nothing, the photo keeps its source category and is still
archived. Losing photos we already paid to fetch because a classifier was
unavailable is the wrong trade.

Low confidence files as `other` rather than guessing, and a low-confidence
ATTRIBUTE becomes `not_classified` on its own. A wrong label is worse than an
honest unknown, because everything downstream will trust it.

## Data, config, and API impact

- **API:** none. Categories and attributes are archive-internal.
- **Persistence:** none, no migration. Labels ride in the manifest and the folder
  name.
- **New settings:** `photo_classification_enabled` (true),
  `photo_classification_model` (`gpt-4o-mini`), `photo_classification_confidence`
  (0.6, the CATEGORY bar, matching the menu filter),
  `photo_attribute_confidence` (0.8, the per-ATTRIBUTE bar — higher because a
  wrong category misfiles a photo while a wrong attribute is read as a fact),
  `photo_classification_batch_size` (10), `photo_attributes_enabled` (true — the
  attribute half of the call, separately switchable).
- **Cost:** ~**$1 for the full 17k-photo catalogue** now that each photo is sent
  once (85 input tokens per image at low detail; the attribute JSON is what
  costs). One fortieth of a single month of SearchApi's $40 plan, and a one-off
  rather than a subscription. Metered, not assumed.

## Error handling and observability

| Failure | Behavior |
|---|---|
| Model unavailable / errors | photos keep the source category; run continues |
| A label outside the vocabulary | dropped; the rest of the verdict is kept |
| Confidence below threshold | `other` |
| Malformed JSON | that batch falls back; logged with the venue id |
| An attribute below the confidence bar | that attribute is `not_classified`; its neighbours are unaffected |
| An attribute with no confidence reported | `not_classified` — nothing to check it against |
| Classification disabled | no call, no cost |
| Source provides real categories | not called at all |

Metrics: photos classified by resulting category, attribute coverage per
category, fallbacks by reason, estimated cost, batch latency.

## Test plan

Feature file: `tests/bdd/enrichment/photo-classification.feature`

Scenarios:
- A photo is filed under its classified category, not its authorship.
- `authorship` survives classification and is still in the manifest.
- `likely_authorship` is written only when the provider said `unknown`, and
  never overwrites a known `authorship`.
- A sky-visible facade is `exterior`; a covered varanda is `interior`.
- People at the edge of a room shot leave it `interior`, and the people block is
  still extracted from it.
- A crowd photo with children records `has_kids`, which `familia` needs.
- A menu photo records `legible` and `has_prices`.
- An illegible menu is marked so, and menu extraction skips it.
- Each category's attributes come from that category's schema only.
- A **[T]** field emits taxonomy labels and rejects a non-taxonomy label.
- A low-confidence verdict files the photo as `other` with an `other_kind`.
- An invented label is dropped and the rest of the verdict survives.
- A model failure leaves every photo archived under its source category.
- A verdict with no attributes still records the whole schema as `not_classified`.
- A source that provides real categories is never classified.
- Classification and attributes can each be disabled per run, and then cost
  nothing.
- One venue's photos classify in one batched call per pass, not one per photo.
- Attributes can be re-derived for an existing run from S3 without re-fetching
  from the provider.

Pytest unit tests:
- Every category and attribute value accepted; anything outside rejected.
- **[T]** fields validate through `validate_category_labels` against the real
  `TAXONOMY`, so a taxonomy edit cannot silently desync the two vocabularies.
- Cardinality: array fields accept arrays, single-valued fields reject them.
- Confidence threshold at, above, and below the boundary.
- Category precedence: crowd-vs-interior, exterior-by-sky.
- The people block attaches to non-`crowd` categories.
- `likely_authorship` is written only for `unknown`, and `authorship` is never
  mutated.
- Batching: batch size respected, remainder batch, empty input, `other` skipped
  when attributes are switched off.
- Each failure mode maps to its documented behavior.
- Cost estimate arithmetic.
- The generated prompts name every category, field, and allowed value, so a
  taxonomy change cannot leave the prompt stale.

## Acceptance criteria

- Archived photos carry the six categories, their category's attributes, and the
  people block wherever people appear.
- `exterior` means the sky is visible; `interior` means a roof.
- **[T]** attributes are the exact labels from `app/models/taxonomy.py`, with no
  parallel vocabulary to maintain or translate.
- `authorship` is never written by classification; `likely_authorship` fills in
  only where the provider was silent.
- `music_format`, `publico`, and `has_kids` — three things with no photo evidence
  today — are populated.
- A classifier failure never costs a photo that was already fetched.
- Low confidence is `other`, never a guess.
- The catalogue classifies for a couple of dollars, and the spend is visible.
- Attributes can be extended and re-derived from S3 without re-paying a provider.

## Open questions

None. One recorded judgement: photo labels are **historical** — a packed 2019
dancefloor does not prove a venue is agitado today — so any venue-level
aggregation built on this must weight by `uploaded_at`. That is a further reason
to prefer Apify (which returns dates) over SearchApi (which does not) as the
photo source.
