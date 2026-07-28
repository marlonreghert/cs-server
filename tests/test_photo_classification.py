"""Unit tests for the per-photo classifier: vocabulary, cardinality, degrading.

The BDD feature covers what an operator sees — a photo filed under a category,
a manifest carrying its attributes. These cover the parts that are cheap to get
wrong and expensive to notice: a label that is not in the vocabulary, a single
valued field given a list, a confidence exactly on the threshold, and a prompt
that has quietly fallen behind the taxonomy it is supposed to describe.
"""
import asyncio
import unittest

from app.api.openai_photo_classifier_client import (
    _attribute_prompt,
    _batched,
    _category_prompt,
)
from app.models.photo_taxonomy import (
    CATEGORY_CROWD,
    CATEGORY_INTERIOR,
    CATEGORY_MENU,
    CATEGORY_OTHER,
    PEOPLE_ATTRIBUTES,
    PHOTO_ATTRIBUTES,
    PHOTO_CATEGORIES,
    validate_attributes,
    validate_authorship_guess,
    validate_people,
    validate_quality,
)
from app.models.taxonomy import TAXONOMY
from app.services.photo_classification_service import PhotoClassificationService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeClient:
    """Returns programmed verdicts, or raises, index-aligned like the real one."""

    def __init__(self, verdicts=None, attributes=None,
                 fail_classify=False, fail_attributes=False):
        self._verdicts = verdicts or []
        self._attributes = attributes or []
        self.fail_classify = fail_classify
        self.fail_attributes = fail_attributes
        self.classify_batches = []
        self.attribute_batches = []

    async def classify_photos(self, photo_urls, *, model=None, batch_size=10):
        self.classify_batches.append(list(photo_urls))
        if self.fail_classify:
            raise RuntimeError("vision model unavailable")
        return list(self._verdicts)[:len(photo_urls)]

    async def derive_attributes(self, category, photo_urls, *, model=None, batch_size=10):
        self.attribute_batches.append((category, list(photo_urls)))
        if self.fail_attributes:
            raise RuntimeError("vision model unavailable")
        return list(self._attributes)[:len(photo_urls)]


def _photos(n=1, **fields):
    return [dict({"url": f"https://cdn.example/p{i}"}, **fields) for i in range(n)]


def _service(client, **over):
    return PhotoClassificationService(client=client, **over)


class TestVocabulary(unittest.TestCase):
    """Every listed value is accepted; anything else is dropped, not stored."""

    def test_every_allowed_value_of_every_category_validates(self):
        for category, specs in PHOTO_ATTRIBUTES.items():
            for spec in specs:
                if spec.boolean:
                    raw = {spec.name: True}
                    expected = True
                elif spec.many:
                    raw = {spec.name: list(spec.allowed())}
                    expected = list(spec.allowed())
                else:
                    raw = {spec.name: spec.allowed()[0]}
                    expected = spec.allowed()[0]
                out = validate_attributes(category, raw)
                self.assertEqual(out.get(spec.name), expected,
                                 f"{category}.{spec.name} rejected its own vocabulary")

    def test_an_invented_label_is_dropped_and_the_rest_survives(self):
        out = validate_attributes(CATEGORY_MENU, {
            "legible": "definitely_not_a_value",
            "has_prices": True,
        })
        self.assertNotIn("legible", out)
        self.assertTrue(out["has_prices"])

    def test_an_attribute_from_another_category_is_dropped(self):
        # `legible` belongs to menu; an interior verdict must not carry it.
        out = validate_attributes(CATEGORY_INTERIOR, {
            "legible": "sim", "space_type": "salao",
        })
        self.assertEqual(out, {"space_type": "salao"})

    def test_an_unknown_category_yields_nothing(self):
        self.assertEqual(validate_attributes("not_a_category", {"legible": "sim"}), {})

    def test_quality_and_authorship_guess_reject_anything_unlisted(self):
        self.assertEqual(validate_quality("boa"), "boa")
        self.assertIsNone(validate_quality("linda"))
        self.assertEqual(validate_authorship_guess("by_owner"), "by_owner")
        self.assertIsNone(validate_authorship_guess("unknown"))


class TestTaxonomyAlignedFields(unittest.TestCase):
    """[T] fields defer to `taxonomy.py`, so the two vocabularies cannot desync."""

    def test_taxonomy_fields_accept_the_real_taxonomy_labels(self):
        for category, specs in PHOTO_ATTRIBUTES.items():
            for spec in (s for s in specs if s.taxonomy_key):
                self.assertIn(spec.taxonomy_key, TAXONOMY,
                              f"{category}.{spec.name} defers to a key taxonomy.py "
                              f"does not have")
                label = TAXONOMY[spec.taxonomy_key][0]
                value = [label] if spec.many else label
                out = validate_attributes(category, {spec.name: value})
                self.assertEqual(out.get(spec.name), value)

    def test_a_label_outside_the_venue_taxonomy_is_rejected(self):
        out = validate_attributes(CATEGORY_INTERIOR, {"estetica": ["Cyberpunk"]})
        self.assertNotIn("estetica", out)

    def test_a_taxonomy_field_keeps_the_valid_half_of_a_mixed_list(self):
        real = TAXONOMY["estetica"][0]
        out = validate_attributes(CATEGORY_INTERIOR, {"estetica": [real, "Cyberpunk"]})
        self.assertEqual(out["estetica"], [real])


