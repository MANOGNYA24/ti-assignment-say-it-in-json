"""CLI for the .pfcfg -> JSON converter, equivalence verifier, and
unmigratable report. No conversion or validation logic of its own -
argument handling and file I/O only; see convert.py, verify.py, and
schema_check.py.

Run from this directory (solution/, so `pfcfg` is importable as -m):

    python3 -m pfcfg.cli convert <entry.pfcfg> [-o out.json] [--schema PATH]
    python3 -m pfcfg.cli convert-all [configs_dir] [-o out_dir] [--schema PATH]
                                      [--entries REL_PATH [REL_PATH ...]]
    python3 -m pfcfg.cli verify [configs_dir] [--entries REL_PATH ...]
                                 [--fixtures-dir DIR] [--schema PATH]
    python3 -m pfcfg.cli report [configs_dir] [--entries REL_PATH ...]
                                 [--fixtures-dir DIR] [--schema PATH]
                                 [-o out/unmigratable.ndjson]

convert-all/verify/report all default to the five entry points
format-reference.md lists; pass --entries to use a different set (paths
relative to configs_dir). configs_dir itself defaults to this repo's own
starter/configs, found by walking up from this file - no argument needed
for a plain checkout, so `python3 -m pfcfg.cli verify` alone is the whole
command.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

from .convert import convert_entry
from .schema_check import validate_bundle
from .verify import DEFAULT_ENTRIES, build_report, load_fixtures, run_matrix

_DEFAULT_SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema", "pfcfg.schema.json")
_DEFAULT_FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def _find_default_configs_dir() -> str:
    """A cwd-independent default for configs_dir: walk up from this file
    (not from wherever the caller happens to be) looking for a starter/
    configs directory. Same fix as tests/test_json_eval.py's own
    _find_starter_configs() - and the same reason: this package lives
    nested under submissions/<user>/say-it-in-json/solution/pfcfg/, so a
    literal "starter/configs" relative to cwd only resolves if the caller
    happens to be sitting in the repo root, which `python3 -m pfcfg.cli`
    can't itself require (the module import needs cwd = this package's
    parent). Falls back to the old cwd-relative literal if genuinely not
    found, so an --configs-dir override or a differently-laid-out checkout
    still behaves exactly as before.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    while True:
        candidate = os.path.join(here, "starter", "configs")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return "starter/configs"
        here = parent


_DEFAULT_CONFIGS_DIR = _find_default_configs_dir()


def _load_schema(schema_path: str) -> dict:
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _convert_and_validate(entry_path: str, schema: dict) -> dict:
    bundle = convert_entry(entry_path)
    errors = validate_bundle(bundle, schema)
    if errors:
        print(f"SCHEMA VALIDATION FAILED for {entry_path}:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        raise SystemExit(1)
    return bundle


def _cmd_convert(args: argparse.Namespace) -> None:
    schema = _load_schema(args.schema)
    bundle = _convert_and_validate(args.entry, schema)
    text = json.dumps(bundle, indent=2, sort_keys=False)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {args.output} (valid against {args.schema})", file=sys.stderr)
    else:
        print(text)


def _cmd_convert_all(args: argparse.Namespace) -> None:
    schema = _load_schema(args.schema)
    entries: List[str] = args.entries if args.entries else DEFAULT_ENTRIES
    for rel in entries:
        entry_path = os.path.join(args.configs_dir, rel)
        bundle = _convert_and_validate(entry_path, schema)
        out_path = os.path.join(args.output, rel[: -len(".pfcfg")] + ".json") if rel.endswith(".pfcfg") else os.path.join(
            args.output, rel + ".json"
        )
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2)
            f.write("\n")
        print(f"{rel} -> {out_path} (valid)", file=sys.stderr)


