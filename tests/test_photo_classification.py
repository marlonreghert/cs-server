"""Unit tests for the per-photo classifier: vocabulary, confidence, degrading.

The BDD feature covers what an operator sees — a photo filed under a category, a
manifest carrying its attributes. These cover the parts that are cheap to get
wrong and expensive to notice: a label outside the vocabulary, a confidence
exactly on the threshold, an attribute that arrived without one, and a prompt
that has quietly fallen behind the schema it is supposed to describe.

The rule under most of it: **an attribute is written only when the model was
confident about that one attribute**, and everything else becomes
`not_classified` — a stored answer, not an absent key.
"""
import asyncio
import unittest

from app.api.openai_photo_classifier_client import _batched, _prompt
from app.models.photo_taxonomy import (
    CATEGORY_CROWD,
    CATEGORY_EXTERIOR,
    CATEGORY_FLYER,
    CATEGORY_FOOD_DRINKS,
    CATEGORY_INTERIOR,
    CATEGORY_MENU,
    CATEGORY_OTHER,
    NOT_CLASSIFIED,
    PEOPLE_ATTRIBUTES,
    PHOTO_ATTRIBUTES,
    NOT_APPLICABLE,
    PHOTO_CATEGORIES,
    TIME_OF_DAY,
    attributes_for,
    validate_attributes,
    validate_authorship_guess,
    validate_people,
    validate_quality,
)
from app.services.photo_classification_service import PhotoClassificationService

THRESHOLD = 0.8
SURE = 0.95
UNSURE = 0.4


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _sure(value):
    return {"value": value, "confidence": SURE}


def _wrap(block):
    return {k: v if isinstance(v, dict) else _sure(v) for k, v in block.items()}


class FakeClient:
    """Returns programmed verdicts, or raises, index-aligned like the real one."""

    def __init__(self, verdicts=None, fail=False):
        self._verdicts = verdicts or []
        self.fail = fail
        self.batches = []
        self.attribute_requests = []

    async def classify_photos(self, photo_urls, *, model=None, batch_size=10,
                              with_attributes=True):
        self.batches.append(list(photo_urls))
        self.attribute_requests.append(with_attributes)
        if self.fail:
            raise RuntimeError("vision model unavailable")
        return list(self._verdicts)[:len(photo_urls)]


def _photos(n=1, **fields):
    return [dict({"url": f"https://cdn.example/p{i}"}, **fields) for i in range(n)]


def _service(client, **over):
    over.setdefault("attribute_confidence_threshold", THRESHOLD)
    return PhotoClassificationService(client=client, **over)


def _annotate(verdict, photo_fields=None, **service_kw):
    photos = _photos(1, **(photo_fields or {}))
    stats = _run(_service(FakeClient(verdicts=[verdict]), **service_kw).annotate(photos))
    return photos[0], stats


class TestVocabulary(unittest.TestCase):
    """Every listed value is accepted; anything else becomes not_classified."""

    def test_every_allowed_value_of_every_category_validates(self):
        for category in PHOTO_CATEGORIES:
            for spec in attributes_for(category):
                for value in spec.values:
                    out = validate_attributes(
                        category, {spec.name: _sure(value)}, THRESHOLD)
                    self.assertEqual(out[spec.name], value,
                                     f"{category}.{spec.name} rejected {value!r}")

    def test_not_classified_is_itself_a_valid_answer(self):
        out = validate_attributes(
            CATEGORY_MENU, {"legible": _sure(NOT_CLASSIFIED)}, THRESHOLD)
        self.assertEqual(out["legible"], NOT_CLASSIFIED)

    def test_an_invented_label_becomes_not_classified_and_the_rest_survives(self):
        out = validate_attributes(CATEGORY_MENU, _wrap({
            "legible": "definitely_not_a_value",
            "has_prices": "yes",
        }), THRESHOLD)
        self.assertEqual(out["legible"], NOT_CLASSIFIED)
        self.assertEqual(out["has_prices"], "yes")

    def test_an_attribute_from_another_category_is_ignored(self):
        # `legible` belongs to menu; an interior verdict must not carry it.
        out = validate_attributes(
            CATEGORY_INTERIOR, _wrap({"legible": "yes", "space_type": "bar"}), THRESHOLD)
        self.assertNotIn("legible", out)
        self.assertEqual(out["space_type"], "bar")

    def test_an_unknown_category_yields_nothing(self):
        self.assertEqual(
            validate_attributes("not_a_category", _wrap({"legible": "yes"}), THRESHOLD),
            {})

    def test_quality_and_authorship_guess_reject_anything_unlisted(self):
        self.assertEqual(validate_quality(_sure("good"), THRESHOLD), "good")
        self.assertEqual(validate_quality(_sure("beautiful"), THRESHOLD), NOT_CLASSIFIED)
        self.assertEqual(
            validate_authorship_guess(_sure("by_owner"), THRESHOLD), "by_owner")
        self.assertIsNone(validate_authorship_guess(_sure("unknown"), THRESHOLD))


