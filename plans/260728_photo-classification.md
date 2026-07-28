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

## Attributes, per category

Cardinality follows one rule: **one value when a photo can only be one thing; an
array when it can genuinely show several.** Arrays extend without a migration.

Rows marked **[T]** emit the exact labels from `app/models/taxonomy.py` and are
validated by the existing `validate_category_labels`.

### menu

| Field | Card | Values | Why |
|---|---|---|---|
| `legible` | one | `sim` · `parcial` · `nao` | **Gates menu extraction** — stop paying OCR for blurred menus |
| `medium` | one | `impresso` · `lousa` · `placa_parede` · `tela_digital` · `qr_code` · `livreto` | a QR-code photo has no text to extract; a lousa means daily specials |
| `page_side` | one | `frente` · `verso` · `ambos` · `pagina_interna` | so extraction knows whether it has the whole menu |
| `content_scope` | one | `so_comida` · `so_bebida` · `ambos` | a drinks-only board is a bar signal |
| `sections` | array | `entradas` · `petiscos` · `principais` · `sobremesas` · `cervejas` · `drinks` · `vinhos` · `cafe` · `combos` · `infantil` | `infantil` is direct **`familia`** evidence |
| `has_prices` | one (bool) | | gates the price-tier pipeline |
| `is_promo` | one (bool) | | happy hour, rodízio, chopp em dobro → `intencao: Happy hour` |
| `language` | one | `pt` · `en` · `es` · `multi` | a bilingual menu means tourist-facing → `publico: Turistas` |

### food_drinks

| Field | Card | Values | Why |
|---|---|---|---|
| `subject` | one | `comida` · `bebida` · `ambos` | |
| `dish_type` | array | `petisco` · `porcao` · `prato_individual` · `sanduiche` · `pizza` · `frutos_do_mar` · `carne_churrasco` · `massa` · `japonesa` · `sobremesa` · `regional` · `veg` | |
| `drink_type` | array | `cerveja_chopp` · `coquetel` · `caipirinha` · `vinho` · `destilado` · `sem_alcool` · `cafe` · `balde_combo` | |
| `portion_size` | one | `individual` · `para_dividir` · `combo_balde` | sharing plates → `intencao: Sentar com a galera` |
| `plating` | one | `simples` · `caprichado` · `autoral` · `embalado` | `autoral` → `jantar` / `intencao: Comer bem` |
| `setting` | one | `mesa_posta` · `balcao` · `close_up` · `com_pessoas` | a laid table means table service, not a counter |
| `dietary_labels` | array | `vegetariano` · `vegano` · `sem_gluten` · `infantil` | only when visibly labelled |

### interior

| Field | Card | Values | Why |
|---|---|---|---|
| `space_type` | one | `salao` · `balcao_bar` · `pista_danca` · `palco` · `lounge` · `sala_jantar` · `entrada` · `mezanino_vip` · `cozinha_aberta` · `banheiro` | `pista_danca` is the strongest `role_agitado` signal available |
| `estetica` **[T]** | array | Instagramável · Minimalista · Retrô · Underground · Neon · Intimista · Sofisticado · Moderno · Rústico · Vista bonita · Nature vibe | straight into the vibe profile |
| `lighting` | one | `natural` · `quente_baixa` · `neon_colorida` · `escura_balada` · `fluorescente` | `quente_baixa` is what `date` and `clima_social: Intimista` are made of |
| `seating_type` | array | `mesas` · `banquetas_balcao` · `sofas` · `mesas_altas` · `bancos_comunitarios` · `em_pe` · `puffs` | `em_pe` ≈ balada; `bancos_comunitarios` ≈ resenha |
| `music_format` **[T]** | array | DJ · Som ao vivo · Banda ao vivo · Roda de samba · Karaokê · Playlist ambiente · Open mic · Instrumental | a stage, a DJ booth, a karaoke screen — **`music_format` has no photo evidence today** |
| `screens` | one | `telao` · `tvs` · `nenhuma` | *"tem telão pro jogo?"* is one of the most-asked questions about a Brazilian bar and deserves its own field |
| `capacity` | one | `intimo` · `medio` · `amplo` · `multiplos_ambientes` | `multiplos_ambientes` is a real thing people ask about |

### exterior

| Field | Card | Values | Why |
|---|---|---|---|
| `exterior_kind` | one | `fachada` · `area_externa` · `rooftop` · `quintal_jardim` · `calcada` · `pe_na_areia` · `piscina` · `estacionamento` | separates *how do I find the door* from *is there an open-air area* |
| `covered` | one | `descoberto` · `parcial` · `coberto` | the rain question, which nothing else answers |
| `view` | array | `mar` · `cidade` · `natureza` · `rio` · `rua` · `sem_vista` | maps to `estetica: Beira-mar / Vista bonita` |
| `estetica` **[T]** | array | (as above, plus `Ao ar livre`, `Beira-mar`) | |
| `seating_type` | array | (same set as interior) | |
| `venue_name_legible` | one (bool) | | a facade with a readable name both confirms we have the right venue and is the photo to show for *how do I find it* |
| `time_of_day` | one | `dia` · `entardecer` · `noite` | day and night are different venues |

### crowd — *and* the people block

