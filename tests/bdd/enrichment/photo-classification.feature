Feature: Classify archived photos into our own categories and attributes
  As the venue platform operator
  I must label every archived photo with our six categories and the per-category
  attributes our vibe modes actually consume, using our own vision model,
  so that the archive answers who is in the room, whether there are children,
  and whether a menu is even readable — none of which a photo scraper sells us,
  and none of which is worth a second provider subscription.

  Background:
    Given the media archive is enabled with a configured bucket
    And the photo classifier is available

  # 1 — The six categories
  Scenario: File a photo under the category the classifier chose
    Given a fetched photo the classifier categorizes as "interior"
    When the photo archive job runs for that venue
    Then the photo is filed under the "interior" category
    And the manifest entry records the category "interior"

  Scenario: Replace the authorship placeholder category with a real one
    Given the source files photos under an authorship placeholder category
    And a fetched photo the classifier categorizes as "menu"
    When the photo archive job runs for that venue
    Then the photo is filed under the "menu" category
    And no photo is filed under an authorship placeholder category

  Scenario: Categorize every photo of a venue in one batched request
    Given a venue with 12 fetched photos
    When the photo archive job runs for that venue
    Then the classifier receives the photos in batches
    And the classifier is not called once per photo

  # 2 — Exterior is decided by the sky
  Scenario: Treat an open-air area as exterior
    Given a fetched photo showing open air overhead
    When the photo archive job runs for that venue
    Then the photo is filed under the "exterior" category

  Scenario: Treat a covered space as interior
    Given a fetched photo showing a roof overhead
    When the photo archive job runs for that venue
    Then the photo is filed under the "interior" category

  Scenario: Distinguish a facade from an open-air area within exterior
    Given a fetched photo of the venue facade from the street
    When the photo archive job runs for that venue
    Then the photo is filed under the "exterior" category
    And the manifest entry records the exterior kind "fachada"

  Scenario: Record whether an open-air area has cover from rain
    Given a fetched photo of a partially covered terrace
    When the photo archive job runs for that venue
    Then the manifest entry records the exterior kind "area_externa"
    And the manifest entry records that the area is partially covered

  # 3 — People are read wherever they appear
  Scenario: Keep a room shot as interior when people are incidental
    Given a fetched photo of a room with a few people at its edges
    When the photo archive job runs for that venue
    Then the photo is filed under the "interior" category

  Scenario: Extract the people attributes from a photo filed as interior
    Given a fetched photo of a room with a few people at its edges
    When the photo archive job runs for that venue
    Then the manifest entry carries a people block
    And the people block records a crowd level

  Scenario: Omit the people block when nobody is visible
    Given a fetched photo of an empty room
    When the photo archive job runs for that venue
    Then the manifest entry carries no people block

  Scenario: Record children in the crowd
    Given a fetched photo of a crowd that includes children
    When the photo archive job runs for that venue
    Then the people block records that children are present

  Scenario: Record the crowd scene and the dress read separately
    Given a fetched photo of a crowd at a rock night
    When the photo archive job runs for that venue
    Then the people block records a dress code from the venue taxonomy
    And the people block records the dress scene "rock_metal"

  # 4 — Per-category attributes
  Scenario: Record whether a menu can be read at all
    Given a fetched photo of a blurred menu
    When the photo archive job runs for that venue
    Then the manifest entry records that the menu is not legible

  Scenario: Record what a menu covers and which side it shows
    Given a fetched photo of the back page of a drinks menu
    When the photo archive job runs for that venue
    Then the manifest entry records the menu page side "verso"
    And the manifest entry records the menu content scope "so_bebida"

  Scenario: Record whether a menu carries prices
    Given a fetched photo of a menu without prices
    When the photo archive job runs for that venue
    Then the manifest entry records that the menu has no prices

  Scenario: Skip an illegible menu when extracting menu text
    Given an archived menu photo marked as not legible
    And an archived menu photo marked as legible
    When menu extraction selects photos to read
    Then only the legible menu photo is selected

  Scenario: Record whether a dish is meant to be shared
    Given a fetched photo of a sharing platter
    When the photo archive job runs for that venue
    Then the manifest entry records the portion size "para_dividir"

  Scenario: Record the music setup visible in a room
    Given a fetched photo of a room with a DJ booth
    When the photo archive job runs for that venue
    Then the manifest entry records a music format from the venue taxonomy

  Scenario: Record whether a room has a big screen
    Given a fetched photo of a room with a projector screen
    When the photo archive job runs for that venue
    Then the manifest entry records the screens value "telao"

  Scenario: Give each category only the attributes of its own schema
    Given a fetched photo the classifier categorizes as "menu"
    When the photo archive job runs for that venue
    Then the manifest entry carries no interior attributes

  Scenario: Record why a photo could not be categorized
    Given a fetched photo of an event flyer
    When the photo archive job runs for that venue
    Then the photo is filed under the "other" category
    And the manifest entry records the other kind "flyer_evento"

  # 5 — Taxonomy-aligned attributes use the existing vocabulary
  Scenario: Emit taxonomy labels for an attribute the venue taxonomy already owns
    Given a fetched photo of a dimly lit intimate room
    When the photo archive job runs for that venue
    Then the manifest entry records an aesthetic from the venue taxonomy

  Scenario: Drop a label that is not in the venue taxonomy
    Given the classifier returns an aesthetic label that is not in the taxonomy
    When the photo archive job runs for that venue
    Then that aesthetic label is not stored
    And the rest of the classifier verdict is stored

  # 6 — Who took the photo
  Scenario: Leave a known authorship untouched by classification
    Given a fetched photo the provider attributes to the venue owner
    When the photo archive job runs for that venue
    Then the photo is filed under the "menu" category
    And the manifest entry still records the authorship "by_owner"

  Scenario: Guess the author only when the provider did not say
    Given a fetched photo whose provider authorship is unknown
    When the photo archive job runs for that venue
    Then the manifest entry records a likely authorship
    And the manifest entry still records the authorship "unknown"

  Scenario: Never guess over a provider's answer
    Given a fetched photo the provider attributes to a visitor
    When the photo archive job runs for that venue
    Then the manifest entry records no likely authorship

  # 7 — Degrading without losing photos
  Scenario: Archive every photo when the classifier fails
    Given the photo classifier fails for every request
    And a venue with 4 fetched photos
    When the photo archive job runs for that venue
    Then all 4 photos are still archived
    And the photos keep the category the source gave them

  Scenario: Keep a categorized photo when its attributes cannot be derived
    Given the classifier categorizes photos but fails to derive attributes
    When the photo archive job runs for that venue
    Then the photo is filed under its classified category
    And the manifest entry carries no attributes

  Scenario: File a low confidence verdict as other rather than guessing
    Given the classifier returns a verdict below the confidence threshold
    When the photo archive job runs for that venue
    Then the photo is filed under the "other" category

  Scenario: Keep archiving after the classifier fails for one venue
    Given two eligible venues where the classifier fails for the first
    When the photo archive job runs
    Then both venues are archived
    And the second venue's photos carry a classified category

  # 8 — Controlling what it costs
  Scenario: Never classify a source that provides real categories
    Given the source provides its own photo categories
    When the photo archive job runs for that venue
    Then the classifier is not called
    And the photos keep the category the source gave them

  Scenario: Disable classification for a run
    Given classification is disabled for the run
    When the photo archive job runs for that venue
    Then the classifier is not called

  Scenario: Disable attribute derivation while still categorizing
    Given attribute derivation is disabled for the run
    When the photo archive job runs for that venue
    Then the photo is filed under its classified category
    And the manifest entry carries no attributes

  Scenario: Skip attribute derivation for photos filed as other
    Given a fetched photo the classifier categorizes as "other"
    When the photo archive job runs for that venue
    Then no attribute request is made for that photo

  Scenario: Count what classification cost
    Given a venue with 6 fetched photos
    When the photo archive job runs for that venue
    Then the run summary reports the number of photos classified
    And the run summary reports an estimated classification cost

  # 9 — Extending the schema without paying a provider again
  Scenario: Re-derive attributes for an archived run from stored photos
    Given a completed run whose photos are archived
    And the attribute schema gains a field the model can read from them
    When attribute derivation is re-run for that run
    Then the archived photos are read from the bucket
    And no provider request is made
    And the manifest entry records the new attribute
