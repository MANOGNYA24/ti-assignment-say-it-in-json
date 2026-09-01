"""CLI for the .pfcfg -> JSON converter. No conversion or validation logic
of its own - argument handling and file I/O only; see convert.py and
schema_check.py.

    python3 -m pfcfg.cli convert <entry.pfcfg> [-o out.json] [--schema PATH]
    python3 -m pfcfg.cli convert-all [configs_dir] [-o out_dir] [--schema PATH]
                                      [--entries REL_PATH [REL_PATH ...]]

convert-all defaults to the five entry points format-reference.md lists for
verification; pass --entries to convert a different set (paths relative to
configs_dir).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

from .convert import convert_entry
from .schema_check import validate_bundle

_DEFAULT_SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema", "pfcfg.schema.json")

DEFAULT_ENTRIES = [
    "customers/acme-corp/pipeline.pfcfg",
    "customers/globex/pipeline.pfcfg",
    "customers/initech/pipeline.pfcfg",
    "edge-cases/interpolation-cascade.pfcfg",
    "edge-cases/conditional-includes.pfcfg",
]


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


def main(argv: List[str] = None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m pfcfg.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_convert = sub.add_parser("convert", help="convert one entry config")
    p_convert.add_argument("entry", help="path to a .pfcfg entry config")
    p_convert.add_argument("-o", "--output", help="write JSON here instead of stdout")
    p_convert.add_argument("--schema", default=_DEFAULT_SCHEMA, help="path to pfcfg.schema.json")
    p_convert.set_defaults(func=_cmd_convert)

    p_all = sub.add_parser("convert-all", help="convert a set of entry configs")
    p_all.add_argument("configs_dir", nargs="?", default="starter/configs", help="root of the config tree")
    p_all.add_argument("-o", "--output", default="out", help="output directory (tree mirrors configs_dir)")
    p_all.add_argument("--schema", default=_DEFAULT_SCHEMA, help="path to pfcfg.schema.json")
    p_all.add_argument("--entries", nargs="+", help="override the default 5 entry points (relative to configs_dir)")
    p_all.set_defaults(func=_cmd_convert_all)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