class TestFlyerCategory(unittest.TestCase):
    """Instagram archives are the first source to hand this classifier a
    poster rather than a photograph — without its own category every flyer
    fell into `other`, indistinguishable from a logo or a document."""

    def test_flyer_is_a_known_category(self):
        self.assertIn(CATEGORY_FLYER, PHOTO_CATEGORIES)

    def test_flyer_has_its_own_shallow_attributes(self):
        names = {spec.name for spec in attributes_for(CATEGORY_FLYER)}
        self.assertEqual(names, {"announces_event", "names_time"})

    def test_flyer_attribute_values_validate(self):
        out = validate_attributes(
            CATEGORY_FLYER,
            _wrap({"announces_event": "yes", "names_time": "no"}),
            THRESHOLD,
        )
        self.assertEqual(out, {"announces_event": "yes", "names_time": "no"})

    def test_adding_flyer_does_not_disturb_the_existing_categories(self):
        # Every OTHER category's schema must be exactly what it was before —
        # a new tuple entry must not perturb dict iteration order elsewhere.
        for category in (CATEGORY_MENU, CATEGORY_FOOD_DRINKS, CATEGORY_INTERIOR,
                          CATEGORY_EXTERIOR, CATEGORY_CROWD, CATEGORY_OTHER):
            self.assertTrue(attributes_for(category), category)


class TestEveryFieldIsAnswered(unittest.TestCase):
    """A known unknown is stored; a silent gap is not."""

    def test_an_unmentioned_attribute_is_recorded_as_not_classified(self):
        out = validate_attributes(CATEGORY_INTERIOR, _wrap({"space_type": "bar"}),
                                  THRESHOLD)
        for spec in attributes_for(CATEGORY_INTERIOR):
            self.assertIn(spec.name, out)
        self.assertEqual(out["lighting"], NOT_CLASSIFIED)

    def test_an_empty_verdict_still_yields_the_whole_schema(self):
        out = validate_attributes(CATEGORY_MENU, {}, THRESHOLD)
        self.assertEqual(set(out), {s.name for s in attributes_for(CATEGORY_MENU)})
        self.assertEqual(set(out.values()), {NOT_CLASSIFIED})

    def test_not_applicable_is_a_distinct_answer_from_not_classified(self):
        # A dessert photo has no drink in it. Recording that as "could not tell"
        # would blame the model for a fact about the photograph — and it is why
        # drink_type read 4/13 on a real run while being almost entirely right.
        out = validate_attributes(CATEGORY_FOOD_DRINKS, _wrap({
            "subject": "food",
            "food_type": "dessert",
            "drink_type": NOT_APPLICABLE,
        }), THRESHOLD)
        self.assertEqual(out["drink_type"], NOT_APPLICABLE)
        self.assertNotEqual(out["drink_type"], NOT_CLASSIFIED)

    def test_every_attribute_can_say_either(self):
        for category in PHOTO_CATEGORIES:
            for spec in attributes_for(category):
                self.assertIn(NOT_APPLICABLE, spec.allowed())
                self.assertIn(NOT_CLASSIFIED, spec.allowed())

    def test_not_applicable_still_has_to_clear_the_confidence_bar(self):
        # It is an assertion about the photo, not a shrug, so it is held to the
        # same standard as any other answer.
        out = validate_attributes(
            CATEGORY_FOOD_DRINKS,
            {"drink_type": {"value": NOT_APPLICABLE, "confidence": UNSURE}},
            THRESHOLD)
        self.assertEqual(out["drink_type"], NOT_CLASSIFIED)

    def test_time_of_day_is_asked_only_where_a_photo_can_answer_it(self):
        # It was asked of every photo and answered on 5 of 20, because a menu
        # close-up and a plated dish show nothing of the outside world.
        can_see_outside = {CATEGORY_INTERIOR, CATEGORY_EXTERIOR, CATEGORY_CROWD}
        for category in PHOTO_CATEGORIES:
            names = {spec.name for spec in attributes_for(category)}
            self.assertEqual(
                TIME_OF_DAY.name in names, category in can_see_outside,
                f"{category} has the wrong answer on time_of_day")


