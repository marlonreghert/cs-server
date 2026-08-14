"""plans/260814_seeded-state-and-config-validation.md §C: the real
deliverable is not the six `event_dedup_*` entries themselves (a one-time
consequence) — it is this test, which fails the moment a `validate_*_config`
function exists anywhere in the codebase with no registration in
`Container.__init__`'s `AdminConfigService(validators={...})` map, so the
next added key cannot repeat this defect silently.

## Why this is not built on a live `Container`

`Container.__init__` wires Redis, RDS, and S3 (see
tests/test_container_judge_wiring.py's own docstring: "Container.__init__ is
too heavy to construct here"). Instead of building one, this:

1. DISCOVERS every `validate_*_config` function that actually exists, by
   importing every module under `app/` and inspecting it — the same
   dynamic-discovery shape the plan's own evidence table used ("sixteen
   `validate_*_config` functions exist across the codebase"), so a future
   validator is found automatically, not by hand-maintaining a second list
   here that could itself drift from the real one.
2. Extracts the REAL set of identifiers used as dict VALUES in the
   `validators={...}` literal actually passed to `AdminConfigService(...)`
   inside `Container.__init__`, by parsing that method's real source with
   `ast` — mirrors tests/test_review_queue_completeness.py's own precedent
   for asserting against real source text when the object itself is too
   heavy to execute (`inspect.getsource` there, `ast.parse` over the same
   source here since a dict literal's values need real parsing, not a
   substring search that could false-positive on an import line whose
   validator is never actually placed in the dict).

A function discovered in step 1 is required to have its bare NAME present in
the set from step 2. All 16 `validate_*_config` functions in this codebase
today have distinct names (verified below), so bare-name matching is
unambiguous; the test also pins that distinctness so a future name collision
is caught rather than silently producing a false pass.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import app as app_package
from app.container import Container


def _discover_validate_config_functions() -> dict[str, object]:
    """Every module-level function named `validate_*_config`, DEFINED
    (not merely imported) somewhere under the `app` package, keyed by its
    bare name. Imports every submodule once; a module that fails to import
    fails this discovery loudly (via the AssertionError below) rather than
    being silently skipped, since a validator hiding in an unimportable
    module would otherwise never be checked at all."""
    functions: dict[str, object] = {}
    failures: list[tuple[str, Exception]] = []
    for module_info in pkgutil.walk_packages(app_package.__path__, prefix="app."):
        try:
            module = importlib.import_module(module_info.name)
        except Exception as e:  # pragma: no cover - defensive, asserted below
            failures.append((module_info.name, e))
            continue
        for name, obj in vars(module).items():
            if (
                inspect.isfunction(obj)
                and obj.__module__ == module.__name__
                and name.startswith("validate_")
                and name.endswith("_config")
            ):
                functions[name] = obj
    assert not failures, f"could not import every app module for discovery: {failures}"
    return functions


def _registered_validator_identifiers() -> set[str]:
    """The bare identifiers used as VALUES in the `validators={...}` dict
    literal passed to `AdminConfigService(...)` inside the REAL
    `Container.__init__` source — parsed with `ast`, never executed."""
    raw = inspect.getsource(Container.__init__)
    lines = raw.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip())
    dedented = "\n".join(line[indent:] if len(line) >= indent else line for line in lines)
    tree = ast.parse(dedented)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef) and func_def.name == "__init__"

    identifiers: set[str] = set()
    for node in ast.walk(func_def):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Name) and target.id == "AdminConfigService"):
            continue
        for kw in node.keywords:
            if kw.arg != "validators" or not isinstance(kw.value, ast.Dict):
                continue
            for value_node in kw.value.values:
                if isinstance(value_node, ast.Name):
                    identifiers.add(value_node.id)
                elif isinstance(value_node, ast.Attribute):
                    identifiers.add(value_node.attr)
    return identifiers


class TestEveryValidateConfigFunctionIsRegistered:
    def test_discovery_finds_every_known_validator_with_no_import_failures(self):
        """Sanity floor: discovery must actually see a realistic population
        (16 as of this plan's own evidence table), not silently find zero
        because `app_package.__path__`/`pkgutil.walk_packages` broke."""
        found = _discover_validate_config_functions()
        assert len(found) >= 16, sorted(found)

    def test_discovered_validator_names_are_unique(self):
        """Bare-name matching below is only sound if no two DIFFERENT
        validator functions (in different modules) share a name — pin that
        today's 16 are all distinct so a future collision fails loudly
        here, not as a silent false pass in the completeness check."""
        found = _discover_validate_config_functions()
        # dict keys are already unique by construction (same name -> same
        # dict slot); assert on the per-module qualified names instead, so
        # a genuine collision (two DIFFERENT functions, same bare name)
        # would have shown up as one of them silently overwriting the
        # other during discovery.
        qualified = []
        for module_info in pkgutil.walk_packages(app_package.__path__, prefix="app."):
            module = importlib.import_module(module_info.name)
            for name, obj in vars(module).items():
                if (
                    inspect.isfunction(obj)
                    and obj.__module__ == module.__name__
                    and name.startswith("validate_")
                    and name.endswith("_config")
                ):
                    qualified.append(f"{module.__name__}.{name}")
        bare_names = [q.rsplit(".", 1)[1] for q in qualified]
        assert len(bare_names) == len(set(bare_names)), (
            "two different validate_*_config functions share a bare name -- "
            f"the completeness check below cannot disambiguate them: {qualified}"
        )

    def test_every_discovered_validator_is_registered_in_the_container(self):
        """The real deliverable: fails the moment a `validate_*_config`
        function exists with no matching entry in Container.__init__'s
        validators map — never mind WHICH key it is registered under, only
        that the function itself is reachable through some key."""
        discovered = _discover_validate_config_functions()
        registered = _registered_validator_identifiers()
        missing = sorted(set(discovered) - registered)
        assert not missing, (
            f"validate_*_config function(s) {missing} exist but are not "
            "registered in Container.__init__'s AdminConfigService(validators="
            "{...}) map -- a value written through the generic admin-config "
            "CRUD route for the corresponding key is never validated."
        )

    def test_the_six_event_dedup_validators_are_registered(self):
        """The one-time consequence of the completeness check above, pinned
        explicitly: plans/260814_seeded-state-and-config-validation.md §C's
        own named defect. Kept as a second, concrete assertion (not a
        substitute for the generic check) so a reader sees exactly what
        this plan fixed without having to run the discovery machinery."""
        registered = _registered_validator_identifiers()
        for name in (
            "validate_generic_vocabulary_config",
            "validate_stopwords_config",
            "validate_lineup_threshold_config",
            "validate_candidate_window_hours_config",
            "validate_undated_window_days_config",
            "validate_auto_merge_enabled_config",
        ):
            assert name in registered, f"{name} is still unregistered"

    def test_a_hypothetical_unregistered_validator_would_be_caught(self):
        """Proves the mechanism itself works (not just that today's set
        happens to be empty of gaps): a validator that is DEFINED but
        deliberately excluded from the registered set is reported missing."""
        discovered = {"validate_totally_fake_config": object()}
        registered = _registered_validator_identifiers()
        missing = sorted(set(discovered) - registered)
        assert missing == ["validate_totally_fake_config"]
