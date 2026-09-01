"""Converts a .pfcfg entry config into the parse-tree-bundle JSON shape
schema/pfcfg.schema.json defines.

Structure only, per schema/design-note.md: no @ifdef is evaluated, no
${...}/$(...) is resolved, no include is flattened. Reuses parse_file_cached
(parser.py) to get each file's AST and interpolate.py's own _ValueParser to
tokenize each raw value into its Literal/EnvRef/KeyRef segments — this
module has no file-syntax grammar or value grammar of its own; it only
re-shapes what those two already parse.

Environment-independent by construction: nothing here reads os.environ or
takes an env argument. The same entry config must produce byte-identical
JSON regardless of what env vars happen to be set when this runs.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from .interpolate import EnvRef, KeyRef, Literal, Segment, _ValueParser
from .model import Conditional, Include, KeyAssignment, Node, SectionHeader
from .parser import parse_file_cached


def convert_entry(entry_path: str) -> Dict[str, Any]:
    """Convert one entry config to a bundle dict: {"entry", "files"}."""
    entry_abs = os.path.realpath(entry_path)
    files: Dict[str, Any] = {}
    _convert_file(entry_abs, files)
    return {"entry": entry_abs, "files": files}


def _convert_file(path: str, files: Dict[str, Any]) -> None:
    """Populate files[path] with this file's converted node list, recursing
    into every reachable include target (including ones nested inside
    Conditional bodies, since which files are structurally reachable does
    not depend on environment — only whether they're walked at eval time
    does).

    A placeholder is inserted into `files` before recursing into this
    file's own includes, so a self-including (or diamond-re-including)
    file sees itself already present on re-entry and stops instead of
    looping. This is the conversion-time analogue of walker.py's
    include_chain ancestor stack: simpler because bundling only needs
    "have I already produced a node array for this resolved path", not a
    per-environment cycle diagnostic (see design-note.md section 5) - a
    file included twice, cyclically or not, still has exactly one node
    array in `files`, keyed by resolved path.
    """
    if path in files:
        return
    file_node: Dict[str, Any] = {"path": path, "nodes": []}
    files[path] = file_node
    parsed = parse_file_cached(path)
    file_node["nodes"] = _convert_nodes(parsed.nodes, os.path.dirname(path), files)


def _convert_nodes(nodes: List[Node], base_dir: str, files: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for node in nodes:
        if isinstance(node, SectionHeader):
            out.append({"type": "section", "path": node.path, "line": node.loc.line})
        elif isinstance(node, KeyAssignment):
            segments = _ValueParser(node.raw_value, node.loc).parse()
            out.append(
                {
                    "type": "assign",
                    "key": node.key,
                    "value": [_convert_segment(s) for s in segments],
                    "line": node.loc.line,
                }
            )
        elif isinstance(node, Include):
            # Same join-and-normalize walker.py's _handle_include does -
            # purely static (relative to the containing file's directory),
            # never environment-dependent, so it's safe to compute once here.
            resolved = os.path.realpath(os.path.join(base_dir, node.target))
            _convert_file(resolved, files)
            out.append(
                {
                    "type": "include",
                    "target": node.target,
                    "once": node.once,
                    "resolved_path": resolved,
                    "line": node.loc.line,
                }
            )
        elif isinstance(node, Conditional):
            out.append(
                {
                    "type": "conditional",
                    "kind": node.kind,
                    "var": node.var,
                    "body": _convert_nodes(node.body, base_dir, files),
                    "line": node.loc.line,
                }
            )
        else:  # pragma: no cover - exhaustive over model.Node
            raise AssertionError(f"unknown node type: {node!r}")
    return out


def _convert_segment(seg: Segment) -> Dict[str, Any]:
    if isinstance(seg, Literal):
        return {"type": "literal", "text": seg.text}
    if isinstance(seg, EnvRef):
        d: Dict[str, Any] = {"type": "env", "var": seg.var, "mode": seg.mode}
        if seg.expr is not None:
            d["expr"] = [_convert_segment(s) for s in seg.expr]
        return d
    if isinstance(seg, KeyRef):
        return {"type": "key", "path": seg.path}
    raise AssertionError(f"unknown segment type: {seg!r}")  # pragma: no cover - exhaustive over Segment