class TestPerAttributeConfidence(unittest.TestCase):
    """The bar is per attribute — one unsure field must not sink its neighbours."""

    def test_above_the_threshold_is_kept(self):
        out = validate_attributes(
            CATEGORY_INTERIOR,
            {"space_type": {"value": "bar", "confidence": 0.81}}, THRESHOLD)
        self.assertEqual(out["space_type"], "bar")

    def test_exactly_on_the_threshold_is_kept(self):
        out = validate_attributes(
            CATEGORY_INTERIOR,
            {"space_type": {"value": "bar", "confidence": 0.8}}, THRESHOLD)
        self.assertEqual(out["space_type"], "bar")

    def test_below_the_threshold_becomes_not_classified(self):
        out = validate_attributes(
            CATEGORY_INTERIOR,
            {"space_type": {"value": "bar", "confidence": 0.79}}, THRESHOLD)
        self.assertEqual(out["space_type"], NOT_CLASSIFIED)

    def test_a_confident_field_survives_an_unsure_neighbour(self):
        out = validate_attributes(CATEGORY_INTERIOR, {
            "space_type": {"value": "bar", "confidence": SURE},
            "lighting": {"value": "dim", "confidence": UNSURE},
        }, THRESHOLD)
        self.assertEqual(out["space_type"], "bar")
        self.assertEqual(out["lighting"], NOT_CLASSIFIED)

    def test_a_value_with_no_confidence_is_refused(self):
        # Nothing to check it against, so it cannot have cleared the bar.
        out = validate_attributes(
            CATEGORY_INTERIOR, {"space_type": {"value": "bar"}}, THRESHOLD)
        self.assertEqual(out["space_type"], NOT_CLASSIFIED)

    def test_a_bare_value_without_the_wrapper_is_refused(self):
        out = validate_attributes(CATEGORY_INTERIOR, {"space_type": "bar"}, THRESHOLD)
        self.assertEqual(out["space_type"], NOT_CLASSIFIED)

    def test_an_unparseable_confidence_is_refused(self):
        out = validate_attributes(
            CATEGORY_INTERIOR,
            {"space_type": {"value": "bar", "confidence": "very"}}, THRESHOLD)
        self.assertEqual(out["space_type"], NOT_CLASSIFIED)

    def test_a_list_answer_is_refused(self):
        # A list means the model answered a different question than the one
        # asked; picking one of its answers would invent a fact.
        out = validate_attributes(
            CATEGORY_INTERIOR,
            {"space_type": {"value": ["bar", "dining"], "confidence": SURE}}, THRESHOLD)
        self.assertEqual(out["space_type"], NOT_CLASSIFIED)

    def test_the_threshold_is_configurable(self):
        raw = {"space_type": {"value": "bar", "confidence": 0.5}}
        self.assertEqual(
            validate_attributes(CATEGORY_INTERIOR, raw, 0.4)["space_type"], "bar")
        self.assertEqual(
            validate_attributes(CATEGORY_INTERIOR, raw, 0.9)["space_type"],
            NOT_CLASSIFIED)


