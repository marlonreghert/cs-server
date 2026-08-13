Feature: Event dedup by fuzzy title
  As an operator
  I want two rows at one venue on one night collapsed when one title's
  distinctive words are contained in the other's, and merely proposed when they
  only overlap
  So that one party stored under six title variants becomes one listing, while
  two genuinely different events on the same night stay two listings

  Background:
    Given the venue "Conchittas Bar" exists
    And the venue "Sempre Rock Bar" exists
    And the venue "Entre Amigos O Bode" exists
    And the venue "Casanova Ecobar" exists
    And the venue "Sala de Reboco" exists
    And the candidate window is 8 hours
    And the generic event vocabulary includes "aniversario", "oficina", "sextou", "anos" and "semana"

  # ── the auto band ──────────────────────────────────────────────────────────

  Scenario: Merge a shortened title into its fuller form on the same night
    Given an item "Rodolpho" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then one item remains at "Conchittas Bar" for that night
    And the surviving item is titled "Rodolpho Produções"
    And the surviving item carries both source posts

  Scenario: Merge the whole Rodolpho cluster into one listing
    Given the following items at "Conchittas Bar":
      | title                             | starts_at local  |
      | Aniversário do Rodolpho Produções | 2026-08-07 19:00 |
      | 31º Rodolpho Produções            | 2026-08-07 19:00 |
      | Rodolpho                          | 2026-08-07 21:00 |
      | Rodolpho Produções                | 2026-08-07 21:00 |
      | Aniversário do Rodolpho Produções | 2026-08-08 00:00 |
    When the merge pass runs
    Then exactly one item remains for that night at "Conchittas Bar"
    And that item carries all five source posts

  Scenario: Merge two items whose starts straddle midnight
    Given an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And an item "Aniversário do Rodolpho Produções" at "Conchittas Bar" starting 2026-08-08 00:00 local
    When the merge pass runs
    Then the two items are merged

  Scenario: Merge two identical titles stored on different days but one local date
    Given an item "Homenagem aos 98 anos de Onildo Almeida" at "Sala de Reboco" starting 2026-08-07 00:00 local
    And an item "Homenagem aos 98 anos de Onildo Almeida" at "Sala de Reboco" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then the two items are merged

  # ── the refuse band ────────────────────────────────────────────────────────

  Scenario: Refuse to merge two workshops that share only their series prefix
    Given an item "Oficina Vida de Inseto" at "Entre Amigos O Bode" starting 2026-07-15 14:00 local
    And an item "Oficina Cobra Gigante" at "Entre Amigos O Bode" starting 2026-07-15 14:00 local
    And an item "Oficina de Sorvete" at "Entre Amigos O Bode" starting 2026-07-15 14:00 local
    When the merge pass runs
    Then all three items remain
    And no merge is suggested for any pair of them

  Scenario: Refuse to merge four numbered weeks of one programme
    Given the following items at "Entre Amigos O Bode" starting 2026-07-15 10:00 local:
      | title                        |
      | Férias Amigos Park — Semana 1 |
      | Férias Amigos Park — Semana 2 |
      | Férias Amigos Park — Semana 3 |
      | Férias Amigos Park — Semana 4 |
    When the merge pass runs
    Then all four items remain

  Scenario: Refuse to merge two acts that share only a surname
    Given an item "Bolinha do Cavaco" at "Casanova Ecobar" starting 2026-08-07 20:00 local
    And an item "JB do Cavaco" at "Casanova Ecobar" starting 2026-08-07 20:00 local
    When the merge pass runs
    Then both items remain

  Scenario: Refuse to merge a generic weekday post into a named party
    Given an item "SEXTOU NO CONCHITTAS BAR!" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then both items remain
    And no merge is suggested for that pair

  Scenario: Refuse to merge two items at different venues on the same night
    Given an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And an item "Rodolpho Produções" at "Sala de Reboco" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then both items remain
    And no merge is suggested for that pair

  Scenario: Refuse to merge two items at the same venue a week apart
    Given an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-14 21:00 local
    When the merge pass runs
    Then both items remain

  # ── the suggest band ───────────────────────────────────────────────────────

  Scenario: Suggest a merge for two talks that differ only in the speaker
    Given an item "Ação Leitura: Bate-papo com Marcelino Freire" at "Sempre Rock Bar" starting 2026-08-04 19:00 local
    And an item "Ação Leitura: Bate-papo com Jeferson Tenório" at "Sempre Rock Bar" starting 2026-08-04 19:00 local
    When the merge pass runs
    Then both items remain
    And a merge is suggested for that pair
    And the suggestion states each title's distinctive words

  Scenario: Show merge suggestions on the review queue
    Given a suggested merge between two items at "Sempre Rock Bar"
    When the review queue is read
    Then each of the two items carries the suggestion
    And each suggestion names the other item

  Scenario: Apply a suggestion only when an operator asks for it
    Given a suggested merge between two items at "Sempre Rock Bar"
    When the merge pass runs again
    Then both items still remain
    When an operator applies the suggestion
    Then one item remains
    And the absorbed item is superseded

  # ── the undated row ────────────────────────────────────────────────────────

  Scenario: Absorb an undated item into its dated twin from the same account
    Given an item "Aniversário do RODOLPHO Produções" at "Conchittas Bar" with no date, from the account "conchittasbar"
    And an item "Aniversário do Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 19:00 local, from the account "conchittasbar"
    When the merge pass runs
    Then one item remains
    And the surviving item has the date 2026-08-07
    And the surviving item carries both source posts

  Scenario: Refuse to absorb an undated item whose distinctive words are only a subset
    Given an item "Rodolpho" at "Conchittas Bar" with no date, from the account "conchittasbar"
    And an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 19:00 local, from the account "conchittasbar"
    When the merge pass runs
    Then both items remain

  Scenario: Refuse to absorb an undated item from a different account
    Given an item "Aniversário do Rodolpho Produções" at "Conchittas Bar" with no date, from the account "oquetemhojeemnatal"
    And an item "Aniversário do Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 19:00 local, from the account "conchittasbar"
    When the merge pass runs
    Then both items remain

  Scenario: Refuse to absorb an undated item first seen long after its twin
    Given an item "Aniversário do Rodolpho Produções" at "Conchittas Bar" with no date, first seen 2026-10-01
    And an item "Aniversário do Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 19:00 local
    When the merge pass runs
    Then both items remain

  Scenario: Leave menu items out of the title similarity path
    Given a menu item "Especial do dia" at "Conchittas Bar" with no date
    And a menu item "Especial do dia da casa" at "Conchittas Bar" with no date
    When the merge pass runs
    Then the title similarity path considered neither item

  # ── operator protections ───────────────────────────────────────────────────

  Scenario: Never absorb a confirmed item
    Given a confirmed item "Rodolpho" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then the confirmed item still exists
    And the confirmed item is still confirmed

  Scenario: Never absorb an item whose operator edited its title, but still suggest it
    Given an item "Rodolpho" at "Conchittas Bar" starting 2026-08-07 21:00 local whose operator edited its title
    And an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then both items remain
    And a merge is suggested for that pair

  Scenario: Never absorb an item whose operator edited its venue
    Given an item "Rodolpho" at "Conchittas Bar" starting 2026-08-07 21:00 local whose operator edited its venue
    And an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then both items remain

  Scenario: Leave a group with two confirmed members entirely alone
    Given a confirmed item "Rodolpho" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And a confirmed item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And an item "Aniversário do Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then all three items remain unchanged

  # ── reversibility and audit ────────────────────────────────────────────────

  Scenario: Supersede rather than delete the item a title merge absorbs
    Given an item "Rodolpho" at "Conchittas Bar" starting 2026-08-07 21:00 local
    And an item "Rodolpho Produções" at "Conchittas Bar" starting 2026-08-07 21:00 local
    When the merge pass runs
    Then the absorbed item is still readable
    And the absorbed item is superseded
    And the absorbed item records the item that absorbed it
    And the absorbed item's source posts are attached to the surviving item

  Scenario: Reverse an applied merge
    Given a title merge that absorbed one item into another
    When an operator reverses the merge
    Then the absorbed item is no longer superseded
    And the absorbed item carries its own source post again
    And the previously surviving item no longer carries that source post

  Scenario: Keep the exact identity merge behaving exactly as it does today
    Given two items with the same venue, the same date and the same normalized title
    When the merge pass runs
    Then the two items are merged by the exact identity rule
    And the absorbed item is deleted, not superseded

  # ── identity must not move ─────────────────────────────────────────────────

  Scenario: Leave the stored content identity unchanged
    Given the production titles from the Conchittas cluster
    When each title's content identity key is computed
    Then every key equals the key stored for it before this feature shipped

  # ── shared lineup: an independent auto-merge signal (§B2) ──────────────────

  Scenario: Merge two rows sharing two performers even though neither title contains the other
    Given an item "ONILDO ALMEIDA & CONVIDADOS" at "Sala de Reboco" on 2026-08-14
    And an item "Homenagem aos 98 anos de Onildo Almeida" at "Sala de Reboco" on 2026-08-14
    And both items list "Cezzinha", "Josildo Sá" and "Silvério Pessoa"
    When the merge pass runs
    Then the two items are merged
    And the merge records shared lineup as its reason

  Scenario: Merge a row whose title shares nothing with its twin but whose lineup matches
    Given an item "Rodolpho Produções" at "Conchittas Bar" on 2026-08-07
    And an item "SEXTOU NO CONCHITTAS BAR!" at "Conchittas Bar" on 2026-08-07
    And both items list the same eleven performers
    When the merge pass runs
    Then the two items are merged

  Scenario: Refuse to merge two rows sharing only one performer
    Given an item "Quarta do Rock" at "Conchittas Bar" on 2026-08-05
    And an item "Samba de Quarta" at "Conchittas Bar" on 2026-08-05
    And both items list only "DJ Fabinho" in common
    When the merge pass runs
    Then the two items are not merged

  Scenario: Refuse to merge on lineup when one side has no lineup
    Given an item "Rodolpho" at "Conchittas Bar" on 2026-08-07 with no lineup
    And an item "Noite do Brega" at "Conchittas Bar" on 2026-08-07 listing three performers
    When the merge pass runs
    Then the two items are not merged by the lineup rule

  Scenario: Treat the same performer written in different case as one name
    Given an item listing "DAYANNE" and "PALAS PINHO"
    And a sibling item at the same venue and night listing "Dayanne" and "Palas Pinho"
    When the merge pass runs
    Then the two items are merged

  Scenario: Treat a performer and a longer name containing it as two names
    Given an item listing "Dayanne" and "Cheila do Pará"
    And a sibling item at the same venue and night listing "Dayanne Henrique" and "Anny Love"
    When the merge pass runs
    Then the two items are not merged by the lineup rule

  # ── a greeting is not the party (§B2 non-event guard) ──────────────────────

  Scenario: Refuse to merge a birthday greeting into the party it congratulates
    Given an item "31 Anos" at "Conchittas Bar" on 2026-08-07 typed as "other"
    And an item "Aniversário do Rodolpho Produções" at "Conchittas Bar" on 2026-08-07 typed as "event"
    When the merge pass runs
    Then the two items are not merged

  Scenario: Refuse to merge across differing post types at one venue on one night
    Given an item "Especial do dia" at "Conchittas Bar" on 2026-08-07 typed as "promotion"
    And an item "Especial do dia no Conchittas" at "Conchittas Bar" on 2026-08-07 typed as "event"
    When the merge pass runs
    Then the two items are not merged

  Scenario: Collapse the whole Rodolpho cluster to a single row
    Given the eight production rows of the Conchittas Rodolpho cluster
    When the merge pass runs
    Then the seven event rows survive as one item titled "Aniversário do Rodolpho Produções"
    And that item carries every source post of the seven
    And "31 Anos" is still a separate item
