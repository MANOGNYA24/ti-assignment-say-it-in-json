"""The single interleaved top-to-bottom walk.

Expands @include/@include_once inline at the point they appear, gates on
@ifdef/@ifndef against the given environment, and applies last-writer-wins
by walk position — producing the raw merged key/value map that
interpolate.py (next) will resolve. This module does NOT touch
${...} / $(...) interpolation.

Three decisions locked in for this pass (see DECISIONS.md for the
reasoning behind each):

1. @include_once dedups against "already included in this load" — and
   BOTH a plain @include and an @include_once mark a resolved path as
   seen. Only @include_once *consults* the seen-set before walking; a
   plain @include always walks unconditionally. This is what makes a
   late diamond re-include (globex/overrides.pfcfg re-@include_once'ing
   _base/defaults.pfcfg after ci-shared.pfcfg already overlaid it) skip
   instead of silently reverting the overlay.

2. A $(section.key) reference to a path that doesn't exist at all is our
   own interpretation, not something format-reference.md specifies (it
   only covers unset ${VAR} defaulting to empty) — handled in
   interpolate.py, noted here only because it's the sibling decision to
   include cycles below.

3. Include cycles are tracked separately from the once-set, via a stack
   of currently-open files (ancestry), and reported as a distinct
   `include_cycle` diagnostic rather than raised or looped forever.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Set

from .errors import WalkError
from .model import (
    Conditional,
    Diagnostic,
    Environment,
    Include,
    KeyAssignment,
    MergedConfig,
    Node,
    RawAssignment,
    SectionHeader,
)
from .parser import parse_file_cached


def _env_truthy(env: Environment, var: str) -> bool:
    """Set and non-empty, per format-reference.md's @ifdef/${VAR:+...} rule."""
    return bool(env.get(var))


@dataclass
class _WalkContext:
    env: Environment
    assignments: Dict[str, RawAssignment] = field(default_factory=dict)
    seen_once: Set[str] = field(default_factory=set)  # resolved paths "already included in this load"
    include_order: List[str] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    include_chain: List[str] = field(default_factory=list)  # ancestry stack, for include-cycle detection
    current_section: str | None = None  # shared/global across include boundaries — see walk()


def walk(entry_path: str, env: Environment) -> MergedConfig:
    """Run the single interleaved walk for one (entry config, environment)
    pair. Fresh _WalkContext every call: seen_once/include_chain are
    walk-scoped, never shared across different environments, since which
    includes are even reached depends on env-gated conditionals.
    """
    entry_abs = os.path.realpath(entry_path)
    ctx = _WalkContext(env=env)
    _enter_file(entry_abs, ctx)
    return MergedConfig(
        entry=entry_abs,
        assignments=ctx.assignments,
        include_order=ctx.include_order,
        diagnostics=ctx.diagnostics,
    )


def _enter_file(path: str, ctx: _WalkContext) -> None:
    parsed = parse_file_cached(path)
    ctx.include_chain.append(path)
    _walk_nodes(parsed.nodes, ctx, os.path.dirname(path))
    ctx.include_chain.pop()


def _walk_nodes(nodes: List[Node], ctx: _WalkContext, base_dir: str) -> None:
    for node in nodes:
        if isinstance(node, SectionHeader):
            # current_section is a single pointer for the whole walk, not
            # reset on entering/leaving an included file: "@include expands
            # inline at the point it appears" is taken literally, as if the
            # included text were spliced in. Every included file in this
            # corpus opens with its own section header before any key, so
            # this never actually gets exercised across a boundary here —
            # but it's the faithful reading of "one interleaved pass".
            ctx.current_section = node.path
        elif isinstance(node, KeyAssignment):
            _apply_assignment(node, ctx)
        elif isinstance(node, Conditional):
            truthy = _env_truthy(ctx.env, node.var)
            active = truthy if node.kind == "ifdef" else not truthy
            if active:
                _walk_nodes(node.body, ctx, base_dir)
        elif isinstance(node, Include):
            _handle_include(node, ctx, base_dir)
        else:  # pragma: no cover - exhaustive over model.Node
            raise AssertionError(f"unknown node type: {node!r}")


def _apply_assignment(node: KeyAssignment, ctx: _WalkContext) -> None:
    if ctx.current_section is None:
        raise WalkError(f"key {node.key!r} assigned before any section header was ever set", node.loc)
    path = f"{ctx.current_section}.{node.key}"
    existing = ctx.assignments.get(path)
    overridden = (existing.overridden + [existing.loc]) if existing else []
    # Last-writer-wins by walk position: this assignment always replaces
    # whatever was there, since we're visiting nodes in interleaved
    # top-to-bottom order already (includes inlined, conditionals gated).
    ctx.assignments[path] = RawAssignment(path=path, value=node.raw_value, loc=node.loc, overridden=overridden)


def _handle_include(node: Include, ctx: _WalkContext, base_dir: str) -> None:
    target_abs = os.path.realpath(os.path.join(base_dir, node.target))

    if node.once and target_abs in ctx.seen_once:
        return  # @include_once: this path was already included earlier in this load

    if target_abs in ctx.include_chain:
        chain_display = " -> ".join(ctx.include_chain + [target_abs])
        ctx.diagnostics.append(
            Diagnostic(
                kind="include_cycle",
                reason=f"include cycle detected: {chain_display}",
                loc=node.loc,
            )
        )
        return

    if not os.path.isfile(target_abs):
        raise WalkError(f"included file not found: {node.target!r} (resolved to {target_abs})", node.loc)

    # Decision: BOTH @include and @include_once mark the path as seen, so a
    # later @include_once of the same path is skipped even if it only ever
    # arrived via a plain @include. A plain @include still always walks
    # here (it doesn't consult seen_once), it just records that it happened.
    ctx.seen_once.add(target_abs)
    ctx.include_order.append(target_abs)
    _enter_file(target_abs, ctx)