class TestCardinality(unittest.TestCase):
    def test_a_scalar_for_an_array_field_is_wrapped(self):
        # A formatting slip, not a different answer.
        out = validate_attributes(CATEGORY_MENU, {"sections": "petiscos"})
        self.assertEqual(out["sections"], ["petiscos"])

    def test_a_list_for_a_single_valued_field_is_rejected(self):
        # Picking one would invent a fact the model did not assert.
        out = validate_attributes(CATEGORY_MENU, {"legible": ["sim", "nao"]})
        self.assertNotIn("legible", out)

    def test_a_string_boolean_is_coerced_and_anything_else_dropped(self):
        self.assertIs(validate_attributes(CATEGORY_MENU, {"has_prices": "true"})["has_prices"], True)
        self.assertNotIn("has_prices", validate_attributes(CATEGORY_MENU, {"has_prices": "sim"}))


class TestPeopleBlock(unittest.TestCase):
    def test_the_people_block_validates_independently_of_category(self):
        people = validate_people({"has_kids": True, "crowd_level": "cheio"})
        self.assertTrue(people["has_kids"])
        self.assertEqual(people["crowd_level"], "cheio")

    def test_crowd_attributes_are_the_people_block(self):
        self.assertEqual(PHOTO_ATTRIBUTES[CATEGORY_CROWD], PEOPLE_ATTRIBUTES)

    def test_nothing_in_the_vocabulary_profiles_individuals(self):
        # A guardrail, not a formality: race/ethnicity/gender fields must not
        # reappear in this schema by accident later.
        banned = {"race", "ethnicity", "gender", "skin_tone", "attractiveness",
                  "cleanliness", "hygiene"}
        names = {s.name for specs in PHOTO_ATTRIBUTES.values() for s in specs}
        self.assertEqual(names & banned, set())


class TestConfidenceThreshold(unittest.TestCase):
    def _category_for(self, confidence, threshold=0.6):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": confidence}])
        photos = _photos(1)
        _run(_service(client, confidence_threshold=threshold).annotate(
            photos, derive_attributes=False))
        return photos[0]["category"]

    def test_above_the_threshold_keeps_the_verdict(self):
        self.assertEqual(self._category_for(0.61), "interior")

    def test_exactly_on_the_threshold_keeps_the_verdict(self):
        self.assertEqual(self._category_for(0.6), "interior")

    def test_below_the_threshold_files_as_other(self):
        self.assertEqual(self._category_for(0.59), CATEGORY_OTHER)

    def test_an_unparseable_confidence_files_as_other(self):
        self.assertEqual(self._category_for("very high"), CATEGORY_OTHER)

    def test_an_other_verdict_records_why_from_the_first_pass(self):
        # Pass 2 skips `other`, so this is the only chance to record WHY.
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.2,
                                       "other_kind": "flyer_evento"}])
        photos = _photos(1)
        _run(_service(client).annotate(photos, derive_attributes=False))
        self.assertEqual(photos[0]["category"], CATEGORY_OTHER)
        self.assertEqual(photos[0]["attributes"], {"other_kind": "flyer_evento"})

    def test_an_invalid_other_kind_is_dropped_rather_than_stored(self):
        client = FakeClient(verdicts=[{"category": "other", "confidence": 0.9,
                                       "other_kind": "borrada"}])
        photos = _photos(1)
        _run(_service(client).annotate(photos, derive_attributes=False))
        self.assertFalse(photos[0].get("attributes"))


class TestAuthorship(unittest.TestCase):
    """The provider's fact and the model's guess never merge."""

    def _annotate(self, authorship):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9,
                                       "likely_authorship": "by_owner"}])
        photos = _photos(1, authorship=authorship)
        _run(_service(client).annotate(photos, derive_attributes=False))
        return photos[0]

    def test_a_guess_fills_in_only_where_the_provider_was_silent(self):
        for silent in ("unknown", "", None):
            self.assertEqual(self._annotate(silent).get("likely_authorship"), "by_owner")

    def test_a_provider_answer_is_never_overwritten_or_guessed_over(self):
        photo = self._annotate("by_visitor")
        self.assertEqual(photo["authorship"], "by_visitor")
        self.assertNotIn("likely_authorship", photo)

    def test_classification_never_writes_authorship(self):
        client = FakeClient(verdicts=[{"category": "menu", "confidence": 0.9,
                                       "authorship": "by_owner"}])
        photos = _photos(1, authorship="by_visitor")
        _run(_service(client).annotate(photos, derive_attributes=False))
        self.assertEqual(photos[0]["authorship"], "by_visitor")


