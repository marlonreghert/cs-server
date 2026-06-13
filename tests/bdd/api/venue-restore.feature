@wip
Feature: Restore (reactivate) eligibility-deprecated venues
  An operator must be able to reactivate a venue the eligibility sweep
  soft-deleted, so a false-positive block is recoverable without a raw database
  edit. Reactivation is restricted to venues deprecated by the eligibility filter
  — a real-world closure flagged by Google must not be silently un-closed. The
  endpoint reports the venue's live eligibility verdict so the operator knows
  whether the next sweep would re-deprecate it, and both soft-delete and
  reactivation are recorded in the audit history. The venue reappears in the
  serving projection on the next projector cycle.

  Background:
    Given the admin reactivate endpoint is available

  Scenario: Reactivate a venue the eligibility sweep deprecated
    Given a venue deprecated with source "eligibility_filter"
    When the operator reactivates the venue
    Then the response status is "reactivated"
    And the venue lifecycle is active
    And the deprecated reason, source, and timestamp are cleared
    And an audit history entry with operation "reactivate" is recorded for the venue

  Scenario: Warn when the restored venue still matches a block rule
    Given a venue deprecated with source "eligibility_filter"
    And the venue's Google type still matches an active block rule
    When the operator reactivates the venue
    Then the response status is "reactivated"
    And the eligibility verdict reports it would be re-swept with the predicted reason

  Scenario: Confirm a restored venue that no longer matches any block rule is stable
    Given a venue deprecated with source "eligibility_filter"
    And no active block rule matches the venue
    When the operator reactivates the venue
    Then the eligibility verdict reports it would not be re-swept

  Scenario: Refuse to reactivate a venue Google flagged permanently closed
    Given a venue deprecated with a source other than "eligibility_filter"
    When the operator reactivates the venue
    Then the request is refused as a conflict
    And the venue remains deprecated

  Scenario: Reactivating an already-active venue is an idempotent no-op
    Given an active venue
    When the operator reactivates the venue
    Then the response status is "already_active"
    And no audit history entry is recorded for the venue

  Scenario: Reactivating an unknown venue is not found
    Given no venue exists for the requested id
    When the operator reactivates the venue
    Then the request is reported as not found

  Scenario: Soft-deleting a venue records an audit history entry
    Given an active venue
    When the eligibility sweep soft-deletes the venue
    Then an audit history entry with operation "soft_delete" is recorded for the venue

  Scenario: A reactivated venue is eligible for the serving projection again
    Given a venue deprecated with source "eligibility_filter"
    When the operator reactivates the venue
    Then the venue appears in the active venue ids
    And the venue is absent from the deprecated venue ids
