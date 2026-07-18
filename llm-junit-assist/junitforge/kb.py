"""Lean standalone KB: error-pattern -> fix-hint lookup for the compile-repair loop.

Loads junitforge's own copy of KB entries targeting Spring Boot 3.5 and Java 17 
by default to prevent version-mismatch compilation failures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_KB_ROOT = Path(__file__).parent / "kb"


@dataclass
class KbEntry:
    """DTO wrapping individual self-healing rule blocks."""
    id: str
    title: str
    fix: str
    patterns: list[re.Pattern] = field(default_factory=list)


@dataclass
class KbIndex:
    """The master index manager processing diagnostic pattern matching."""
    entries: list[KbEntry] = field(default_factory=list)

    @classmethod
    # CRITICAL FIX: Shifted default dictionary target from 'junit-boot4-tests' to 'junit-boot3-tests'
    # This guarantees your self-healing loop pulls context matching your Spring Boot 3.5 / Java 17 stack
    def load(cls, path_id: str = "junit-boot3-tests") -> "KbIndex":
        """Loads configuration entries from the active system schema directory."""
        entries: list[KbEntry] = []
        edir = _KB_ROOT / path_id / "entries"
        
        if edir.exists():
            for f in sorted(edir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                pats = [re.compile(p, re.IGNORECASE)
                        for p in (data.get("triggers", {}).get("errors", []) or [])]
                entries.append(KbEntry(
                    id=data.get("id", f.stem), title=data.get("title", f.stem),
                    fix=(data.get("fix") or "").strip(), patterns=pats))
                    
        return cls(entries=entries)

    def lookup_by_errors(self, errors: list[str]) -> list[KbEntry]:
        """Scans compiler log strings to extract matching fix metadata records."""
        blob = "\n".join(errors)
        hits: list[KbEntry] = []
        for e in self.entries:
            if any(p.search(blob) for p in e.patterns):
                hits.append(e)
        return hits

    def hints_for(self, errors: list[str]) -> list[str]:
        """Formats matching errors into clean textual prompt strings."""
        return [f"{e.title}: {e.fix}" for e in self.lookup_by_errors(errors)]