The `crowd` **category** means people are the subject. The `people` **block** is
extracted from **any** photo with visible people, whatever its category —
otherwise every person in every interior shot is thrown away, and `has_kids`,
the one thing `familia` needs, is the field most likely to appear in a photo
filed as something else.

| Field | Card | Values | Why |
|---|---|---|---|
| `crowd_level` | one | `vazio` · `poucas_pessoas` · `movimentado` · `cheio` · `lotado` | corroborates busyness with a second, independent source |
| `publico` **[T]** | array | Galera jovem · Galera 30+ · Galera 50+ · Família · Casais · Turistas · Alternativo · Gótico · LGBTQ+ · Artistas / criativos · Público misto | this **is** the age-range and scene read, in the vocabulary the product already speaks |
| `has_kids` | one (bool) | | **the `familia` unlock.** Kept as its own boolean rather than trusting `publico: Família`, because it is a fact, not an impression |
| `dress_code` **[T]** | array | Casual · Arrumadinho · Esporte fino · Praia · Alternativo · Sem dress code | the generic read |
| `dress_scene` | array | `rock_metal` · `rap_hip_hop` · `funk_baile` · `sertanejo` · `alternativo_indie` · `queer` · `praia` · `esportivo` · `fantasia_tematico` | the specific read — subculture, which `dress_code` is too coarse for and which is exactly what people pick a night out by |
| `group_type` | array | `casais` · `amigos` · `familias` · `sozinhos` · `grupo_grande` · `turistas` | `casais` → `date`; `grupo_grande` → `resenha` |
| `activity` | array | `dancando` · `bebendo` · `comendo` · `conversando` · `assistindo_show` · `assistindo_jogo` · `karaoke` · `fila` | `dancando` is the single best `role_agitado` signal; `fila` means the place is hot |
| `clima_social` **[T]** | one | Intimista · Social · Animado · Agitado · Fervendo · Tranquilo | direct to the vibe profile |
| `time_of_day` | one | `dia` · `entardecer` · `noite` | a Tuesday-afternoon crowd is not a Saturday-night crowd |

### other

| Field | Card | Values | Why |
|---|---|---|---|
| `other_kind` | one | `logo_arte` · `flyer_evento` · `documento_aviso` · `pessoa_isolada` · `irrelevante` · `ilegivel` | knowing **why** it is `other` lets us reclassify later without re-billing |

`flyer_evento` is the likeliest future promotion out of `other`: a flyer carries
the event, the DJ, the date and the cover charge — a whole extraction pipeline
of its own. Naming it now means we can find those photos when we want them.

### Shared, on every photo

| Field | Card | Values | Why |
|---|---|---|---|
| `category` | one | the six above | |
| `confidence` | one | 0–1 | below threshold → `other` |
| `quality` | one | `boa` · `escura` · `borrada` · `baixa_resolucao` | **which photo do we show in the app.** Feeds directly into the in-flight `venue-list-hero-photo` work — an `interior`, `quality: boa`, `crowd_level: movimentado`, `lighting: quente_baixa` photo is a good hero, and now that is a query |
| `people` | block | the crowd block above, when people are visible | |

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

### 2. Two passes, not one

- **Pass 1 — categorize.** A small prompt, one label + confidence + quality per
  image, batched. Runs **before storing** so photos land in the right folder with
  no second write and no object copy.
- **Pass 2 — attributes.** Photos grouped by category, one focused prompt per
  category. `other` is skipped entirely.

Two passes rather than one six-schema mega-prompt because a focused prompt is
measurably more accurate, `other` costs nothing in pass 2, and — the operational
reason — **adding a field later re-runs one category, not the catalogue.** Since
the writer role can now `GetObject` on `retrieved/*`, that re-run reads archived
photos from S3 and never re-pays the provider. The classifier therefore takes a
list of image urls and does not care whether they are provider CDN links (live)
or presigned S3 keys (backfill).

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
unavailable is the wrong trade. Pass 2 failing leaves a categorized photo with no
attributes — still useful, and re-runnable from S3.

Low confidence files as `other` rather than guessing. A wrong label is worse than
an honest unknown, because everything downstream will trust it.

## Data, config, and API impact

- **API:** none. Categories and attributes are archive-internal.
- **Persistence:** none, no migration. Labels ride in the manifest and the folder
  name.
- **New settings:** `photo_classification_enabled` (true),
  `photo_classification_model` (`gpt-4o-mini`), `photo_classification_confidence`
  (0.6, matching the menu filter), `photo_classification_batch_size` (10),
  `photo_attributes_enabled` (true — pass 2, separately switchable).
- **Cost:** ~**$2 for the full 17k-photo catalogue**, of which pass 2 is roughly
  three quarters (85 input tokens per image at low detail; the attribute JSON is
  what costs). That is one twentieth of a single month of SearchApi's $40 plan,
  and it is a one-off rather than a subscription. Metered, not assumed.

## Error handling and observability

| Failure | Behavior |
|---|---|
| Model unavailable / errors | photos keep the source category; run continues |
| A label outside the vocabulary | dropped; the rest of the verdict is kept |
| Confidence below threshold | `other` |
| Malformed JSON | that batch falls back; logged with the venue id |
| Pass 2 fails | photo stays categorized, attributes absent |
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
- Pass 2 failing leaves the photo categorized with no attributes.
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
  in pass 2.
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