def _cmd_verify(args: argparse.Namespace) -> None:
    schema = _load_schema(args.schema)
    entries: List[str] = args.entries if args.entries else DEFAULT_ENTRIES
    fixtures = load_fixtures(args.fixtures_dir)
    cells = run_matrix(args.configs_dir, entries, fixtures, schema)

    fixture_names = [name for name, _ in fixtures]
    entry_width = max(len(rel) for rel in entries)
    col_width = max(max(len(n) for n in fixture_names), len("PASS")) + 2

    header = " " * (entry_width + 2) + "".join(name.ljust(col_width) for name in fixture_names)
    print(header)
    by_key = {(c.entry_rel, c.fixture_name): c for c in cells}
    for rel in entries:
        row = rel.ljust(entry_width + 2)
        for name in fixture_names:
            row += by_key[(rel, name)].status.ljust(col_width)
        print(row)

    failing = [c for c in cells if c.status != "PASS"]
    print()
    print(f"{len(cells) - len(failing)}/{len(cells)} passed")

    for c in failing:
        print()
        print(f"{c.status} {c.entry_rel} x {c.fixture_name}")
        if c.status == "ERROR":
            print(f"  {c.error}")
            continue
        if c.values_diff is not None:
            print("  values:")
            print(f"    only_in_reference: {c.values_diff['only_in_reference']}")
            print(f"    only_in_json_eval: {c.values_diff['only_in_json_eval']}")
            print(f"    differing: {c.values_diff['differing']}")
        if c.errors_diff is not None:
            print("  errors:")
            print(f"    only_in_reference: {c.errors_diff['only_in_reference']}")
            print(f"    only_in_json_eval: {c.errors_diff['only_in_json_eval']}")

    if failing:
        raise SystemExit(1)


def _cmd_report(args: argparse.Namespace) -> None:
    schema = _load_schema(args.schema)
    entries: List[str] = args.entries if args.entries else DEFAULT_ENTRIES
    fixtures = load_fixtures(args.fixtures_dir)
    cells = run_matrix(args.configs_dir, entries, fixtures, schema)

    records, skipped = build_report(cells, relative_to=os.getcwd())

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=False))
            f.write("\n")

    by_kind: dict = {}
    for record in records:
        by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
    kind_summary = ", ".join(f"{count} {kind}" for kind, count in sorted(by_kind.items())) or "none"
    print(
        f"wrote {len(records)} rows to {args.output} ({kind_summary}); "
        f"{skipped} cell(s) skipped (not PASS in verify)",
        file=sys.stderr,
    )


def main(argv: List[str] = None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m pfcfg.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_convert = sub.add_parser("convert", help="convert one entry config")
    p_convert.add_argument("entry", help="path to a .pfcfg entry config")
    p_convert.add_argument("-o", "--output", help="write JSON here instead of stdout")
    p_convert.add_argument("--schema", default=_DEFAULT_SCHEMA, help="path to pfcfg.schema.json")
    p_convert.set_defaults(func=_cmd_convert)

    p_all = sub.add_parser("convert-all", help="convert a set of entry configs")
    p_all.add_argument("configs_dir", nargs="?", default=_DEFAULT_CONFIGS_DIR, help="root of the config tree")
    p_all.add_argument("-o", "--output", default="out", help="output directory (tree mirrors configs_dir)")
    p_all.add_argument("--schema", default=_DEFAULT_SCHEMA, help="path to pfcfg.schema.json")
    p_all.add_argument("--entries", nargs="+", help="override the default 5 entry points (relative to configs_dir)")
    p_all.set_defaults(func=_cmd_convert_all)

    p_verify = sub.add_parser("verify", help="diff reference evaluator vs. JSON evaluator across configs x fixtures")
    p_verify.add_argument("configs_dir", nargs="?", default=_DEFAULT_CONFIGS_DIR, help="root of the config tree")
    p_verify.add_argument("--entries", nargs="+", help="override the default 5 entry points (relative to configs_dir)")
    p_verify.add_argument("--fixtures-dir", default=_DEFAULT_FIXTURES, help="directory of *.json environment fixtures")
    p_verify.add_argument("--schema", default=_DEFAULT_SCHEMA, help="path to pfcfg.schema.json")
    p_verify.set_defaults(func=_cmd_verify)

    p_report = sub.add_parser("report", help="write the unmigratable-diagnostics NDJSON report")
    p_report.add_argument("configs_dir", nargs="?", default=_DEFAULT_CONFIGS_DIR, help="root of the config tree")
    p_report.add_argument("--entries", nargs="+", help="override the default 5 entry points (relative to configs_dir)")
    p_report.add_argument("--fixtures-dir", default=_DEFAULT_FIXTURES, help="directory of *.json environment fixtures")
    p_report.add_argument("--schema", default=_DEFAULT_SCHEMA, help="path to pfcfg.schema.json")
    p_report.add_argument("-o", "--output", default="out/unmigratable.ndjson", help="write NDJSON here")
    p_report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
