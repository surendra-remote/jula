"""Lazy, cached, source-context-aware Java symbol resolution.

A cheap source-path index is built at startup. Every physical Java source file is
parsed at most once per CLI run. Resolution prefers exact FQCN/import/package
matches and never silently selects the first duplicate simple name.
"""
from __future__ import annotations

import re
from pathlib import Path
from threading import RLock
from time import perf_counter
from typing import Iterable

from junitforge.models import ClassSymbol
from junitforge.parser.java_symbols import parse_file
from junitforge.timing import event as timing_event
from junitforge.vendor.logging_utils import get

log = get(__name__)
_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")


class LazySymbolResolver:
    def __init__(self, sources: Iterable[Path]):
        started = perf_counter()
        self._lock = RLock()
        self._by_simple: dict[str, list[Path]] = {}
        self._by_fqcn: dict[str, Path] = {}
        self._file_cache: dict[Path, object] = {}
        self._symbol_cache: dict[str, ClassSymbol] = {}
        self._missing_cache: set[str] = set()
        self._ambiguous_cache: set[str] = set()
        self._parse_counts: dict[Path, int] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        source_list = list(sources)
        for path in source_list:
            self._by_simple.setdefault(path.stem, []).append(path)
            try:
                head = path.read_text(encoding="utf-8", errors="ignore")[:8192]
                match = _PACKAGE_RE.search(head)
                if match:
                    self._by_fqcn.setdefault(f"{match.group(1)}.{path.stem}", path)
            except OSError:
                continue
        elapsed = perf_counter() - started
        log.info(
            "[TIMING] lazy source-path index: %.3fs | files=%d | simpleNames=%d | fqcn=%d",
            elapsed, len(source_list), len(self._by_simple), len(self._by_fqcn),
        )
        timing_event(
            "symbol.index.end",
            elapsed=elapsed,
            files=len(source_list),
            simple_names=len(self._by_simple),
            fqcn=len(self._by_fqcn),
        )

    def parse_target(self, path: Path):
        return self._parse_and_cache(path, reason="target")

    def lookup(self, type_name: str) -> ClassSymbol | None:
        return self.lookup_context(type_name)

    def lookup_context(
        self,
        type_name: str,
        owner: ClassSymbol | None = None,
        *,
        owner_package: str | None = None,
        owner_imports: Iterable[str] | None = None,
        owner_fqcn: str | None = None,
    ) -> ClassSymbol | None:
        """Resolve a type using the source context that declared the reference."""
        normalized = self._normalize(type_name)
        if not normalized:
            return None
        simple = normalized.rsplit(".", 1)[-1]
        package = owner_package or (owner.package_name if owner else None)
        imports = list(owner_imports if owner_imports is not None else (owner.imports if owner else ()))
        enclosing = owner_fqcn or (owner.fqcn if owner else None)

        candidate_names: list[str] = []
        if "." in normalized:
            candidate_names.append(normalized)
        if enclosing:
            candidate_names.extend((f"{enclosing}.{simple}",))
            if owner and owner.enclosing_fqcn:
                candidate_names.append(f"{owner.enclosing_fqcn}.{simple}")
        for imp in imports:
            imp = (imp or "").strip()
            if imp.startswith("static "):
                imp = imp[7:].strip()
            if imp.endswith(".*"):
                candidate_names.append(f"{imp[:-2]}.{simple}")
            elif imp.rsplit(".", 1)[-1] == simple:
                candidate_names.append(imp)
            elif imp.endswith(f".{simple}"):
                candidate_names.append(imp)
        if package:
            candidate_names.append(f"{package}.{simple}")
        candidate_names.append(normalized)
        candidate_names = list(dict.fromkeys(candidate_names))

        with self._lock:
            for name in candidate_names:
                cached = self._symbol_cache.get(name)
                if cached is not None:
                    self._cache_hits += 1
                    return cached

        # Parse the strongest exact source candidate first.
        for name in candidate_names:
            path = self._path_for_fqcn_or_nested(name)
            if path is None:
                continue
            self._parse_and_cache(path, reason=f"lookup:{name}")
            with self._lock:
                found = self._symbol_cache.get(name)
                if found is not None:
                    return found
                # An explicit top-level import may identify a file whose nested
                # symbol uses Outer.Inner as its FQCN. Match that exact suffix.
                nested = [s for key, s in self._symbol_cache.items() if key.endswith(f".{name}")]
                if len({s.fqcn for s in nested}) == 1:
                    return nested[0]

        with self._lock:
            cached_simple = [s for key, s in self._symbol_cache.items() if key == simple]
            if len({s.fqcn for s in cached_simple}) == 1 and cached_simple:
                self._cache_hits += 1
                return cached_simple[0]
            paths = list(dict.fromkeys(self._by_simple.get(simple, [])))

        # Only a unique repository-wide simple name is safe as a fallback.
        if len(paths) == 1:
            self._parse_and_cache(paths[0], reason=f"lookup-unique:{simple}")
            with self._lock:
                exact = self._symbol_cache.get(normalized)
                if exact is not None:
                    return exact
                matches = self._symbols_by_simple(simple)
                if len(matches) == 1:
                    return matches[0]

        with self._lock:
            self._cache_misses += 1
            if len(paths) > 1:
                self._ambiguous_cache.add(normalized)
                timing_event("symbol.lookup.ambiguous", type=normalized, candidates=len(paths))
            else:
                self._missing_cache.add(normalized)
                timing_event("symbol.lookup.missing", type=normalized)
        return None

    def _path_for_fqcn_or_nested(self, fqcn: str) -> Path | None:
        with self._lock:
            direct = self._by_fqcn.get(fqcn)
            if direct is not None:
                return direct
            # Nested FQCNs map to the top-level source file. Walk prefixes from
            # longest to shortest until a known top-level class is found.
            parts = fqcn.split(".")
            for cut in range(len(parts) - 1, 0, -1):
                prefix = ".".join(parts[:cut])
                path = self._by_fqcn.get(prefix)
                if path is not None:
                    return path
        return None

    def _symbols_by_simple(self, simple: str) -> list[ClassSymbol]:
        seen: dict[str, ClassSymbol] = {}
        for symbol in self._symbol_cache.values():
            if symbol.name == simple:
                seen[symbol.fqcn] = symbol
        return list(seen.values())

    def _index_symbol(self, symbol: ClassSymbol) -> None:
        if symbol.fqcn:
            self._symbol_cache.setdefault(symbol.fqcn, symbol)
        # A simple-name alias is added only while unambiguous. If another class
        # with the same simple name appears, remove the unsafe alias.
        existing = self._symbol_cache.get(symbol.name)
        if existing is None:
            self._symbol_cache[symbol.name] = symbol
        elif existing.fqcn != symbol.fqcn:
            self._symbol_cache.pop(symbol.name, None)
        for nested in symbol.nested or []:
            self._index_symbol(nested)

    def _parse_and_cache(self, path: Path, *, reason: str):
        resolved = path.resolve()
        with self._lock:
            cached = self._file_cache.get(resolved)
            if cached is not None:
                self._cache_hits += 1
                log.debug("[TIMING] JavaParser cache hit: %s", path.name)
                return cached

            count = self._parse_counts.get(resolved, 0) + 1
            self._parse_counts[resolved] = count
            if count > 1:
                raise AssertionError(f"Duplicate JavaParser invocation prevented: {resolved}")

            started = perf_counter()
            log.info("[TIMING] JavaParser start | reason=%s | file=%s", reason, path)
            timing_event("symbol.parse.start", reason=reason, file=path)
            parsed = parse_file(path)
            elapsed = perf_counter() - started
            self._file_cache[resolved] = parsed
            for symbol in parsed.types:
                self._index_symbol(symbol)
            log.info(
                "[TIMING] JavaParser end   | reason=%s | file=%s | %.3fs | types=%d",
                reason, path.name, elapsed, len(parsed.types),
            )
            timing_event(
                "symbol.parse.end",
                elapsed=elapsed,
                reason=reason,
                file=path.name,
                types=len(parsed.types),
                parsed_files=len(self._file_cache),
                cache_hits=self._cache_hits,
                cache_misses=self._cache_misses,
            )
            return parsed

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "parsed_files": len(self._file_cache),
                "cached_symbols": len({s.fqcn for s in self._symbol_cache.values()}),
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "missing_types": len(self._missing_cache),
                "ambiguous_types": len(self._ambiguous_cache),
                "duplicate_parse_attempts": sum(max(0, count - 1) for count in self._parse_counts.values()),
            }

    @staticmethod
    def _normalize(type_name: str) -> str:
        value = (type_name or "").strip()
        previous = None
        while previous != value:
            previous = value
            value = re.sub(r"<[^<>]*>", "", value)
        value = value.replace("[]", "").replace("...", "").strip()
        value = re.sub(r"^\?\s*(?:extends|super)\s+", "", value)
        return value