class TestPeopleBlock(unittest.TestCase):
    def test_the_people_block_validates_independently_of_category(self):
        people = validate_people(_wrap({"has_kids": "yes", "crowd_level": "packed"}),
                                 THRESHOLD)
        self.assertEqual(people["has_kids"], "yes")
        self.assertEqual(people["crowd_level"], "packed")

    def test_no_people_reported_means_no_block_at_all(self):
        # Different from a block whose fields could not be read.
        self.assertEqual(validate_people(None, THRESHOLD), {})
        self.assertEqual(validate_people({}, THRESHOLD), {})

    def test_a_block_the_model_could_not_fill_is_still_a_block(self):
        people = validate_people({"has_kids": {"value": "yes", "confidence": UNSURE}},
                                 THRESHOLD)
        self.assertEqual(people["has_kids"], NOT_CLASSIFIED)

    def test_crowd_attributes_are_exactly_the_people_block(self):
        self.assertEqual(PHOTO_ATTRIBUTES[CATEGORY_CROWD], PEOPLE_ATTRIBUTES)
        self.assertEqual(attributes_for(CATEGORY_CROWD), PEOPLE_ATTRIBUTES)

    def test_nothing_in_the_vocabulary_profiles_individuals(self):
        # A guardrail, not a formality: these must not reappear by accident.
        banned = {"race", "ethnicity", "gender", "skin_tone", "attractiveness",
                  "cleanliness", "hygiene", "age", "dress_scene"}
        names = {s.name for specs in PHOTO_ATTRIBUTES.values() for s in specs}
        self.assertEqual(names & banned, set())


class TestCategoryConfidence(unittest.TestCase):
    """The category has its own, lower bar — a misfiled photo is recoverable."""

    def _category_for(self, confidence, threshold=0.6):
        photo, _ = _annotate({"category": "interior", "confidence": confidence},
                             confidence_threshold=threshold)
        return photo["category"]

    def test_above_the_threshold_keeps_the_verdict(self):
        self.assertEqual(self._category_for(0.61), "interior")

    def test_exactly_on_the_threshold_keeps_the_verdict(self):
        self.assertEqual(self._category_for(0.6), "interior")

    def test_below_the_threshold_files_as_other(self):
        self.assertEqual(self._category_for(0.59), CATEGORY_OTHER)

    def test_an_unparseable_confidence_files_as_other(self):
        self.assertEqual(self._category_for("very high"), CATEGORY_OTHER)

    def test_an_unknown_category_files_as_other(self):
        photo, _ = _annotate({"category": "cardapio", "confidence": 0.99})
        self.assertEqual(photo["category"], CATEGORY_OTHER)

    def test_an_other_photo_records_why(self):
        photo, _ = _annotate({"category": "other", "confidence": 0.9,
                              "attributes": _wrap({"other_kind": "event_flyer"})})
        self.assertEqual(photo["attributes"]["other_kind"], "event_flyer")


class TestAuthorship(unittest.TestCase):
    """The provider's fact and the model's guess never merge."""

    def _for(self, authorship):
        photo, _ = _annotate(
            {"category": "interior", "confidence": 0.9,
             "likely_authorship": _sure("by_owner")},
            photo_fields={"authorship": authorship},
        )
        return photo

    def test_a_guess_fills_in_only_where_the_provider_was_silent(self):
        for silent in ("unknown", "", None):
            self.assertEqual(self._for(silent).get("likely_authorship"), "by_owner")

    def test_a_provider_answer_is_never_overwritten_or_guessed_over(self):
        photo = self._for("by_visitor")
        self.assertEqual(photo["authorship"], "by_visitor")
        self.assertNotIn("likely_authorship", photo)

    def test_an_unsure_guess_is_dropped_rather_than_stored(self):
        # It came back `by_visitor` for 20 photos out of 20 on a real run. A
        # field with one possible answer is a constant, not a signal, so a weak
        # guess must leave no trace at all.
        photo, _ = _annotate(
            {"category": "interior", "confidence": 0.9,
             "likely_authorship": {"value": "by_visitor", "confidence": UNSURE}},
            photo_fields={"authorship": "unknown"},
        )
        self.assertNotIn("likely_authorship", photo)

    def test_a_guess_with_no_confidence_is_dropped(self):
        photo, _ = _annotate(
            {"category": "interior", "confidence": 0.9,
             "likely_authorship": "by_visitor"},
            photo_fields={"authorship": "unknown"},
        )
        self.assertNotIn("likely_authorship", photo)

    def test_classification_never_writes_authorship(self):
        photo, _ = _annotate(
            {"category": "menu", "confidence": 0.9, "authorship": "by_owner"},
            photo_fields={"authorship": "by_visitor"},
        )
        self.assertEqual(photo["authorship"], "by_visitor")


