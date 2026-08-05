@wip
Feature: Instagram promoter events
  As the operator of the event pipeline
  I want events from accounts that belong to no venue mapped to known venues
  So that a city's promoted nightlife is captured, and an event is never
  attributed to the wrong bar on a weak guess

  Background:
    Given the catalog holds venues with confirmed Instagram handles
    And the event extractor is available

  Scenario: Register and crawl a promoter account within its post bound
    Given the operator registers the promoter account "festadorecife"
    And that account is active
    And the run caps posts per account at 5
    When the promoter crawl runs
    Then 5 posts of that account are crawled
    And its posts are archived under the source folder "instagram_posts"
    And its posts are not archived under any venue id

  Scenario: Propose a candidate account without crawling it
    Given confirmed events whose captions mention "@coletivonoturno" 4 times
    And the mention threshold is 3
    When promoter discovery runs
    Then "@coletivonoturno" is registered with the status "candidate"
    And its discovery source is "mention"
    And no post of that account is crawled

  Scenario: Crawl a candidate only once an operator activates it
    Given a promoter account with the status "candidate"
    When the promoter crawl runs
    Then no post of that account is crawled
    When the operator sets that account active
    And the promoter crawl runs
    Then its posts are crawled

  Scenario: Link automatically from a mention of a known venue handle
    Given a promoter post whose caption mentions a known venue's handle
    When the promoter crawl runs
    Then the event is linked to that venue
    And the link method is "handle_mention"
    And the link resolution is "auto"

  Scenario: Prefer a handle mention over a higher-scoring name match
    Given a promoter post that mentions a known venue's handle
    And its location text matches a different venue by name with a higher score
    When the promoter crawl runs
    Then the event is linked to the venue whose handle was mentioned

  Scenario: Link from an Instagram location tag when no handle is mentioned
    Given a promoter post with no handle mention
    And the post carries a location tag matching a known venue
    When the promoter crawl runs
    Then the event is linked to that venue
    And the link method is "location_tag"

  Scenario: Link from a name match when neither handle nor location tag exists
    Given a promoter post whose location text names a known venue
    And the post carries no handle mention and no location tag
    When the promoter crawl runs
    Then the event is linked to that venue
    And the link method is "name_match"

  Scenario: Queue for review when two candidates score within the margin
    Given a promoter post whose location text matches two venues with scores
      0.91 and 0.89
    And the auto-link margin is 0.05
    When the promoter crawl runs
    Then the event has the status "pending_review"
    And the event has no venue id
    And both venues are recorded as ranked candidates

  Scenario: Leave an event unresolved when every candidate is below the floor
    Given a promoter post whose best candidate scores below the confidence floor
    When the promoter crawl runs
    Then the event location resolution is "unresolved"
    And the event has no venue id
    And the event is distinguishable from one queued for review

  Scenario: Present ranked candidates with their evidence in the review queue
    Given an event queued for review with three candidate venues
    When the review queue is requested
    Then the three candidates are returned in rank order
    And each candidate carries its score, its method and its evidence

  Scenario: Link manually from the review queue
    Given an event queued for review with three candidate venues
    When the operator links it to the second candidate
    Then the event is linked to that venue
    And the link resolution is "manual"
    And the operator who linked it is recorded

  Scenario: Preserve a manual link across a later crawl
    Given an event an operator linked manually
    When the promoter crawl runs again over its post
    Then the event stays linked to the venue the operator chose

  Scenario: Unlink a wrong automatic link
    Given an event linked automatically to the wrong venue
    When the operator unlinks it
    Then the event has no venue id
    And the event location resolution is "unresolved"

  Scenario: Skip a private or missing account and finish the run
    Given three active promoter accounts and the second one is unavailable
    When the promoter crawl runs
    Then the unavailable account is marked and skipped
    And the third account is still crawled

  Scenario: Never link an event to the promoter's own account
    Given a promoter post whose caption mentions the promoter's own handle
    When the promoter crawl runs
    Then that mention is not used to resolve the venue

  Scenario: Report the selection without writing under dry run
    Given two active promoter accounts
    When the promoter crawl runs as a dry run
    Then the accounts and post counts it would crawl are reported
    And no post is crawled
    And no event is written
