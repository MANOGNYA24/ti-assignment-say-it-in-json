import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pfcfg.convert import convert_entry  # noqa: E402
from pfcfg.interpolate import resolve_all  # noqa: E402
from pfcfg.json_eval import evaluate_bundle  # noqa: E402
from pfcfg.walker import walk  # noqa: E402


def _find_starter_configs() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "starter" / "configs"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("could not locate starter/configs directory from " + str(here))


STARTER = _find_starter_configs()
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The five entry points format-reference.md / cli.py's DEFAULT_ENTRIES list.
ENTRIES = [
    "customers/acme-corp/pipeline.pfcfg",
    "customers/globex/pipeline.pfcfg",
    "customers/initech/pipeline.pfcfg",
    "edge-cases/interpolation-cascade.pfcfg",
    "edge-cases/conditional-includes.pfcfg",
]

# Per assignment.md: at least one CI-like fixture (CI set, non-empty) and
# one non-CI fixture (CI unset or empty).
ENV_FIXTURES = [
    ("non-ci", {}),
    ("ci", {"CI": "true"}),
]


def _reference_result(entry_path: str, env: dict):
    """Reference evaluator = walk() + resolve_all(), combined the same way
    json_eval.evaluate_bundle combines them: walk diagnostics first, then
    interpolation errors, each reshaped to {path, kind, reason}. The oracle
    itself keeps these two lists separate (MergedConfig.diagnostics vs.
    ResolvedConfig.errors) and never merges them - see design-note.md's
    "implementation trap" note - so the merge has to happen at the call
    site on both the reference side and the json_eval side identically for
    a fair comparison.
    """
    merged = walk(entry_path, env)
    resolved = resolve_all(merged, env)
    errors = [{"path": d.path, "kind": d.kind, "reason": d.reason} for d in merged.diagnostics]
    errors += [{"path": d.path, "kind": d.kind, "reason": d.reason} for d in resolved.errors]
    return resolved.values, errors


def _errorset(errors):
    return sorted((e["path"], e["kind"], e["reason"]) for e in errors)


class TestSectionPersistsAcrossInclude(unittest.TestCase):
    """current_section is one pointer for the whole walk, never reset on
    entering/leaving an included file. Unexercised anywhere in
    starter/configs, so agreement between the two implementations there
    proves nothing about this rule specifically — they could both be wrong
    the same way. This fixture (fixtures/section_persists_across_include)
    isolates exactly that: included.pfcfg ends on a bare [x] header with
    no key under it, and entry.pfcfg's next node after the include returns
    is a bare assignment with no section header of its own.

    Correct behavior: that bare assignment lands under [x] (included.pfcfg's
    last section), giving "x.after" = "also-lands-under-x". A per-file
    section-pointer scoping bug would instead either raise (no section ever
    set "in this file") or wrongly fall back to [entry].
    """

    ENTRY = FIXTURES / "section_persists_across_include" / "entry.pfcfg"

    def _reference_values(self, env):
        merged = walk(str(self.ENTRY), env)
        resolved = resolve_all(merged, env)
        return resolved.values

    def _json_eval_values(self, env):
        bundle = convert_entry(str(self.ENTRY))
        result = evaluate_bundle(bundle, env)
        return result["values"]

    def test_bare_assignment_lands_under_included_files_last_section(self):
        env = {}
        reference = self._reference_values(env)
        under_test = self._json_eval_values(env)

        expected = {
            "shared.seeded": "value",
            "x.after": "also-lands-under-x",
        }
        self.assertEqual(reference, expected)
        self.assertEqual(under_test, expected)
        self.assertEqual(reference, under_test)


class TestFullMapEquivalence(unittest.TestCase):
    """The actual equivalence check Phase 4 exists for: the reference
    evaluator (walk + resolve_all) and the independent evaluate_bundle
    must agree on the ENTIRE resolved values map and the ENTIRE errors
    list, not just a handful of keys someone happened to print. Agreement
    on a chosen subset proves nothing about the keys nobody checked -
    this asserts full dict equality, so any single divergent key anywhere
    in any of the five entry configs, under either environment fixture,
    fails loudly with a diff.
    """

    def _check(self, rel_path: str, env_name: str, env: dict):
        entry = str(STARTER / rel_path)
        bundle = convert_entry(entry)
        ref_values, ref_errors = _reference_result(entry, env)
        result = evaluate_bundle(bundle, env)

        with self.subTest(entry=rel_path, env=env_name, aspect="values"):
            if ref_values != result["values"]:
                ref_keys, mine_keys = set(ref_values), set(result["values"])
                diff = {
                    "only_in_reference": sorted(ref_keys - mine_keys),
                    "only_in_json_eval": sorted(mine_keys - ref_keys),
                    "differing": {
                        k: (ref_values[k], result["values"][k])
                        for k in ref_keys & mine_keys
                        if ref_values[k] != result["values"][k]
                    },
                }
                self.fail(f"{rel_path} under {env_name}: values diverged: {diff}")

        with self.subTest(entry=rel_path, env=env_name, aspect="errors"):
            self.assertEqual(
                _errorset(ref_errors),
                _errorset(result["errors"]),
                f"{rel_path} under {env_name}: error sets diverged",
            )

    def test_all_five_entries_under_both_fixtures(self):
        checked = 0
        for rel_path in ENTRIES:
            for env_name, env in ENV_FIXTURES:
                self._check(rel_path, env_name, env)
                checked += 1
        self.assertEqual(checked, len(ENTRIES) * len(ENV_FIXTURES))


if __name__ == "__main__":
    unittest.main()
