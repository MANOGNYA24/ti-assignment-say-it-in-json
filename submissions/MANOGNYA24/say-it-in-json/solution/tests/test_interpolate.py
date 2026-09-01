import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pfcfg.interpolate import MAX_DEPTH, resolve_all  # noqa: E402
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


class TestCascadeDepth(unittest.TestCase):
    """edge-cases/interpolation-cascade.pfcfg:

      alpha = ${CASCADE_ALPHA:-unset}
      beta  = prefix-$(cascade.alpha)-suffix
      gamma = ${CASCADE_GAMMA:-$(cascade.beta)}
      delta = ${CASCADE_DELTA:-$(cascade.gamma)-final}
      epsilon = local-$(cascade.delta)             (or ci-$(cascade.delta) under @ifdef CI)

    A legitimate 5-node / 4-hop chain, well under the depth-10 cap. Also
    exercises ${...} whose default branch itself contains $(...) (gamma,
    delta) — the nesting that rules out a flat "resolve env refs, then
    resolve cross-refs" two-pass design.
    """

    def _resolve(self, env):
        merged = walk(str(STARTER / "edge-cases/interpolation-cascade.pfcfg"), env)
        return resolve_all(merged, env)

    def test_chain_resolves_with_all_defaults(self):
        resolved = self._resolve(env={})
        self.assertEqual(resolved.values["cascade.alpha"], "unset")
        self.assertEqual(resolved.values["cascade.beta"], "prefix-unset-suffix")
        self.assertEqual(resolved.values["cascade.gamma"], "prefix-unset-suffix")
        self.assertEqual(resolved.values["cascade.delta"], "prefix-unset-suffix-final")
        self.assertEqual(resolved.values["cascade.epsilon"], "local-prefix-unset-suffix-final")
        # the alpha..epsilon chain resolves clean — the only errors in this
        # file come from the unrelated [cascade.loop] cycle (TestCascadeLoop
        # covers that), not from this chain.
        chain_paths = {"cascade.alpha", "cascade.beta", "cascade.gamma", "cascade.delta", "cascade.epsilon"}
        self.assertEqual([d for d in resolved.errors if d.path in chain_paths], [])

    def test_chain_with_env_overrides_partway_through(self):
        # CASCADE_GAMMA set: gamma's ${...:-$(cascade.beta)} default branch
        # never evaluates, so cascade.beta is computed but NOT threaded
        # into gamma/delta/epsilon — env wins over the cross-ref default,
        # same rule as any other ${VAR:-default}.
        resolved = self._resolve(env={"CASCADE_ALPHA": "A", "CASCADE_GAMMA": "G"})
        self.assertEqual(resolved.values["cascade.alpha"], "A")
        self.assertEqual(resolved.values["cascade.beta"], "prefix-A-suffix")
        self.assertEqual(resolved.values["cascade.gamma"], "G")
        self.assertEqual(resolved.values["cascade.delta"], "G-final")
        self.assertEqual(resolved.values["cascade.epsilon"], "local-G-final")

    def test_ci_conditional_overrides_epsilon_prefix(self):
        # epsilon is reassigned under @ifdef CI in the *merged* map already
        # (walker's job) — interpolate.py just resolves whichever raw value
        # won. This pins that the two phases compose correctly end-to-end.
        resolved = self._resolve(env={"CI": "true"})
        self.assertEqual(resolved.values["cascade.epsilon"], "ci-prefix-unset-suffix-final")


class TestCascadeLoop(unittest.TestCase):
    """[cascade.loop] a = $(cascade.loop.b); b = $(cascade.loop.a) — a
    genuine mutual cycle. Must be reported as a "cycle" diagnostic (not
    "max_depth", not a generic error, not an infinite loop), and neither
    a nor b should appear in the resolved values.
    """

    def _resolve(self, env):
        merged = walk(str(STARTER / "edge-cases/interpolation-cascade.pfcfg"), env)
        return resolve_all(merged, env)

    def test_loop_reported_as_cycle_not_max_depth(self):
        resolved = self._resolve(env={})

        self.assertNotIn("cascade.loop.a", resolved.values)
        self.assertNotIn("cascade.loop.b", resolved.values)

        by_path = {d.path: d for d in resolved.errors}
        self.assertIn("cascade.loop.a", by_path)
        self.assertIn("cascade.loop.b", by_path)
        self.assertEqual(by_path["cascade.loop.a"].kind, "cycle")
        self.assertEqual(by_path["cascade.loop.b"].kind, "cycle")

    def test_rest_of_config_still_resolves(self):
        # the cycle is isolated to cascade.loop.* — it must not poison
        # unrelated keys elsewhere in the same config.
        resolved = self._resolve(env={})
        self.assertEqual(resolved.values["cascade.alpha"], "unset")
        self.assertIn("meta.format_version", resolved.values)


class TestMaxDepthDistinctFromCycle(unittest.TestCase):
    """tests/fixtures/deep_chain/chain.pfcfg: k0 -> k1 -> ... -> k10, an
    11-node chain with NO cycle. Resolving k0 requires a stack of depth 10
    before reaching k10, tripping the cap there. Every node from k0..k10
    genuinely depends (directly or transitively) on k10, so none of them
    have a value — same propagation behavior as TestCascadeLoop, where
    both `a` and `b` fail, not just whichever is detected first. All 11
    inherit kind="max_depth" (never "cycle" — there isn't one here); only
    k10's own diagnostic is the root cause, the rest chain back to it.
    """

    def test_depth_cap_fires_and_propagates_kind_not_cycle(self):
        merged = walk(str(FIXTURES / "deep_chain/chain.pfcfg"), env={})
        resolved = resolve_all(merged, env={})

        expected_failed = {f"chain.k{i}" for i in range(11)}
        self.assertEqual(set(resolved.values.keys()) & expected_failed, set())

        by_path = {d.path: d for d in resolved.errors}
        self.assertEqual(set(by_path.keys()), expected_failed)
        self.assertTrue(all(d.kind == "max_depth" for d in by_path.values()))
        # nothing here is misreported as a cycle — there isn't one
        self.assertNotIn("cycle", [d.kind for d in resolved.errors])

        # k10 is the actual trigger: its reason has no "depends on" prefix
        root = by_path["chain.k10"]
        self.assertIn(f"max depth of {MAX_DEPTH}", root.reason)
        self.assertNotIn("depends on", root.reason)

        # everything upstream of it chains back to that root cause
        self.assertIn("depends on chain.k10", by_path["chain.k9"].reason)


if __name__ == "__main__":
    unittest.main()
