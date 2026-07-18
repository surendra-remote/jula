"""junitforge CLI entry point (Maven and Ant/Eclipse backends)."""

from __future__ import annotations

import json
import sys
from time import perf_counter
from junitforge.timing import configure_timing, event as timing_event
from pathlib import Path

from junitforge.backends.ant import (
    AntCompileGate,
    AntCoverageRunner,
    ant_source_files,
    ant_test_path,
    detect_release as ant_detect_release,
    discover_ant_modules,
    harvest_jars,
    is_ant_repo,
)
from junitforge.compile_gate import CompileGate
from junitforge.config import AppConfig, load_config
from junitforge.coverage_run import CoverageRunner
from junitforge.discovery import discover_modules, is_test_target, iter_source_files, test_path_for
from junitforge.loop import Engine
from junitforge.execution.config_values import load_application_values
from junitforge.parser.java_symbols import parse_file
from junitforge.parser.lazy_symbol_resolver import LazySymbolResolver
from junitforge.report import write_reports
from junitforge.stack.classifier import classify
from junitforge.stack.classpath_profile import from_classpath_string, from_pom_artifacts
from junitforge.stack.detect import detect_stack
from junitforge.toolchain import ensure_toolchain
from junitforge.vendor.logging_utils import configure, get

log = get("junitforge")


