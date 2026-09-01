"""Interpolation: resolves ${...} / $(...) over a MergedConfig, producing
the fully resolved flat map — "effective settings".

Runs strictly after the walk (model.MergedConfig already has includes
expanded, conditionals gated, and last-writer-wins applied) — cross-refs
can point at keys defined later in the walk or in other files, so there's
nothing to resolve against until the full raw map exists.

Two kinds of reference, and they compose (a ${VAR:-default} or
${VAR:+alt} branch can itself contain $(section.key), and vice versa isn't
possible but doesn't need to be ruled out specially — see interpolation-
cascade.pfcfg's gamma/delta/release.version-style values):

  ${VAR}              -> env lookup, "" if unset. Leaf: never a graph edge.
  ${VAR:-default}      -> env value if set+non-empty, else resolve `default`
  ${VAR:+alt}          -> resolve `alt` if set+non-empty, else ""
  $(section.key)        -> resolved value of another path. Graph edge: this
                          is what the DFS/cycle-detection/depth-cap in
                          _resolve_path operates over.

Because a value can itself need resolving before it can be substituted
into whoever references it, this is one recursive, memoized resolve(path)
per path — not "resolve all ${...} in one pass, then all $(...) in a
second pass". A regex-based fixpoint-replace approach breaks on nesting
and can't cleanly tell "still converging" from "genuinely circular".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from .errors import ParseError
from .model import Diagnostic, Environment, MergedConfig, RawAssignment, ResolvedConfig, SourceLoc

# format-reference.md leaves the pass limit unspecified ("pick a reasonable
# one, document it") — 10 is the number locked in for this project. The
# legitimate chains in the corpus (interpolation-cascade.pfcfg's
# epsilon -> delta -> gamma -> beta -> alpha) run 5 deep, well inside it.
MAX_DEPTH = 10


# ---------------------------------------------------------------------------
# Value grammar: a value is a sequence of segments. ${...}'s default/alt
# branch is itself a sequence of segments (recursively), which is how
# ${RELEASE_VERSION:-0.0.0-$(build.node_version)} and
# ${CASCADE_GAMMA:-$(cascade.beta)} parse correctly. $(...) targets are
# plain dotted paths in every sample seen — no nested refs inside $(...).
# ---------------------------------------------------------------------------


@dataclass
class Literal:
    text: str


@dataclass
class EnvRef:
    var: str
    mode: str  # "plain" | "default" | "alt"
    expr: Optional[List["Segment"]]  # the default/alt sub-expression; None for plain ${VAR}


@dataclass
class KeyRef:
    path: str  # dotted target, e.g. "cascade.beta"


Segment = Union[Literal, EnvRef, KeyRef]


class _ValueParser:
    """Recursive-descent over one raw value string. Each ${ we open consumes
    its own matching } via recursion before control returns to the caller,
    so a bare } encountered while scanning always belongs to the nearest
    enclosing ${ — no separate brace-depth counter needed.
    """

    def __init__(self, text: str, loc: SourceLoc):
        self.s = text
        self.i = 0
        self.n = len(text)
        self.loc = loc

    def parse(self) -> List[Segment]:
        return self._parse_segments(stop_at_close_brace=False)

    def _parse_segments(self, stop_at_close_brace: bool) -> List[Segment]:
        segments: List[Segment] = []
        buf: List[str] = []

        def flush() -> None:
            if buf:
                segments.append(Literal("".join(buf)))
                buf.clear()

        while self.i < self.n:
            c = self.s[self.i]
            if stop_at_close_brace and c == "}":
                break
            if c == "$" and self.i + 1 < self.n and self.s[self.i + 1] == "{":
                flush()
                segments.append(self._parse_env_ref())
                continue
            if c == "$" and self.i + 1 < self.n and self.s[self.i + 1] == "(":
                flush()
                segments.append(self._parse_key_ref())
                continue
            buf.append(c)
            self.i += 1
        flush()
        return segments

    def _parse_env_ref(self) -> EnvRef:
        self.i += 2  # consume "${"
        start = self.i
        while self.i < self.n and self.s[self.i] not in ("}", ":"):
            self.i += 1
        var = self.s[start : self.i]
        if not var:
            raise ParseError("empty variable name in ${...}", self.loc)

        mode = "plain"
        expr: Optional[List[Segment]] = None
        if self.i < self.n and self.s[self.i] == ":":
            op = self.s[self.i : self.i + 2]
            if op == ":-":
                mode = "default"
            elif op == ":+":
                mode = "alt"
            else:
                raise ParseError(f"malformed interpolation operator {op!r} in ${{{var}...}}", self.loc)
            self.i += 2
            expr = self._parse_segments(stop_at_close_brace=True)

        if self.i >= self.n or self.s[self.i] != "}":
            raise ParseError(f"unterminated \"${{{var}\" (missing closing '}}')", self.loc)
        self.i += 1  # consume "}"
        return EnvRef(var=var, mode=mode, expr=expr)

    def _parse_key_ref(self) -> KeyRef:
        self.i += 2  # consume "$("
        start = self.i
        while self.i < self.n and self.s[self.i] != ")":
            self.i += 1
        if self.i >= self.n:
            raise ParseError("unterminated \"$(\" (missing closing ')')", self.loc)
        path = self.s[start : self.i]
        self.i += 1  # consume ")"
        if not path:
            raise ParseError("empty $() reference", self.loc)
        return KeyRef(path=path)


# ---------------------------------------------------------------------------
# Resolver core: memoized recursive resolve(path), DFS over $(...) edges
# only (${...} is resolved inline against env — it's a leaf, never pushes
# the stack). ${VAR} unset -> "" is spec'd (format-reference.md). A
# $(section.key) target that doesn't exist at all is NOT spec'd there —
# it's this project's own interpretation (see DECISIONS.md) that it must
# be a hard "missing_ref" error rather than silently resolving to "",
# kept as a distinct diagnostic kind from "cycle" / "max_depth" so it's
# never confused with the spec'd ${VAR}-empty behavior.
# ---------------------------------------------------------------------------


@dataclass
class _ResolveContext:
    env: Environment
    assignments: Dict[str, RawAssignment]
    resolved: Dict[str, str] = field(default_factory=dict)
    failed: Dict[str, Diagnostic] = field(default_factory=dict)
    parsed_cache: Dict[str, List[Segment]] = field(default_factory=dict)
    errors: List[Diagnostic] = field(default_factory=list)


def resolve_all(merged: MergedConfig, env: Environment) -> ResolvedConfig:
    """Resolve every path in a MergedConfig's raw assignments. Iteration
    order doesn't affect the result (resolution is memoized recursion, not
    order-dependent passes) — only affects the order diagnostics land in
    `errors`, which follows walk order.
    """
    ctx = _ResolveContext(env=env, assignments=merged.assignments)
    for path in merged.assignments:
        _resolve_path(path, ctx, stack=[])
    return ResolvedConfig(entry=merged.entry, environment=env, values=dict(ctx.resolved), errors=list(ctx.errors))


def _resolve_path(path: str, ctx: _ResolveContext, stack: List[str]) -> Optional[str]:
    if path in ctx.resolved:
        return ctx.resolved[path]
    if path in ctx.failed:
        return None

    assignment = ctx.assignments.get(path)
    if assignment is None:
        # No provenance to attach a diagnostic to here — the caller (the
        # $(...) reference's own resolving path) records missing_ref using
        # its own loc, since it's the one that knows where the bad
        # reference was written.
        return None

    if path in stack:
        cycle = stack[stack.index(path) :] + [path]
        diag = Diagnostic(
            kind="cycle",
            reason=f"circular reference: {' -> '.join(cycle)}",
            loc=assignment.loc,
            path=path,
        )
        ctx.failed[path] = diag
        ctx.errors.append(diag)
        return None

    if len(stack) >= MAX_DEPTH:
        diag = Diagnostic(
            kind="max_depth",
            reason=f"interpolation exceeded max depth of {MAX_DEPTH} (chain: {' -> '.join(stack + [path])})",
            loc=assignment.loc,
            path=path,
        )
        ctx.failed[path] = diag
        ctx.errors.append(diag)
        return None

    segments = ctx.parsed_cache.get(path)
    if segments is None:
        segments = _ValueParser(assignment.value, assignment.loc).parse()
        ctx.parsed_cache[path] = segments

    stack.append(path)
    value, failure_target = _resolve_segments(segments, ctx, stack)
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
            kind = underlying.kind if underlying else "missing_ref"
            if underlying:
                reason += f": {underlying.reason}"
        else:
            kind = "missing_ref"
            reason = f"reference to undefined key {failure_target!r}"
        diag = Diagnostic(kind=kind, reason=reason, loc=assignment.loc, path=path)
        ctx.failed[path] = diag
        ctx.errors.append(diag)
        return None

    ctx.resolved[path] = value
    return value


def _resolve_segments(segments: List[Segment], ctx: _ResolveContext, stack: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Returns (resolved_string, None) on success, or (None, failing_target)
    where failing_target is the $(...) path that could not be resolved.
    """
    parts: List[str] = []
    for seg in segments:
        if isinstance(seg, Literal):
            parts.append(seg.text)
        elif isinstance(seg, EnvRef):
            val = ctx.env.get(seg.var, "")
            if seg.mode == "plain":
                parts.append(val)
            elif seg.mode == "default":
                if val:
                    parts.append(val)
                else:
                    sub, fail = _resolve_segments(seg.expr, ctx, stack)
                    if sub is None:
                        return None, fail
                    parts.append(sub)
            elif seg.mode == "alt":
                if val:
                    sub, fail = _resolve_segments(seg.expr, ctx, stack)
                    if sub is None:
                        return None, fail
                    parts.append(sub)
                else:
                    parts.append("")
            else:  # pragma: no cover - exhaustive over EnvRef.mode
                raise AssertionError(f"unknown EnvRef mode: {seg.mode!r}")
        elif isinstance(seg, KeyRef):
            child = _resolve_path(seg.path, ctx, stack)
            if child is None:
                return None, seg.path
            parts.append(child)
        else:  # pragma: no cover - exhaustive over Segment
            raise AssertionError(f"unknown segment type: {seg!r}")
    return "".join(parts), None
