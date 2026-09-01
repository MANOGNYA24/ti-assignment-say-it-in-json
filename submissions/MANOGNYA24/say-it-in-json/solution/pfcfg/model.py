"""Shared data types for the .pfcfg parser and evaluator.

No logic here — parser.py builds the parse tree, walker.py builds
MergedConfig, interpolate.py (next) builds ResolvedConfig.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Union

# Environment is an explicit input everywhere — never read from os.environ.
# The same config is evaluated under many fake environments.
Environment = Dict[str, str]


@dataclass(frozen=True)
class SourceLoc:
    file: str  # normalized (realpath) source file
    line: int  # 1-indexed


# ---------------------------------------------------------------------------
# Parse tree: one file, includes/conditionals NOT yet resolved.
#
# SectionHeader is a pointer-setting statement, not a container: it changes
# "current section" for subsequent bare key assignments and has no body.
# Conditional is the only node type that nests.
# ---------------------------------------------------------------------------


@dataclass
class SectionHeader:
    path: str  # dotted, e.g. "notify.slack"
    loc: SourceLoc


@dataclass
class KeyAssignment:
    key: str
    raw_value: str  # unquoted/unescaped, pre-interpolation, exact text
    loc: SourceLoc


@dataclass
class Include:
    target: str  # literal path text from the directive, relative to its file
    once: bool
    loc: SourceLoc


@dataclass
class Conditional:
    kind: Literal["ifdef", "ifndef"]
    var: str
    body: "List[Node]"
    loc: SourceLoc


Node = Union[SectionHeader, KeyAssignment, Include, Conditional]


@dataclass
class ParsedFile:
    path: str  # normalized (realpath)
    nodes: List[Node]


# ---------------------------------------------------------------------------
# Merged intermediate: post-walk (includes expanded, conditionals gated,
# last-writer-wins applied), pre-interpolation. This is the raw merged
# key/value map the interpolation phase operates on.
# ---------------------------------------------------------------------------


@dataclass
class RawAssignment:
    path: str  # full dotted "section.key"
    value: str  # raw text, may still contain ${...} / $(...) tokens
    loc: SourceLoc  # winning assignment's origin
    overridden: List[SourceLoc] = field(default_factory=list)  # prior losing assignments, in walk order


# A diagnostic covers both walk-time problems (e.g. include cycles) and,
# later, interpolation-time problems (cycles, max-depth, missing refs) with
# one uniform shape so both can feed the same unmigratable report.
DiagnosticKind = Literal["include_cycle", "cycle", "max_depth", "missing_ref"]


@dataclass
class Diagnostic:
    kind: DiagnosticKind
    reason: str
    loc: SourceLoc
    path: Optional[str] = None  # dotted section.key this diagnostic is about, if applicable


@dataclass
class MergedConfig:
    entry: str
    assignments: Dict[str, RawAssignment]  # dotted path -> winning assignment
    include_order: List[str]  # resolved paths actually walked, in walk order
    diagnostics: List[Diagnostic]  # e.g. include cycles


# ---------------------------------------------------------------------------
# Resolved output ("effective settings") — produced by interpolate.py, next.
# ---------------------------------------------------------------------------


@dataclass
class ResolvedConfig:
    entry: str
    environment: Environment
    values: Dict[str, str]  # dotted path -> fully resolved value; only cleanly-resolved paths
    errors: List[Diagnostic]  # everything that failed to resolve