class TestDegrading(unittest.TestCase):
    """A photo already paid for is never lost to a classifier problem."""

    def test_a_failure_keeps_the_source_category(self):
        photos = _photos(2, category="menu")
        stats = _run(_service(FakeClient(fail=True)).annotate(photos))
        self.assertEqual([p["category"] for p in photos], ["menu", "menu"])
        self.assertEqual(stats["classified"], 0)

    def test_a_missing_verdict_keeps_the_source_category_and_is_not_other(self):
        # A short response is padded; the padded photos must not be downgraded.
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9}])
        photos = _photos(3, category="all")
        _run(_service(client).annotate(photos))
        self.assertEqual([p["category"] for p in photos], ["interior", "all", "all"])

    def test_a_verdict_with_no_attributes_still_records_the_schema(self):
        photo, stats = _annotate({"category": "interior", "confidence": 0.9})
        self.assertEqual(photo["category"], "interior")
        self.assertEqual(set(photo["attributes"].values()), {NOT_CLASSIFIED})
        self.assertEqual(stats["attributed"], 0)

    def test_the_source_category_is_recorded_before_it_is_replaced(self):
        photo, _ = _annotate({"category": "interior", "confidence": 0.9},
                             photo_fields={"category": "by_owner"})
        self.assertEqual(photo["source_category"], "by_owner")
        self.assertEqual(photo["category"], "interior")


class TestOnePass(unittest.TestCase):
    def test_a_venue_classifies_in_one_call_not_one_per_photo(self):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9}] * 6)
        _run(_service(client).annotate(_photos(6)))
        self.assertEqual(len(client.batches), 1)

    def test_the_category_and_its_attributes_come_from_the_same_call(self):
        photo, _ = _annotate({"category": "interior", "confidence": 0.9,
                              "attributes": _wrap({"space_type": "bar"})})
        self.assertEqual(photo["category"], "interior")
        self.assertEqual(photo["attributes"]["space_type"], "bar")

    def test_attributes_can_be_switched_off(self):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9,
                                       "attributes": _wrap({"space_type": "bar"})}])
        photos = _photos(1)
        _run(_service(client).annotate(photos, derive_attributes=False))
        self.assertEqual(photos[0]["category"], "interior")
        self.assertNotIn("attributes", photos[0])
        self.assertEqual(client.attribute_requests, [False])

    def test_the_output_budget_grows_with_the_batch(self):
        # A flat 2048 truncated a batch of 20 mid-string: the JSON would not
        # parse, the whole batch fell back to no verdict, and the run reported
        # success having classified nothing while paying for every image.
        from app.api.openai_photo_classifier_client import (
            MIN_OUTPUT_TOKENS, OUTPUT_TOKENS_PER_PHOTO, _output_budget,
        )
        self.assertEqual(_output_budget(20), 20 * OUTPUT_TOKENS_PER_PHOTO)
        self.assertGreater(_output_budget(20), _output_budget(10))
        # Small batches still get room for the JSON scaffolding.
        self.assertEqual(_output_budget(1), MIN_OUTPUT_TOKENS)
        self.assertEqual(_output_budget(0), MIN_OUTPUT_TOKENS)

    def test_batches_split_by_size_with_a_remainder(self):
        self.assertEqual(_batched(list(range(5)), 2), [[0, 1], [2, 3], [4]])
        self.assertEqual(_batched([], 10), [])
        self.assertEqual(_batched([1, 2], 0), [[1], [2]])  # never a zero-size loop

    def test_no_photos_costs_nothing(self):
        client = FakeClient()
        stats = _run(_service(client).annotate([]))
        self.assertEqual(stats["cost_usd"], 0.0)
        self.assertEqual(client.batches, [])

    def test_cost_is_one_charge_per_photo_now_that_there_is_one_pass(self):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9,
                                       "attributes": _wrap({"space_type": "bar"})}] * 3)
        stats = _run(_service(client, cost_per_photo_usd=0.001).annotate(_photos(3)))
        self.assertEqual(stats["classified"], 3)
        self.assertEqual(stats["cost_usd"], 0.003)


