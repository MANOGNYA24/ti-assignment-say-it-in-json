"""Exceptions raised by the parser and walker.

These are for structurally invalid input (malformed syntax, an include
directive after a section header, a key assigned with no section context
ever established) — genuine defects in the source file, not resolvable
runtime conditions. Contrast with model.Diagnostic, which represents
conditions that ARE expected to occur in valid configs under some
environment (include cycles, interpolation cycles, missing cross-refs) and
so are collected rather than raised.
"""

from __future__ import annotations

from typing import Optional

from .model import SourceLoc


class PfcfgError(Exception):
    def __init__(self, message: str, loc: Optional[SourceLoc] = None):
        self.message = message
        self.loc = loc
        located = f"{message} ({loc.file}:{loc.line})" if loc else message
        super().__init__(located)


class ParseError(PfcfgError):
    """Malformed syntax, or a structural rule violated (e.g. an include
    directive appearing after a section header in the same file)."""


class WalkError(PfcfgError):
    """A structural problem only detectable while walking (e.g. a key
    assignment with no section ever established, an include target that
    does not exist on disk)."""
