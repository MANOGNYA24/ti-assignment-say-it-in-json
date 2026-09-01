"""Independent JSON evaluator for the pfcfg parse-tree bundle
(schema/pfcfg.schema.json).

Consumes exactly what the bundle already contains — no filesystem access,
no re-parsing of raw strings — and reimplements the walker.py +
interpolate.py semantics from scratch, over JSON nodes/segments instead of
model.py dataclasses. Deliberately does NOT import pfcfg.walker or
pfcfg.interpolate (or any other pfcfg resolver module): the point of this
module is to be a second, separately-written implementation that a
verifier can diff against the reference evaluator, not a wrapper around it.

Public entry point: evaluate_bundle(bundle, env) -> {"values", "errors"}.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_DEPTH = 10  # must match interpolate.py's locked-in cap


# ---------------------------------------------------------------------------
# Walk phase (ports walker.py): interleaved top-to-bottom traversal over the
# bundle's node arrays, includes spliced in inline, conditionals gated
# against `env`, last-writer-wins by walk position.
# ---------------------------------------------------------------------------


class _Assignment:
    __slots__ = ("path", "value", "line", "file")

    def __init__(self, path: str, value: List[dict], line: int, file: str):
        self.path = path
        self.value = value  # unresolved segment array, straight from the bundle
        self.line = line
        self.file = file


class _WalkCtx:
    def __init__(self, env: Dict[str, str]):
        self.env = env
        self.assignments: Dict[str, _Assignment] = {}
        self.seen_once: set = set()  # resolved paths "already included in this load"
        self.include_chain: List[str] = []  # ancestry stack, for include-cycle detection
        self.diagnostics: List[dict] = []
        self.current_section: Optional[str] = None  # one pointer for the WHOLE walk —
        # never reset on entering/leaving an included file. See walker.py's
        # _walk_nodes comment: @include splices inline, as if the included
        # text were pasted in place, so a bare assignment right after an
        # include returns still uses whatever section that included file
        # last set. Do not scope this per-file.


def _env_truthy(env: Dict[str, str], var: str) -> bool:
    return bool(env.get(var))


def _walk(bundle: dict, env: Dict[str, str]) -> _WalkCtx:
    ctx = _WalkCtx(env)
    entry = bundle["entry"]
    _enter_file(entry, bundle, ctx)
    return ctx


def _enter_file(path: str, bundle: dict, ctx: _WalkCtx) -> None:
    file_obj = bundle["files"][path]
    ctx.include_chain.append(path)
    _walk_nodes(file_obj["nodes"], bundle, ctx)
    ctx.include_chain.pop()


def _walk_nodes(nodes: List[dict], bundle: dict, ctx: _WalkCtx) -> None:
    for node in nodes:
        kind = node["type"]
        if kind == "section":
            ctx.current_section = node["path"]
        elif kind == "assign":
            _apply_assignment(node, ctx)
        elif kind == "conditional":
            truthy = _env_truthy(ctx.env, node["var"])
            active = truthy if node["kind"] == "ifdef" else not truthy
            if active:
                _walk_nodes(node["body"], bundle, ctx)
        elif kind == "include":
            _handle_include(node, bundle, ctx)
        else:  # pragma: no cover - exhaustive over bundle Node union
            raise AssertionError(f"unknown node type: {kind!r}")


def _apply_assignment(node: dict, ctx: _WalkCtx) -> None:
    if ctx.current_section is None:
        raise ValueError(f"key {node['key']!r} assigned before any section header was ever set (line {node['line']})")
    path = f"{ctx.current_section}.{node['key']}"
    # Last-writer-wins by walk position: plain dict assignment always
    # replaces whatever was there. (Python dicts preserve first-insertion
    # order across updates to an existing key — matched deliberately, since
    # interpolation error order below follows this same iteration order.)
    ctx.assignments[path] = _Assignment(path=path, value=node["value"], line=node["line"], file=None)


def _handle_include(node: dict, bundle: dict, ctx: _WalkCtx) -> None:
    target = node["resolved_path"]

    if node["once"] and target in ctx.seen_once:
        return  # @include_once: already included earlier in this load

    if target in ctx.include_chain:
        chain_display = " -> ".join(ctx.include_chain + [target])
        ctx.diagnostics.append(
            {
                "path": target,  # the include's resolved_path — the file location of the cycle
                "kind": "include_cycle",
                "reason": f"include cycle detected: {chain_display}",
            }
        )
        return

    # Bundle is self-contained by construction (design-note.md section 1):
    # every statically reachable file is present regardless of environment.
    # A missing key here means a malformed bundle, not an evaluation
    # outcome, so this is a hard failure rather than a diagnostic.
    if target not in bundle["files"]:
        raise KeyError(f"bundle missing file for resolved_path {target!r} (referenced at line {node['line']})")

    # Decision (mirrors walker.py): BOTH @include and @include_once mark the
    # path as seen; only @include_once *consults* seen_once before walking.
    # A plain @include always walks unconditionally, regardless of seen_once.
    ctx.seen_once.add(target)
    _enter_file(target, bundle, ctx)


# ---------------------------------------------------------------------------
# Interpolation phase (ports interpolate.py): memoized recursive resolve()
# per dotted path, DFS over KeyRef ("$(...)") edges only. EnvRef ("${...}")
# is always a leaf — resolved inline against `env`, never pushes the stack.
# Segments are consumed directly from the bundle's JSON arrays; there is no
# string grammar to re-parse here.
# ---------------------------------------------------------------------------


class _ResolveCtx:
    def __init__(self, env: Dict[str, str], assignments: Dict[str, _Assignment]):
        self.env = env
        self.assignments = assignments
        self.resolved: Dict[str, str] = {}
        self.failed: Dict[str, dict] = {}
        self.errors: List[dict] = []


def _resolve_all(assignments: Dict[str, _Assignment], env: Dict[str, str]) -> (Dict[str, str], List[dict]):
    ctx = _ResolveCtx(env, assignments)
    for path in assignments:  # dict iteration order = walk-order of first assignment
        _resolve_path(path, ctx, stack=[])
    return dict(ctx.resolved), list(ctx.errors)


def _resolve_path(path: str, ctx: _ResolveCtx, stack: List[str]) -> Optional[str]:
    if path in ctx.resolved:
        return ctx.resolved[path]
    if path in ctx.failed:
        return None

    assignment = ctx.assignments.get(path)
    if assignment is None:
        # No provenance to attach a diagnostic to here — the caller (the
        # $(...) reference's own resolving path) records missing_ref using
        # its own line, since it's the one that knows where the bad
        # reference was written.
        return None

    if path in stack:
        cycle = stack[stack.index(path):] + [path]
        diag = {"path": path, "kind": "cycle", "reason": f"circular reference: {' -> '.join(cycle)}"}
        ctx.failed[path] = diag
        ctx.errors.append(diag)
        return None

    if len(stack) >= MAX_DEPTH:
        diag = {
            "path": path,
            "kind": "max_depth",
            "reason": f"interpolation exceeded max depth of {MAX_DEPTH} (chain: {' -> '.join(stack + [path])})",
        }
        ctx.failed[path] = diag
        ctx.errors.append(diag)
        return None

    stack.append(path)
    value, failure_target = _resolve_segments(assignment.value, ctx, stack)
    stack.pop()

    if value is None:
        if path in ctx.failed:
            # Already recorded — e.g. this exact path was the re-entry
            # point of a cycle detected deeper in this same chain, while
            # this (outer) frame for the same path is still unwinding.
            return None
        if failure_target in ctx.assignments:
            underlying = ctx.failed.get(failure_target)
            reason = f"depends on {failure_target}, which failed to resolve"
            kind = underlying["kind"] if underlying else "missing_ref"
            if underlying:
                reason += f": {underlying['reason']}"
        else:
            kind = "missing_ref"
            reason = f"reference to undefined key {failure_target!r}"
        diag = {"path": path, "kind": kind, "reason": reason}
        ctx.failed[path] = diag
        ctx.errors.append(diag)
        return None

    ctx.resolved[path] = value
    return value


def _resolve_segments(segments: List[dict], ctx: _ResolveCtx, stack: List[str]):
    """Returns (resolved_string, None) on success, or (None, failing_target)
    where failing_target is the $(...) path that could not be resolved.
    """
    parts: List[str] = []
    for seg in segments:
        seg_type = seg["type"]
        if seg_type == "literal":
            parts.append(seg["text"])
        elif seg_type == "env":
            val = ctx.env.get(seg["var"], "")
            mode = seg["mode"]
            if mode == "plain":
                parts.append(val)
            elif mode == "default":
                if val:
                    parts.append(val)
                else:
                    sub, fail = _resolve_segments(seg["expr"], ctx, stack)
                    if sub is None:
                        return None, fail
                    parts.append(sub)
            elif mode == "alt":
                if val:
                    sub, fail = _resolve_segments(seg["expr"], ctx, stack)
                    if sub is None:
                        return None, fail
                    parts.append(sub)
                else:
                    parts.append("")
            else:  # pragma: no cover - exhaustive over EnvRef.mode
                raise AssertionError(f"unknown env segment mode: {mode!r}")
        elif seg_type == "key":
            child = _resolve_path(seg["path"], ctx, stack)
            if child is None:
                return None, seg["path"]
            parts.append(child)
        else:  # pragma: no cover - exhaustive over Segment
            raise AssertionError(f"unknown segment type: {seg_type!r}")
    return "".join(parts), None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_bundle(bundle: dict, env: Dict[str, str]) -> Dict[str, Any]:
    """Evaluate a pfcfg JSON bundle under an explicit environment, producing
    the same effective-settings shape the reference evaluator
    (pfcfg.walker.walk + pfcfg.interpolate.resolve_all) produces.

    Returns {"values": {dotted_path: str}, "errors": [{path, kind, reason}]}.

    `errors` concatenates walk-time diagnostics (include_cycle) before
    interpolation-time errors (cycle, max_depth, missing_ref) — the oracle
    keeps these in two separate lists (MergedConfig.diagnostics vs.
    ResolvedConfig.errors) and never merges them itself; this is where that
    merge happens for the single-list contract here.
    """
    walk_ctx = _walk(bundle, env)
    values, interp_errors = _resolve_all(walk_ctx.assignments, env)
    errors = walk_ctx.diagnostics + interp_errors
    return {"values": values, "errors": errors}