class TestPricingFromRealTokens(unittest.TestCase):
    """A run is priced from what the API says it consumed, not from a guess."""

    class _Metered(FakeClient):
        def __init__(self, verdicts=None, tokens=None, **kw):
            super().__init__(verdicts=verdicts, **kw)
            self._tokens = tokens or {"input": 0, "output": 0}
            self.takes = 0

        def take_tokens(self):
            self.takes += 1
            used, self._tokens = self._tokens, {"input": 0, "output": 0}
            return used

    def test_cost_comes_from_the_reported_token_counts(self):
        client = self._Metered(
            verdicts=[{"category": "interior", "confidence": 0.9}] * 2,
            tokens={"input": 2000, "output": 500},
        )
        stats = _run(_service(
            client, cost_per_1k_input_usd=0.001, cost_per_1k_output_usd=0.01,
        ).annotate(_photos(2)))
        # 2000/1000*0.001 + 500/1000*0.01 = 0.002 + 0.005
        self.assertEqual(stats["cost_usd"], 0.007)
        self.assertEqual(stats["input_tokens"], 2000)
        self.assertEqual(stats["output_tokens"], 500)
        self.assertFalse(stats["cost_is_estimated"])

    def test_input_and_output_are_priced_separately(self):
        # Output is several times dearer, so a schema change and a batch-size
        # change move the bill for different reasons and by different amounts.
        def cost(tokens):
            client = self._Metered(
                verdicts=[{"category": "interior", "confidence": 0.9}],
                tokens=tokens)
            return _run(_service(
                client, cost_per_1k_input_usd=0.001, cost_per_1k_output_usd=0.01,
            ).annotate(_photos(1)))["cost_usd"]

        self.assertEqual(cost({"input": 1000, "output": 0}), 0.001)
        self.assertEqual(cost({"input": 0, "output": 1000}), 0.01)

    def test_a_client_that_cannot_report_tokens_falls_back_and_says_so(self):
        # The plain FakeClient has no take_tokens, like an older client would.
        stats = _run(_service(
            FakeClient(verdicts=[{"category": "interior", "confidence": 0.9}] * 4),
            cost_per_photo_usd=0.001,
        ).annotate(_photos(4)))
        self.assertEqual(stats["cost_usd"], 0.004)
        self.assertTrue(stats["cost_is_estimated"])

    def test_a_failed_call_reporting_no_tokens_falls_back(self):
        client = self._Metered(tokens={"input": 0, "output": 0}, fail=True)
        stats = _run(_service(client, cost_per_photo_usd=0.001).annotate(_photos(3)))
        self.assertTrue(stats["cost_is_estimated"])
        self.assertEqual(stats["cost_usd"], 0.003)

    def test_tokens_are_read_and_reset_so_venues_do_not_accumulate(self):
        # A cumulative counter would make every venue after the first look
        # progressively more expensive than it was.
        client = self._Metered(
            verdicts=[{"category": "interior", "confidence": 0.9}],
            tokens={"input": 1000, "output": 0})
        service = _service(client, cost_per_1k_input_usd=0.001)
        first = _run(service.annotate(_photos(1)))
        second = _run(service.annotate(_photos(1)))
        self.assertEqual(first["input_tokens"], 1000)
        self.assertEqual(second["input_tokens"], 0)


