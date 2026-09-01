"""Validates a converted bundle against schema/pfcfg.schema.json.

The converter must check its own output - this is the "fail loudly if it
doesn't conform" requirement, not an optional nicety. Uses the `jsonschema`
package (full draft 2020-12 validation) when it's installed; otherwise falls
back to a hand-written structural checker that mirrors the same shape by
hand, so validation still runs on a laptop with no extra installs.

Both paths return a list of human-readable error strings; empty = valid.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

try:
    import jsonschema  # type: ignore

    _HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover - exercised whichever is absent
    _HAVE_JSONSCHEMA = False


def validate_bundle(bundle: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    if _HAVE_JSONSCHEMA:
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(bundle), key=lambda e: list(e.absolute_path))
        return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]
    return _StructuralChecker().check_bundle(bundle)


# ---------------------------------------------------------------------------
# Stdlib fallback. Hand-mirrors schema/pfcfg.schema.json's $defs - kept next
# to the schema file conceptually, not textually driven by it, since the
# point is an independent structural check, not a second JSON Schema
# interpreter.
# ---------------------------------------------------------------------------

_SECTION_PATH_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
_ENV_VAR_RE = re.compile(r"^[^}:]+$")


class _StructuralChecker:
    def __init__(self) -> None:
        self.errors: List[str] = []

    def _fail(self, path: str, msg: str) -> None:
        self.errors.append(f"{path}: {msg}")

    def check_bundle(self, bundle: Any) -> List[str]:
        self._check_object(bundle, "<root>", required=["entry", "files"], allowed={"entry", "files"})
        if isinstance(bundle, dict):
            entry = bundle.get("entry")
            if not isinstance(entry, str) or len(entry) < 1:
                self._fail("entry", "must be a non-empty string")
            files = bundle.get("files")
            if not isinstance(files, dict) or len(files) < 1:
                self._fail("files", "must be a non-empty object")
            elif entry is not None and isinstance(files, dict) and entry not in files:
                self._fail("entry", f"{entry!r} is not a key in files")
            if isinstance(files, dict):
                for key, file_obj in files.items():
                    self._check_file(file_obj, f"files/{key}")
        return self.errors

    def _check_object(self, value: Any, path: str, required: List[str], allowed: set) -> bool:
        if not isinstance(value, dict):
            self._fail(path, f"must be an object, got {type(value).__name__}")
            return False
        ok = True
        for key in required:
            if key not in value:
                self._fail(path, f"missing required property {key!r}")
                ok = False
        for key in value:
            if key not in allowed:
                self._fail(path, f"unexpected property {key!r}")
                ok = False
        return ok

    def _check_file(self, value: Any, path: str) -> None:
        if not self._check_object(value, path, required=["path", "nodes"], allowed={"path", "nodes"}):
            return
        if not isinstance(value["path"], str) or len(value["path"]) < 1:
            self._fail(f"{path}/path", "must be a non-empty string")
        nodes = value["nodes"]
        if not isinstance(nodes, list):
            self._fail(f"{path}/nodes", "must be an array")
            return
        for i, node in enumerate(nodes):
            self._check_node(node, f"{path}/nodes[{i}]")

    def _check_node(self, value: Any, path: str) -> None:
        if not isinstance(value, dict) or "type" not in value:
            self._fail(path, "must be an object with a 'type' property")
            return
        t = value["type"]
        if t == "section":
            if self._check_object(value, path, required=["type", "path", "line"], allowed={"type", "path", "line"}):
                if not isinstance(value["path"], str) or not _SECTION_PATH_RE.match(value["path"]):
                    self._fail(f"{path}/path", "must match ^[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)*$")
                self._check_line(value.get("line"), f"{path}/line")
        elif t == "assign":
            if self._check_object(
                value, path, required=["type", "key", "value", "line"], allowed={"type", "key", "value", "line"}
            ):
                if not isinstance(value["key"], str) or not _KEY_RE.match(value["key"]):
                    self._fail(f"{path}/key", "must match ^[A-Za-z0-9_]+$")
                segs = value["value"]
                if not isinstance(segs, list):
                    self._fail(f"{path}/value", "must be an array")
                else:
                    for i, seg in enumerate(segs):
                        self._check_segment(seg, f"{path}/value[{i}]")
                self._check_line(value.get("line"), f"{path}/line")
        elif t == "include":
            required = ["type", "target", "once", "resolved_path", "line"]
            if self._check_object(value, path, required=required, allowed=set(required)):
                if not isinstance(value["target"], str) or len(value["target"]) < 1:
                    self._fail(f"{path}/target", "must be a non-empty string")
                if not isinstance(value["once"], bool):
                    self._fail(f"{path}/once", "must be a boolean")
                if not isinstance(value["resolved_path"], str) or len(value["resolved_path"]) < 1:
                    self._fail(f"{path}/resolved_path", "must be a non-empty string")
                self._check_line(value.get("line"), f"{path}/line")
        elif t == "conditional":
            required = ["type", "kind", "var", "body", "line"]
            if self._check_object(value, path, required=required, allowed=set(required)):
                if value["kind"] not in ("ifdef", "ifndef"):
                    self._fail(f"{path}/kind", "must be 'ifdef' or 'ifndef'")
                if not isinstance(value["var"], str) or len(value["var"]) < 1:
                    self._fail(f"{path}/var", "must be a non-empty string")
                body = value["body"]
                if not isinstance(body, list):
                    self._fail(f"{path}/body", "must be an array")
                else:
                    for i, sub in enumerate(body):
                        self._check_node(sub, f"{path}/body[{i}]")
                self._check_line(value.get("line"), f"{path}/line")
        else:
            self._fail(path, f"unknown node type {t!r}")

    def _check_segment(self, value: Any, path: str) -> None:
        if not isinstance(value, dict) or "type" not in value:
            self._fail(path, "must be an object with a 'type' property")
            return
        t = value["type"]
        if t == "literal":
            if self._check_object(value, path, required=["type", "text"], allowed={"type", "text"}):
                if not isinstance(value["text"], str):
                    self._fail(f"{path}/text", "must be a string")
        elif t == "env":
            allowed = {"type", "var", "mode", "expr"}
            if self._check_object(value, path, required=["type", "var", "mode"], allowed=allowed):
                if not isinstance(value["var"], str) or not _ENV_VAR_RE.match(value["var"]) or len(value["var"]) < 1:
                    self._fail(f"{path}/var", "must be a non-empty string with no '}' or ':'")
                mode = value["mode"]
                if mode not in ("plain", "default", "alt"):
                    self._fail(f"{path}/mode", "must be 'plain', 'default', or 'alt'")
                has_expr = "expr" in value
                if mode == "plain" and has_expr:
                    self._fail(path, "'expr' must be absent when mode is 'plain'")
                if mode in ("default", "alt") and not has_expr:
                    self._fail(path, f"'expr' is required when mode is {mode!r}")
                if has_expr:
                    expr = value["expr"]
                    if not isinstance(expr, list):
                        self._fail(f"{path}/expr", "must be an array")
                    else:
                        for i, sub in enumerate(expr):
                            self._check_segment(sub, f"{path}/expr[{i}]")
        elif t == "key":
            if self._check_object(value, path, required=["type", "path"], allowed={"type", "path"}):
                if not isinstance(value["path"], str) or len(value["path"]) < 1:
                    self._fail(f"{path}/path", "must be a non-empty string")
        else:
            self._fail(path, f"unknown segment type {t!r}")

    def _check_line(self, value: Any, path: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            self._fail(path, "must be an integer >= 1")
