"""Turns .pfcfg text into a per-file parse tree (model.ParsedFile).

Deliberately dumb: does NOT resolve @include/@include_once or evaluate
@ifdef/@ifndef — that is the walker's job, because resolution is one
interleaved top-to-bottom pass across files, not parse-then-resolve
per file. This module only has to get one file's own text right.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

from .errors import ParseError
from .model import Conditional, Include, KeyAssignment, Node, ParsedFile, SectionHeader, SourceLoc

_SECTION_RE = re.compile(r"^\[([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)\]$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _strip_comment(line: str) -> str:
    """Strip a '#'/';' comment running to end of line.

    format-reference.md documents inline trailing comments as real syntax
    ("# Comment to end of line"), not just full-line ones, and real
    customer configs will use them — so refusing to strip trailing
    comments at all would silently fold "# comment" text into values
    instead of erroring loudly, which is a worse failure mode (silent
    corruption vs. a caught exception) than the bug this replaced. Two
    things must NOT be mistaken for a comment marker:

      - '#'/';' inside a double-quoted value (already handled: in_quotes)
      - '#'/';' inside an active ${...} or $(...) interpolation span, e.g.
        notifications.pfcfg's `${SLACK_CHANNEL:-#builds}` — the '#' there
        is literal replacement text, not a comment.

    interp_depth is a single counter (not two, one per bracket type)
    because pfcfg interpolation syntax is always properly nested
    regardless of whether a given span was opened by '${' or '$(' — e.g.
    ${RELEASE_VERSION:-0.0.0-$(build.node_version)} closes ')' then '}' in
    that order, so tracking "are we inside some span" is sufficient; we
    don't need to know which bracket a given close matches.
    """
    in_quotes = False
    interp_depth = 0
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == '"':
            if in_quotes:
                j = i - 1
                backslashes = 0
                while j >= 0 and line[j] == "\\":
                    backslashes += 1
                    j -= 1
                if backslashes % 2 == 0:
                    in_quotes = False
            else:
                in_quotes = True
            i += 1
            continue
        if in_quotes:
            i += 1
            continue
        if c == "$" and i + 1 < n and line[i + 1] in ("{", "("):
            interp_depth += 1
            i += 2
            continue
        if c in ("}", ")") and interp_depth > 0:
            interp_depth -= 1
            i += 1
            continue
        if c in ("#", ";") and interp_depth == 0:
            return line[:i]
        i += 1
    return line


def _parse_value(raw: str) -> str:
    """Trim an unquoted value, or unquote+unescape a quoted one.

    Quoted values: \\" -> ", \\\\ -> \\. Unquoted values: whitespace-trimmed.
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        inner = s[1:-1]
        out: List[str] = []
        i = 0
        while i < len(inner):
            c = inner[i]
            if c == "\\" and i + 1 < len(inner) and inner[i + 1] in ('"', "\\"):
                out.append(inner[i + 1])
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)
    return s


class _FileParser:
    """Recursive-descent over one file's lines. @ifdef/@ifndef nest
    (bodies are parsed by recursing into _parse_block); everything else is
    a flat statement in whatever block it appears in.
    """

    def __init__(self, path: str, lines: List[str]):
        self.path = path
        self.lines = lines
        # Shared across the whole file (all nesting depths), not per-block:
        # the format reference's "before any section header in this file"
        # rule is file-wide, not block-scoped.
        self.seen_section = False

    def parse(self) -> List[Node]:
        nodes, end = self._parse_block(0, expect_endif=False)
        if end != len(self.lines):
            raise ParseError("unexpected trailing content", SourceLoc(self.path, end + 1))
        return nodes

    def _parse_block(self, start: int, expect_endif: bool) -> Tuple[List[Node], int]:
        nodes: List[Node] = []
        i = start
        while i < len(self.lines):
            lineno = i + 1
            line = _strip_comment(self.lines[i]).strip()
            if not line:
                i += 1
                continue

            if line.startswith("@"):
                parts = line.split(None, 1)
                directive = parts[0]
                rest = parts[1].strip() if len(parts) > 1 else ""

                if directive == "@endif":
                    if not expect_endif:
                        raise ParseError("unexpected @endif with no matching @ifdef/@ifndef", SourceLoc(self.path, lineno))
                    return nodes, i + 1

                if directive in ("@ifdef", "@ifndef"):
                    if not rest:
                        raise ParseError(f"{directive} requires a variable name", SourceLoc(self.path, lineno))
                    kind = "ifdef" if directive == "@ifdef" else "ifndef"
                    body, next_i = self._parse_block(i + 1, expect_endif=True)
                    nodes.append(Conditional(kind=kind, var=rest, body=body, loc=SourceLoc(self.path, lineno)))
                    i = next_i
                    continue

                if directive in ("@include", "@include_once"):
                    if self.seen_section:
                        raise ParseError(
                            "include directive appears after a section header in this file",
                            SourceLoc(self.path, lineno),
                        )
                    if not rest:
                        raise ParseError(f"{directive} requires a path", SourceLoc(self.path, lineno))
                    nodes.append(Include(target=rest, once=(directive == "@include_once"), loc=SourceLoc(self.path, lineno)))
                    i += 1
                    continue

                raise ParseError(f"unknown directive: {directive}", SourceLoc(self.path, lineno))

            if line.startswith("["):
                m = _SECTION_RE.match(line)
                if not m:
                    raise ParseError(f"malformed section header: {line!r}", SourceLoc(self.path, lineno))
                self.seen_section = True
                nodes.append(SectionHeader(path=m.group(1), loc=SourceLoc(self.path, lineno)))
                i += 1
                continue

            if "=" in line:
                key_part, _, value_part = line.partition("=")
                key = key_part.strip()
                if not _KEY_RE.match(key):
                    raise ParseError(f"malformed key: {key!r}", SourceLoc(self.path, lineno))
                nodes.append(KeyAssignment(key=key, raw_value=_parse_value(value_part), loc=SourceLoc(self.path, lineno)))
                i += 1
                continue

            raise ParseError(f"unrecognized line: {line!r}", SourceLoc(self.path, lineno))

        if expect_endif:
            raise ParseError("missing @endif", SourceLoc(self.path, start + 1))
        return nodes, i


def parse_text(path: str, text: str) -> ParsedFile:
    nodes = _FileParser(path, text.splitlines()).parse()
    return ParsedFile(path=path, nodes=nodes)


# Parsing is pure text -> AST, so caching by resolved path is safe to share
# across evaluate() calls with different environments — unlike walk state
# (include-once set, include-cycle stack), which must NOT be shared.
_PARSE_CACHE: Dict[str, ParsedFile] = {}


def parse_file_cached(path: str) -> ParsedFile:
    real = os.path.realpath(path)
    cached = _PARSE_CACHE.get(real)
    if cached is None:
        with open(real, "r", encoding="utf-8") as f:
            text = f.read()
        cached = parse_text(real, text)
        _PARSE_CACHE[real] = cached
    return cached
