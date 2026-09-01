import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


class TestAcmeOverride(unittest.TestCase):
    """build.timeout_minutes: 45 in _base/defaults.pfcfg (reached via
    container-publish.pfcfg -> node-build.pfcfg), overwritten to 90 by
    acme's own pipeline.pfcfg later in the walk. Last-writer-wins by
    position, not by "more specific file wins" or any other rule.
    """

    def test_timeout_minutes_overridden_45_to_90(self):
        merged = walk(str(STARTER / "customers/acme-corp/pipeline.pfcfg"), env={})
        assignment = merged.assignments["build.timeout_minutes"]
        self.assertEqual(assignment.value, "90")
        # and the losing 45 is still visible in the override trail
        self.assertEqual(len(assignment.overridden), 1)

    def test_no_diagnostics(self):
        merged = walk(str(STARTER / "customers/acme-corp/pipeline.pfcfg"), env={})
        self.assertEqual(merged.diagnostics, [])


class TestGlobexIncludeOnceDiamond(unittest.TestCase):
    """globex/pipeline.pfcfg, CI set + PRODUCTION unset:

      @include _base/defaults.pfcfg        (plain)   -> retention_days=14, retry_count=1
      @include environments/ci-shared.pfcfg (plain)   -> @ifdef CI overlays retention_days=7, retry_count=0
      @ifndef PRODUCTION: @include overrides.pfcfg (plain)
        -> overrides.pfcfg's first line: @include_once ../../_base/defaults.pfcfg

    Decision under test: the plain @include of defaults.pfcfg earlier in
    the walk already marked that resolved path as "seen", so the later
    @include_once is skipped — the CI overlay must survive, not get wiped
    back to defaults' 14/1.
    """

    def _walk(self):
        return walk(str(STARTER / "customers/globex/pipeline.pfcfg"), env={"CI": "true"})

    def test_ci_overlay_survives_diamond_reinclude(self):
        merged = self._walk()
        self.assertEqual(merged.assignments["build.retry_count"].value, "0")
        self.assertEqual(merged.assignments["artifacts.retention_days"].value, "7")
        # timeout_minutes isn't touched by ci-shared.pfcfg at all — it comes
        # from overrides.pfcfg (30), which DOES get walked (it's a plain
        # @include of overrides.pfcfg itself; only its *nested*
        # @include_once of defaults.pfcfg is the one that's skipped). If
        # @include_once had incorrectly re-walked defaults.pfcfg, this
        # would still read 30 (overrides walks after defaults either way) —
        # it's retry_count/retention_days above that actually pin the
        # decision, this just confirms overrides.pfcfg itself did load.
        self.assertEqual(merged.assignments["build.timeout_minutes"].value, "30")

    def test_defaults_included_exactly_once(self):
        merged = self._walk()
        defaults_path = str((STARTER / "_base/defaults.pfcfg").resolve())
        occurrences = [p for p in merged.include_order if p == defaults_path]
        self.assertEqual(len(occurrences), 1)

    def test_no_diagnostics(self):
        merged = self._walk()
        self.assertEqual(merged.diagnostics, [])


class TestGlobexProductionPath(unittest.TestCase):
    """globex/pipeline.pfcfg, PRODUCTION set + CI unset:

      @include _base/defaults.pfcfg        (plain, always)
      @include environments/ci-shared.pfcfg (plain, but its @ifdef CI body
        is now inactive -> contributes nothing)
      @ifdef PRODUCTION: @include environments/on-prem.pfcfg   <- this arm
      @ifndef PRODUCTION: @include overrides.pfcfg              <- NOT this one

    Checks last-writer-wins across an include boundary in the other
    direction from the CI case: on-prem.pfcfg sets deploy.target=on-prem
    positionally BEFORE the entry file's own trailing [deploy] section, so
    the entry file's raw, uninterpolated ${GLOBEX_ENV:-development} must
    win over on-prem's plain "on-prem".
    """

    def _walk(self):
        return walk(str(STARTER / "customers/globex/pipeline.pfcfg"), env={"PRODUCTION": "1"})

    def test_on_prem_loaded_overrides_did_not(self):
        merged = self._walk()
        # on-prem loaded:
        self.assertEqual(merged.assignments["deploy.strategy"].value, "manual")
        # overrides.pfcfg did NOT load: timeout_minutes stays defaults' 45,
        # not overrides' 30 (and ci-shared never touches timeout_minutes).
        self.assertEqual(merged.assignments["build.timeout_minutes"].value, "45")

    def test_entry_files_own_deploy_target_wins_over_on_prem(self):
        merged = self._walk()
        self.assertEqual(merged.assignments["deploy.target"].value, "${GLOBEX_ENV:-development}")

    def test_full_deploy_section(self):
        merged = self._walk()
        deploy_keys = {p: a.value for p, a in merged.assignments.items() if p.startswith("deploy.")}
        print("\nglobex PRODUCTION=1 deploy.* :", deploy_keys)
        self.assertEqual(
            deploy_keys,
            {
                "deploy.strategy": "manual",
                "deploy.requires_approval": "true",
                "deploy.target": "${GLOBEX_ENV:-development}",
            },
        )


class TestIncludeCycle(unittest.TestCase):
    """a.pfcfg @includes b.pfcfg @includes a.pfcfg — a genuine cycle, not
    present in starter/configs (added as a solution/tests fixture). Must
    be reported as a distinct include_cycle diagnostic, not looped
    forever or raised as a fatal error, and must not prevent the rest of
    each file from merging.
    """

    def test_cycle_reported_not_looped(self):
        merged = walk(str(FIXTURES / "include_cycle/a.pfcfg"), env={})

        self.assertEqual(len(merged.diagnostics), 1)
        diag = merged.diagnostics[0]
        self.assertEqual(diag.kind, "include_cycle")
        self.assertIn("a.pfcfg", diag.reason)
        self.assertIn("b.pfcfg", diag.reason)

        # both files still contributed their own (non-cyclic) content
        self.assertEqual(merged.assignments["a.value"].value, "from-a")
        self.assertEqual(merged.assignments["b.value"].value, "from-b")


if __name__ == "__main__":
    unittest.main()