def _load_env(repo: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()
    load_dotenv(repo / ".env")


def _norm_filter_text(value: str) -> str:
    return value.replace("\\", "/").strip().strip('"').strip("'").lower()


def _filter(cfg: AppConfig, all_sources: list[Path]) -> list[Path]:
    targets = all_sources
    if cfg.only:
        needles = [_norm_filter_text(o) for o in cfg.only if str(o).strip()]
        targets = [
            s for s in targets
            if any(
                needle in _norm_filter_text(str(s))
                or needle in s.stem.lower()
                for needle in needles
            )
        ]
    if cfg.limit:
        targets = targets[: cfg.limit]
    return targets


def _symbol_index(sources: list[Path]) -> dict:
    index: dict = {}
    for s in sources:
        for t in parse_file(s).types:
            index.setdefault(t.name, t)
    return index


def run(cfg: AppConfig) -> int:
    configure("debug" if cfg.verbose else "info")
    repo = cfg.repo_path
    configure_timing(repo)
    timing_event("cli.run.start", repo=repo, verbose=cfg.verbose)
    _load_env(repo)

    maven = (repo / "pom.xml").exists()
    ant = is_ant_repo(repo)
    if not maven and not ant:
        log.error("No pom.xml or build.xml at %s (Maven or Ant/Eclipse projects only)", repo)
        return 2

    stack = detect_stack(repo)
    log.info("Stack: Boot %s / Spring %s / Java %s (jakarta=%s, @MockitoBean=%s)",
             stack.boot_major, stack.spring_major, stack.java_version, stack.jakarta, stack.has_mockitobean)

    if ant:
        cfg.gen.java_release = cfg.gen.java_release or ant_detect_release(repo) or stack.java_version
        modules = discover_ant_modules(repo)
        all_sources = ant_source_files(repo)
        test_path_fn = ant_test_path
    else:
        cfg.gen.java_release = cfg.gen.java_release or stack.java_version
        modules = discover_modules(repo)
        all_sources = [s for m in modules for s in iter_source_files(m)]
        test_path_fn = test_path_for
    log.info("Backend: %s | %d module(s)", "ant" if ant else "maven", len(modules))

    targets = _filter(cfg, all_sources)
    log.info("Selected %d source class(es)", len(targets))

    if cfg.dry_run:
        return _dry_run(cfg, stack, repo, targets, ant)

    from junitforge.vendor.llm.watsonx import WatsonxClient, WatsonxConfig
    try:
        wcfg = WatsonxConfig.from_env(model_id=cfg.gen.model_id)
    except KeyError as exc:
        log.error("Missing watsonx env var: %s (see .env.example)", exc)
        return 2
    llm = WatsonxClient(wcfg)

    if ant:
        tc = ensure_toolchain()
        if not tc.ok:
            log.warning("Ant toolchain unavailable: %s", tc.note)
        gate = AntCompileGate(repo_root=repo, cache_dir=repo / ".junitforge", toolchain=tc,
                              java_release=cfg.gen.java_release)
        log.info("Preparing Ant compile gate (javac main sources + classpath)...")
        timing_event("compile_gate.setup.start", backend="ant", modules=len(modules))
        _t = perf_counter()
        gate.setup(modules)
        _elapsed = perf_counter() - _t
        log.info("[TIMING] compile gate setup: %.3fs", _elapsed)
        timing_event("compile_gate.setup.end", elapsed=_elapsed, backend="ant")
        coverage_runner = (AntCoverageRunner(repo_root=repo, cache_dir=repo / ".junitforge",
                                             toolchain=tc, gate=gate)
                           if cfg.gen.run_coverage else None)
    else:
        gate = CompileGate(repo_root=repo, cache_dir=repo / ".junitforge",
                           java_release=cfg.gen.java_release, offline=cfg.gen.offline,
                           settings_file=cfg.gen.settings_file, local_repo=cfg.gen.local_repo,
                           mvn_args=cfg.gen.mvn_args)
        log.info("Preparing compile gate (reactor test-compile + classpath)...")
        timing_event("compile_gate.setup.start", backend="maven", modules=len(modules))
        _t = perf_counter()
        gate.setup(modules)
        _elapsed = perf_counter() - _t
        log.info("[TIMING] compile gate setup: %.3fs", _elapsed)
        timing_event("compile_gate.setup.end", elapsed=_elapsed, backend="maven")
        coverage_runner = None
        if cfg.gen.run_coverage:
            coverage_runner = CoverageRunner(repo_root=repo, offline=cfg.gen.offline,
                                             settings_file=cfg.gen.settings_file, local_repo=cfg.gen.local_repo,
                                             mvn_args=cfg.gen.mvn_args)
            if coverage_runner.available():
                _t = perf_counter()
                coverage_runner.warm_cache()
                _elapsed = perf_counter() - _t
                log.info("[TIMING] coverage warm cache: %.3fs", _elapsed)
                timing_event("coverage.warm_cache.end", elapsed=_elapsed)

    if not gate.ready:
        log.warning("Compile gate unavailable: %s - tests generated but NOT verified", gate.setup_note)

    from junitforge.kb import KbIndex
    _t = perf_counter()
    resolver = LazySymbolResolver(all_sources)
    _elapsed = perf_counter() - _t
    log.info("[TIMING] lazy resolver initialization: %.3fs", _elapsed)
    timing_event("lazy_resolver.init.end", elapsed=_elapsed, source_files=len(all_sources))
    configuration_values = load_application_values(repo)
    _t = perf_counter()
    kb = KbIndex.load()
    _elapsed = perf_counter() - _t
    log.info("[TIMING] knowledge-base load: %.3fs", _elapsed)
    timing_event("knowledge_base.load.end", elapsed=_elapsed)
    engine = Engine(
        llm=llm, compile_gate=gate, coverage_runner=coverage_runner, stack=stack,
        modules=modules, repo_root=repo, lookup=resolver.lookup, cfg=cfg.gen,
        kb=kb, test_path_fn=test_path_fn, symbol_resolver=resolver,
        configuration_values=configuration_values)

    outcomes = []
    for i, src in enumerate(targets, 1):
        log.info("[%d/%d] %s", i, len(targets), src.name)
        timing_event("target.start", index=i, total=len(targets), file=src)
        _target_t = perf_counter()
        outcome = engine.run_target(src)
        _elapsed = perf_counter() - _target_t
        log.info("[TIMING] target total: %.3fs | %s", _elapsed, src.name)
        timing_event("target.end", elapsed=_elapsed, file=src.name, status=outcome.status)
        if outcome.status == "compile_failed":
            log.error("    -> compile_failed: %s", outcome.fqcn)
            for err in outcome.compile_errors[:12]:
                log.error("       %s", err.replace("\n", "\n       "))
            if not outcome.compile_errors:
                log.error("       <no compiler errors captured; check notes/report>")
            for note in outcome.notes[-5:]:
                log.error("       note: %s", note)
        elif outcome.status in {"generation_failed", "skipped", "integration_only"}:
            note = f" - {outcome.notes[-1]}" if outcome.notes else ""
            log.warning("    -> %s%s", outcome.status, note)
        else:
            log.info("    -> %s%s", outcome.status,
                     f" (line {outcome.line_pct}%/branch {outcome.branch_pct}%)"
                     if outcome.line_pct is not None else "")
        outcomes.append(outcome)

    json_path, md_path = write_reports(cfg.report_dir, repo, stack, outcomes)
    log.info("Reports: %s | %s", json_path, md_path)
    print(json.dumps({
        "backend": "ant" if ant else "maven",
        "targets": len(outcomes),
        "covered": sum(1 for o in outcomes if o.status == "covered"),
        "below_threshold": sum(1 for o in outcomes if o.status == "below_threshold"),
        "integration_only": sum(1 for o in outcomes if o.status == "integration_only"),
        "compile_failed": sum(1 for o in outcomes if o.status == "compile_failed"),
        "skipped": sum(1 for o in outcomes if o.status == "skipped"),
        "json_report": str(json_path),
        "md_report": str(md_path),
    }, indent=2))
    return 0


def _dry_run(cfg, stack, repo, targets, ant) -> int:
    cp = (from_classpath_string("\n".join(harvest_jars(repo)), boot_major=stack.boot_major)
          if ant else None)
    rows = []
    for src in targets:
        jf = parse_file(src)
        t = jf.primary_type
        ok, why = is_test_target(jf)
        if not ok or t is None:
            rows.append((src.name, "-", f"skip: {why}"))
            continue
        prof = cp if ant else from_pom_artifacts(repo, src.parents[0], boot_major=stack.boot_major)
        spec = classify(t, stack, prof)
        rows.append((t.fqcn, spec.kind.value,
                     f"testable={spec.testable} targets={spec.line_target:.0f}/{spec.branch_target:.0f}"))
    for name, kind, info in rows:
        print(f"{name:55s} {kind:16s} {info}")
    print(f"\n{len(rows)} classes")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(load_config(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
