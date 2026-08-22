@wip
Feature: Venue visit tracking — batch ingest and system of record
  The device detects that a signed-in user was physically present at a venue and
  for how long, then uploads those visits in batches. cs-server is the system of
  record for that dataset: it pseudonymizes the user, validates every visit, and
  writes one durable row per visit.

  The device may be offline for days, so a batch can carry old visits and the
  same batch can be retried after a transport failure. A retry must never
  duplicate a row, and one poisoned visit must never strand the whole buffered
  batch — a batch that can only ever be rejected as a unit would be retried
  forever and the good visits inside it would never land.

  A user can genuinely visit the same venue twice on the same day, so the
  idempotency key is the device-minted visit id, never the calendar day. This is
  a deliberate departure from the hot-like idempotency rule, where collapsing a
  day's repeats is correct.

  Visits are the most sensitive data this system holds. The raw user id is never
  stored, coordinates are never logged, and account deletion must erase every
  visit row rather than deactivate it.

  Background:
    Given the engagement pseudonymization key is configured
    And the minimum accepted dwell is 60 seconds
    And the maximum accepted dwell is 86400 seconds
    And the maximum accepted clock skew is 300 seconds
    And the maximum accepted backfill window is 30 days
    And the maximum accepted batch size is 200 visits

  Scenario: A batch of visits is persisted
    Given a signed-in user with two buffered visits to different venues
    When the batch is uploaded
    Then the response status must be 200
    And the response must report 2 accepted visits
    And the response must report 0 duplicate visits
    And the response must report 0 rejected visits
    And one venue visit row must exist for each uploaded visit

  Scenario: The raw user id is never stored
    Given a signed-in user with one buffered visit
    When the batch is uploaded
    Then the persisted visit row must carry the HMAC pseudonym of the user id
    And the raw user id must not appear in any persisted column

  Scenario: A retried batch does not duplicate rows
    Given a signed-in user with two buffered visits to different venues
    And the batch has already been uploaded successfully
    When the identical batch is uploaded again
    Then the response must report 0 accepted visits
    And the response must report 2 duplicate visits
    And the total number of persisted visit rows must remain 2

  Scenario: Two visits to the same venue on the same day are both kept
    Given a signed-in user who visited the same venue at midday and again at night on the same Recife day
    And each visit carries its own client visit id
    When the batch is uploaded
    Then the response must report 2 accepted visits
    And two distinct visit rows must exist for that venue and that Recife day

  Scenario: A visit shorter than the minimum dwell is rejected
    Given a signed-in user with one buffered visit lasting 20 seconds
    When the batch is uploaded
    Then the response must report 0 accepted visits
    And the response must report 1 rejected visit
    And no visit row must be persisted
    And the rejection must be counted with reason "dwell_too_short"

  Scenario: A visit longer than the maximum dwell is rejected
    Given a signed-in user with one buffered visit lasting 90000 seconds
    When the batch is uploaded
    Then the response must report 1 rejected visit
    And no visit row must be persisted
    And the rejection must be counted with reason "dwell_too_long"

  Scenario: A visit arriving beyond the clock skew allowance is rejected
    Given a signed-in user with one buffered visit whose arrival is 1 hour in the future
    When the batch is uploaded
    Then the response must report 1 rejected visit
    And no visit row must be persisted
    And the rejection must be counted with reason "future_timestamp"

  Scenario: A visit older than the backfill window is rejected
    Given a signed-in user with one buffered visit whose arrival is 45 days ago
    When the batch is uploaded
    Then the response must report 1 rejected visit
    And no visit row must be persisted
    And the rejection must be counted with reason "too_old"

  Scenario: An unrecognized source is rejected
    Given a signed-in user with one buffered visit whose source is "beacon"
    When the batch is uploaded
    Then the response must report 1 rejected visit
    And no visit row must be persisted
    And the rejection must be counted with reason "bad_source"

  Scenario: A visit missing its client visit id is rejected
    Given a signed-in user with one buffered visit whose client visit id is empty
    When the batch is uploaded
    Then the response must report 1 rejected visit
    And no visit row must be persisted
    And the rejection must be counted with reason "missing_id"

  Scenario: One invalid visit does not reject the valid ones in the same batch
    Given a signed-in user with one valid buffered visit and one visit lasting 5 seconds
    When the batch is uploaded
    Then the response must report 1 accepted visit
    And the response must report 1 rejected visit
    And exactly one visit row must be persisted

  Scenario: The business period is derived from the arrival in America/Recife
    Given a signed-in user with one buffered visit arriving 30 minutes before midnight in Recife
    And the arrival timestamp is expressed in a non-Recife offset
    When the batch is uploaded
    Then the persisted visit row must record the Recife calendar day of the arrival
    And the business period must not be taken from any value supplied by the client

  Scenario: A visit to an unknown venue is stored rather than rejected
    Given a signed-in user with one buffered visit to a venue that is no longer in the database
    When the batch is uploaded
    Then the response must report 1 accepted visit
    And the visit row must be persisted with that venue id
    And the unknown venue counter must be incremented

  Scenario: An oversized batch is refused
    Given a signed-in user with 201 buffered visits
    When the batch is uploaded
    Then the response status must be 422
    And no visit row must be persisted

  Scenario: A storage failure is reported as retryable
    Given a signed-in user with one buffered visit
    And the visit store fails to commit
    When the batch is uploaded
    Then the response status must be 502
    And the response detail must instruct the caller to retry

  Scenario: Account deletion erases visits
    Given a signed-in user with three persisted visits
    When the user's data is erased
    Then no visit row must remain for that user
    And the erasure response must report 3 erased visits

  Scenario: Visit ingestion never logs the raw user id or coordinates
    Given a signed-in user with one buffered visit
    When the batch is uploaded
    Then the emitted logs must contain the pseudonym
    And the emitted logs must not contain the raw user id
    And the emitted logs must not contain any latitude or longitude