class TestDegrading(unittest.TestCase):
    """A photo already paid for is never lost to a classifier problem."""

    def test_a_classifier_failure_keeps_the_source_category(self):
        photos = _photos(2, category="menu")
        stats = _run(_service(FakeClient(fail_classify=True)).annotate(photos))
        self.assertEqual([p["category"] for p in photos], ["menu", "menu"])
        self.assertEqual(stats["classified"], 0)

    def test_a_missing_verdict_keeps_the_source_category_and_is_not_other(self):
        # A short response is padded; the padded photos must not be downgraded.
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9}])
        photos = _photos(3, category="all")
        _run(_service(client).annotate(photos, derive_attributes=False))
        self.assertEqual([p["category"] for p in photos], ["interior", "all", "all"])

    def test_an_attribute_failure_leaves_the_photo_categorized(self):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9}],
                            fail_attributes=True)
        photos = _photos(1)
        stats = _run(_service(client).annotate(photos))
        self.assertEqual(photos[0]["category"], "interior")
        self.assertNotIn("attributes", photos[0])
        self.assertEqual(stats["attributed"], 0)

    def test_an_unknown_category_files_as_other(self):
        client = FakeClient(verdicts=[{"category": "cardapio", "confidence": 0.99}])
        photos = _photos(1)
        _run(_service(client).annotate(photos, derive_attributes=False))
        self.assertEqual(photos[0]["category"], CATEGORY_OTHER)

    def test_the_source_category_is_recorded_before_it_is_replaced(self):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9}])
        photos = _photos(1, category="by_owner")
        _run(_service(client).annotate(photos, derive_attributes=False))
        self.assertEqual(photos[0]["source_category"], "by_owner")
        self.assertEqual(photos[0]["category"], "interior")


class TestBatchingAndCost(unittest.TestCase):
    def test_batches_split_by_size_with_a_remainder(self):
        self.assertEqual(_batched(list(range(5)), 2), [[0, 1], [2, 3], [4]])
        self.assertEqual(_batched([], 10), [])
        self.assertEqual(_batched([1, 2], 0), [[1], [2]])  # never a zero-size loop

    def test_a_venue_classifies_in_one_call_not_one_per_photo(self):
        verdicts = [{"category": "interior", "confidence": 0.9}] * 6
        client = FakeClient(verdicts=verdicts)
        _run(_service(client).annotate(_photos(6), derive_attributes=False))
        self.assertEqual(len(client.classify_batches), 1)

    def test_other_photos_never_reach_the_attribute_pass(self):
        client = FakeClient(verdicts=[{"category": "other", "confidence": 0.9}] * 3)
        _run(_service(client).annotate(_photos(3)))
        self.assertEqual(client.attribute_batches, [])

    def test_each_category_gets_its_own_focused_attribute_call(self):
        client = FakeClient(
            verdicts=[{"category": "interior", "confidence": 0.9},
                      {"category": "menu", "confidence": 0.9}],
            attributes=[{"attributes": {}}] * 2,
        )
        _run(_service(client).annotate(_photos(2)))
        self.assertEqual(sorted(c for c, _ in client.attribute_batches),
                         ["interior", "menu"])

    def test_no_photos_costs_nothing(self):
        client = FakeClient()
        stats = _run(_service(client).annotate([]))
        self.assertEqual(stats["cost_usd"], 0.0)
        self.assertEqual(client.classify_batches, [])

    def test_cost_counts_both_passes(self):
        client = FakeClient(verdicts=[{"category": "interior", "confidence": 0.9}],
                            attributes=[{"attributes": {"space_type": "salao"}}])
        stats = _run(_service(client, cost_per_photo_usd=0.001).annotate(_photos(1)))
        self.assertEqual(stats["classified"], 1)
        self.assertEqual(stats["attributed"], 1)
        self.assertEqual(stats["cost_usd"], 0.002)


class TestPromptsStayInSyncWithTheTaxonomy(unittest.TestCase):
    """A stale prompt asks for labels that no longer validate — silently."""

    def test_the_category_prompt_names_every_category(self):
        prompt = _category_prompt()
        for category in PHOTO_CATEGORIES:
            self.assertIn(category, prompt)

    def test_the_category_prompt_asks_for_other_kind(self):
        # It cannot be asked in pass 2, which skips `other` entirely.
        prompt = _category_prompt()
        self.assertIn("other_kind", prompt)
        for value in PHOTO_ATTRIBUTES[CATEGORY_OTHER][0].allowed():
            self.assertIn(value, prompt)

    def test_every_attribute_prompt_names_its_fields_and_values(self):
        for category, specs in PHOTO_ATTRIBUTES.items():
            if category == CATEGORY_OTHER:
                continue  # asked in pass 1
            prompt = _attribute_prompt(category)
            for spec in specs:
                self.assertIn(spec.name, prompt, f"{category}.{spec.name} missing")
                for value in spec.allowed():
                    self.assertIn(value, prompt,
                                  f"{category}.{spec.name} value {value!r} missing")

    def test_non_crowd_prompts_ask_for_the_people_block(self):
        prompt = _attribute_prompt(CATEGORY_INTERIOR)
        self.assertIn("people", prompt)
        self.assertIn("has_kids", prompt)


if __name__ == "__main__":
    unittest.main()
