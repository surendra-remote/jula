"""Lazy, cached Java symbol resolution.

A cheap path index is built at startup. Every physical Java source file is
parsed at most once per CLI run. Repeated repository/service/DTO lookups return
from memory and never relaunch ``javaparser-cli.jar``.
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
        normalized = self._normalize(type_name)
        if not normalized:
            return None
        simple = normalized.rsplit(".", 1)[-1]
        with self._lock:
            cached = self._symbol_cache.get(normalized) or self._symbol_cache.get(simple)
            if cached is not None:
                self._cache_hits += 1
                log.debug("[TIMING] symbol cache hit: %s", normalized)
                return cached
            if normalized in self._missing_cache or simple in self._missing_cache:
                self._cache_hits += 1
                return None
            path = self._by_fqcn.get(normalized)
            if path is None:
                candidates = self._by_simple.get(simple, [])
                path = candidates[0] if candidates else None
            if path is None:
                self._missing_cache.add(normalized)
                self._missing_cache.add(simple)
                self._cache_misses += 1
                timing_event("symbol.lookup.missing", type=normalized)
                return None
            self._cache_misses += 1

        self._parse_and_cache(path, reason=f"lookup:{normalized}")
        with self._lock:
            return self._symbol_cache.get(normalized) or self._symbol_cache.get(simple)

    def _parse_and_cache(self, path: Path, *, reason: str):
        resolved = path.resolve()
        # Parsing is intentionally protected by the resolver lock. It is a small
        # one-time startup cost and guarantees one physical file -> one parser
        # invocation even if concurrent generation requests the same type.
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
                self._symbol_cache.setdefault(symbol.name, symbol)
                if symbol.fqcn:
                    self._symbol_cache.setdefault(symbol.fqcn, symbol)
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
                "cached_symbols": len(self._symbol_cache),
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "missing_types": len(self._missing_cache),
                "duplicate_parse_attempts": sum(max(0, count - 1) for count in self._parse_counts.values()),
            }

    @staticmethod
    def _normalize(type_name: str) -> str:
        value = (type_name or "").strip()
        # Remove nested generic declarations without allowing the cache key to
        # vary between Foo, Foo<Bar>, Foo[] and wildcard renderings.
        previous = None
        while previous != value:
            previous = value
            value = re.sub(r"<[^<>]*>", "", value)
        value = value.replace("[]", "").strip()
        value = re.sub(r"^\?\s*(?:extends|super)\s+", "", value)
        return value
