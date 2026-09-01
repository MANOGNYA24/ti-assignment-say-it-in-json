"""Phase 5: the equivalence verifier + unmigratable report, sharing one
matrix-running core so the two never wire up "run reference vs. run
json_eval" slightly differently (see design-note.md section 5's own
implementation-trap example: resolve_all() alone drops include_cycle).

What a PASS matrix cell proves and doesn't: pfcfg/walker.py + interpolate.py
(the oracle's resolve algorithm) and pfcfg/json_eval.py (a from-scratch,
independently-written reimplementation of that same algorithm over JSON
nodes - it does not import walker/interpolate) agree on the full values map
and full error set for that (entry, fixture) pair. That IS a real
cross-check of the walk/interpolation algorithm. It is NOT a cross-check of
pfcfg's file/value grammar: convert.py and the oracle both parse source text
through the same pfcfg/parser.py and the same interpolate.py._ValueParser, so
a systematic misreading baked into either of those (e.g. a wrong comment-
strip rule, a wrong ":-" vs ":+" split) would corrupt what both "independent"
evaluators see identically, before either algorithm ever runs - agreement
there is agreement with itself, not a second opinion.

The unmigratable report is built from the oracle's own Diagnostic objects,
and ONLY for cells this module's matrix has independently confirmed PASS -
never from a FAIL/ERROR cell, since on a cell where the two evaluators
disagree (or one of them crashed), neither side's diagnostics have been
proven trustworthy yet, and guessing which one is right would be exactly
the "reasons I trust" failure Jordan is asking not to repeat.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .convert import convert_entry
from .interpolate import resolve_all
from .json_eval import evaluate_bundle
from .model import Diagnostic
from .schema_check import validate_bundle
from .walker import walk

DEFAULT_ENTRIES = [
    "customers/acme-corp/pipeline.pfcfg",
    "customers/globex/pipeline.pfcfg",
    "customers/initech/pipeline.pfcfg",
    "edge-cases/interpolation-cascade.pfcfg",
    "edge-cases/conditional-includes.pfcfg",
]


# ---------------------------------------------------------------------------
# Fixtures: committed JSON files, not hardcoded. One file per environment,
# {"VAR": "value", ...}; the filename (minus .json) is the fixture's display
# name in the table and the report.
# ---------------------------------------------------------------------------


def load_fixtures(fixtures_dir: str) -> List[Tuple[str, Dict[str, str]]]:
    if not os.path.isdir(fixtures_dir):
        raise FileNotFoundError(f"fixtures directory not found: {fixtures_dir}")
    names = sorted(f for f in os.listdir(fixtures_dir) if f.endswith(".json"))
    if not names:
        raise ValueError(f"no *.json fixture files found in {fixtures_dir}")
    fixtures: List[Tuple[str, Dict[str, str]]] = []
    for name in names:
        path = os.path.join(fixtures_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            try:
                env = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"fixture {path!r} is not valid JSON: {e}") from e
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise ValueError(f"fixture {path!r} must be a flat JSON object of string values")
        fixtures.append((name[: -len(".json")], env))
    return fixtures


# ---------------------------------------------------------------------------
# One matrix cell: (entry config, fixture) run through both evaluators.
# ---------------------------------------------------------------------------


@dataclass
class CellResult:
    entry_rel: str
    fixture_name: str
    status: str  # "PASS" | "FAIL" | "ERROR"
    error: Optional[str] = None  # ERROR only: exception message
    values_diff: Optional[dict] = None  # FAIL only
    errors_diff: Optional[dict] = None  # FAIL only
    # PASS/FAIL only (whenever the oracle itself ran clean) - raw Diagnostic
    # objects, kept (not yet reshaped to the report's flat record) so the
    # report can pull loc.file/loc.line, which the values/errors diff above
    # deliberately strips for a fair side-by-side comparison.
    ref_diagnostics: List[Diagnostic] = field(default_factory=list)


def _errorset(errors: List[dict]):
    return sorted((e["path"], e["kind"], e["reason"]) for e in errors)


def _diff_values(ref: Dict[str, str], mine: Dict[str, str]) -> Optional[dict]:
    if ref == mine:
        return None
    ref_keys, mine_keys = set(ref), set(mine)
    return {
        "only_in_reference": sorted(ref_keys - mine_keys),
        "only_in_json_eval": sorted(mine_keys - ref_keys),
        "differing": {k: (ref[k], mine[k]) for k in ref_keys & mine_keys if ref[k] != mine[k]},
    }


def _diff_errors(ref: List[dict], mine: List[dict]) -> Optional[dict]:
    ref_set, mine_set = set(_errorset(ref)), set(_errorset(mine))
    if ref_set == mine_set:
        return None
    return {
        "only_in_reference": sorted(ref_set - mine_set),
        "only_in_json_eval": sorted(mine_set - ref_set),
    }


def run_cell(entry_abs: str, entry_rel: str, fixture_name: str, env: Dict[str, str], schema: dict) -> CellResult:
    """Runs one (entry, fixture) pair through conversion, schema validation,
    the reference evaluator, and the JSON evaluator. Any exception anywhere
    in that pipeline - a malformed config, a schema violation, a bug in
    either evaluator - is caught here and turned into an ERROR cell, never
    silently treated as agreement. A crash must never be reportable as PASS.
    """
    try:
        bundle = convert_entry(entry_abs)
        schema_errors = validate_bundle(bundle, schema)
        if schema_errors:
            raise ValueError(f"bundle failed schema validation: {schema_errors}")

        merged = walk(entry_abs, env)
        resolved = resolve_all(merged, env)
        ref_values = resolved.values
        ref_errors = [{"path": d.path, "kind": d.kind, "reason": d.reason} for d in merged.diagnostics]
        ref_errors += [{"path": d.path, "kind": d.kind, "reason": d.reason} for d in resolved.errors]

        json_result = evaluate_bundle(bundle, env)
    except Exception as e:  # noqa: BLE001 - deliberately broad: any failure -> ERROR cell, not a pass
        return CellResult(
            entry_rel=entry_rel,
            fixture_name=fixture_name,
            status="ERROR",
            error=f"{type(e).__name__}: {e}",
        )

    values_diff = _diff_values(ref_values, json_result["values"])
    errors_diff = _diff_errors(ref_errors, json_result["errors"])
    status = "PASS" if values_diff is None and errors_diff is None else "FAIL"
    return CellResult(
        entry_rel=entry_rel,
        fixture_name=fixture_name,
        status=status,
        values_diff=values_diff,
        errors_diff=errors_diff,
        ref_diagnostics=list(merged.diagnostics) + list(resolved.errors),
    )


def run_matrix(
    configs_dir: str,
    entries: List[str],
    fixtures: List[Tuple[str, Dict[str, str]]],
    schema: dict,
) -> List[CellResult]:
    """Full cross product: every entry config against every fixture. A
    fixture that doesn't affect a given config (e.g. globex-production
    against acme, which never branches on PRODUCTION) still runs the same
    pipeline and diffs trivially-equal maps - PASS, not a special case.
    """
    cells: List[CellResult] = []
    for rel in entries:
        entry_abs = os.path.realpath(os.path.join(configs_dir, rel))
        for fixture_name, env in fixtures:
            cells.append(run_cell(entry_abs, rel, fixture_name, env, schema))
    return cells


# ---------------------------------------------------------------------------
# Unmigratable report: one NDJSON record per oracle Diagnostic, but only
# for cells the matrix confirmed PASS.
# ---------------------------------------------------------------------------


def diagnostic_to_record(diag: Diagnostic, entry_rel: str, relative_to: str) -> Dict[str, Any]:
    """Reshapes one oracle Diagnostic into a report row - no `fixture` field
    here; which fixture(s) it was observed under is a property of the
    dedup grouping in build_report, not of a single diagnostic occurrence.
    """
    if diag.kind == "include_cycle":
        # diag.path is the cyclic include's resolved_path (a file), not a
        # dotted section.key - there's no key to split, so section/key are
        # null, and "file" names the file the cycle re-enters rather than
        # the location of the @include statement that detected it. loc.line
        # (the include directive's own line) doesn't pair meaningfully with
        # that repointed file, so line is omitted rather than printed next
        # to the wrong file.
        return {
            "file": os.path.relpath(diag.path, relative_to),
            "section": None,
            "key": None,
            "reason": diag.reason,
            "kind": diag.kind,
            "entry": entry_rel,
        }
    section, _, key = (diag.path or "").rpartition(".")
    return {
        "file": os.path.relpath(diag.loc.file, relative_to),
        "section": section or None,
        "key": key or None,
        "reason": diag.reason,
        "kind": diag.kind,
        "entry": entry_rel,
        "line": diag.loc.line,
    }


def build_report(cells: List[CellResult], relative_to: str) -> Tuple[List[Dict[str, Any]], int]:
    """Returns (records, skipped_cell_count). Records come only from PASS
    cells; FAIL/ERROR cells are counted but contribute no rows, per the
    report/verifier coupling design: don't source "reasons to trust" from a
    cell that isn't proven to agree.

    Dedup: an unmigratable item (same entry/file/section/key/kind/reason -
    everything but which fixture triggered it) is one row with an
    `observed_under` list, not one row per fixture. cascade.loop.a/.b fail
    identically under every fixture in this corpus because the cycle is a
    property of the config, not the environment - a human reviewer needs
    "2 unmigratable keys, both environment-invariant," not "6 rows" that
    are the same 2 facts repeated 3 times. The reason string is *part of*
    the grouping key, not dropped: a key that resolves fine under most
    fixtures but hits a genuinely different failure under one specific
    fixture (e.g. a missing_ref that only exists behind an inactive
    @ifdef in other environments) produces a different reason and so
    stays a distinct row - dedup collapses repetition, it does not hide
    an environment-only failure.
    """
    grouped: "Dict[tuple, Dict[str, Any]]" = {}
    order: List[tuple] = []
    skipped = 0
    for cell in cells:
        if cell.status != "PASS":
            skipped += 1
            continue
        for diag in cell.ref_diagnostics:
            record = diagnostic_to_record(diag, cell.entry_rel, relative_to)
            group_key = (
                record["entry"],
                record["file"],
                record["section"],
                record["key"],
                record["kind"],
                record["reason"],
                record.get("line"),
            )
            if group_key not in grouped:
                grouped[group_key] = dict(record)
                grouped[group_key]["observed_under"] = []
                order.append(group_key)
            grouped[group_key]["observed_under"].append(cell.fixture_name)

    records: List[Dict[str, Any]] = []
    for group_key in order:
        rec = grouped[group_key]
        rec["observed_under"] = sorted(set(rec["observed_under"]))
        records.append(rec)
    return records, skipped