class TestReDerivingAnArchivedRun(unittest.TestCase):
    def test_attributes_are_written_back_onto_the_entries(self):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9,
                                       "attributes": _wrap({"has_screens": "yes"})}])
        entries = [{"key": "media/interior/p0.jpg", "category": "interior"}]
        _run(_service(client).derive_for_archived(entries, ["https://signed/p0"]))
        self.assertEqual(entries[0]["attributes"]["has_screens"], "yes")

    def test_the_stored_category_is_never_replaced(self):
        # The category is in the S3 key of an object that already exists, so a
        # manifest that took a new one would disagree with its own key.
        client = FakeClient(verdicts=[{"category": "crowd", "confidence": 0.99,
                                       "attributes": _wrap({"crowd_level": "packed"})}])
        entries = [{"key": "media/interior/p0.jpg", "category": "interior"}]
        _run(_service(client).derive_for_archived(entries, ["https://signed/p0"]))
        self.assertEqual(entries[0]["category"], "interior")

    def test_attributes_are_validated_against_the_stored_category(self):
        # `crowd_level` is not an interior attribute, so it must not land.
        client = FakeClient(verdicts=[{"category": "crowd", "confidence": 0.99,
                                       "attributes": _wrap({"crowd_level": "packed"})}])
        entries = [{"key": "media/interior/p0.jpg", "category": "interior"}]
        _run(_service(client).derive_for_archived(entries, ["https://signed/p0"]))
        self.assertNotIn("crowd_level", entries[0]["attributes"])

    def test_a_known_authorship_is_carried_through_untouched(self):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9,
                                       "likely_authorship": "by_owner"}])
        entries = [{"key": "media/interior/p0.jpg", "category": "interior",
                    "authorship": "by_visitor"}]
        _run(_service(client).derive_for_archived(entries, ["https://signed/p0"]))
        self.assertEqual(entries[0]["authorship"], "by_visitor")
        self.assertNotIn("likely_authorship", entries[0])


class TestPromptStaysInSyncWithTheSchema(unittest.TestCase):
    """A stale prompt asks for labels that no longer validate — silently."""

    def test_the_prompt_names_every_category(self):
        prompt = _prompt()
        for category in PHOTO_CATEGORIES:
            self.assertIn(category, prompt)

    def test_the_prompt_names_every_field_and_every_value(self):
        prompt = _prompt()
        for category in PHOTO_CATEGORIES:
            for spec in attributes_for(category):
                self.assertIn(spec.name, prompt, f"{category}.{spec.name} missing")
                for value in spec.allowed():
                    self.assertIn(value, prompt,
                                  f"{category}.{spec.name} value {value!r} missing")

    def test_the_prompt_asks_for_the_people_block(self):
        prompt = _prompt()
        for spec in PEOPLE_ATTRIBUTES:
            self.assertIn(spec.name, prompt)

    def test_the_prompt_asks_for_a_confidence_per_attribute(self):
        self.assertIn("per attribute", _prompt())
        self.assertIn(NOT_CLASSIFIED, _prompt())

    def test_the_prompt_distinguishes_the_two_ways_of_not_answering(self):
        prompt = _prompt()
        self.assertIn(NOT_APPLICABLE, prompt)
        self.assertIn("DOES NOT ARISE", prompt)
        self.assertIn("CANNOT TELL", prompt)

    def test_the_prompt_gives_concrete_cues_for_both_authorship_reads(self):
        # A vaguer version returned by_visitor 20 times out of 20.
        prompt = _prompt()
        for cue in ("by_owner", "by_visitor", "commissioned", "handheld"):
            self.assertIn(cue, prompt)

    def test_the_prompt_requires_other_kind_when_the_category_is_other(self):
        self.assertIn("other_kind is REQUIRED", _prompt())

    def test_the_cheap_prompt_omits_the_attribute_schema(self):
        cheap = _prompt(with_attributes=False)
        self.assertIn("interior", cheap)          # still categorizes
        self.assertNotIn("space_type", cheap)     # but asks for nothing else


if __name__ == "__main__":
    unittest.main()
