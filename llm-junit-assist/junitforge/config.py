"""CLI + runtime configuration.

Upgraded to optimize iteration rounds and support seamless single-method incremental
test generation for modern Spring Boot 3.5 architectures.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GenConfig:
    """Per-run generation/verification tuning records."""
    model_id: str = "mistralai/mistral-medium-2505"
    temperature_gen: float = 0.0
    temperature_repair: float = 0.0
    max_tokens_gen: int = 8000
    max_tokens_aug: int = 8000
    max_tokens_repair: int = 8000
    max_gen_reasks: int = 2
    max_compile_repairs: int = 3
    # Expanded round ceilings to support comprehensive, self-healing method passes
    max_coverage_rounds: int = 5
    max_failure_repairs: int = 2
    overwrite: bool = True
    run_coverage: bool = False
    offline: bool = False
    java_release: str | None = "17"
    settings_file: Path | None = None
    local_repo: Path | None = None
    mvn_args: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    repo_path: Path
    report_dir: Path
    gen: GenConfig
    only: list[str] = field(default_factory=list)   # Restrict to FQCN/simple-name substrings
    limit: int = 0                                   # 0 = no limit
    verbose: bool = False
    dry_run: bool = False                            # Classify+plan only, no LLM/compile


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="junitforge",
        description="Enterprise coverage-driven JUnit 5 generator for Spring Boot 3.5 (watsonx + JaCoCo/javac).",
    )
    p.add_argument("--repo-path", required=True, help="Target repository root")
    p.add_argument("--report-dir", default="junitforge-reports",
                   help="Report output dir (relative to repo unless absolute)")
    p.add_argument("--model", default=os.getenv("WATSONX_MODEL_ID", "mistralai/mistral-medium-2505"))
    p.add_argument("--only", default="", help="Comma-separated class name/FQCN substrings to target")
    p.add_argument("--limit", type=int, default=0, help="Max number of classes to process (0=all)")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing test files")
    p.add_argument("--coverage", action="store_true",
                   help="Enable JaCoCo coverage loop after compile gate. Default is compile-gate only.")
    p.add_argument("--no-coverage", action="store_true",
                   help="Keep JaCoCo coverage disabled (default; compile-gate only)")
    p.add_argument("--offline", action="store_true", help="Prefer offline Maven (-o), online fallback")
    p.add_argument("--max-coverage-rounds", type=int, default=5)
    p.add_argument("--max-compile-repairs", type=int, default=3)
    p.add_argument("--mvn-args", default="",
                   help="Extra Maven args for strict-plugin repos, e.g. \"-Drat.skip=true -Denforcer.skip=true\"")
    p.add_argument("--settings", default="", help="Maven settings.xml")
    p.add_argument("--local-repo", default="", help="Maven local repo dir")
    p.add_argument("--dry-run", action="store_true", help="Plan + classify only; no LLM, no compile")
    p.add_argument("--verbose", action="store_true")
    return p


def load_config(argv: list[str] | None = None) -> AppConfig:
    ns = build_parser().parse_args(argv)
    repo = Path(ns.repo_path).expanduser().resolve()
    report_dir = Path(ns.report_dir)
    if not report_dir.is_absolute():
        report_dir = repo / report_dir
        
    gen = GenConfig(
        model_id=ns.model,
        overwrite=bool(ns.overwrite),
        run_coverage=bool(ns.coverage) and not bool(ns.no_coverage),
        offline=bool(ns.offline),
        max_coverage_rounds=ns.max_coverage_rounds,
        max_compile_repairs=ns.max_compile_repairs,
        settings_file=Path(ns.settings).expanduser().resolve() if ns.settings else None,
        local_repo=Path(ns.local_repo).expanduser().resolve() if ns.local_repo else None,
        mvn_args=ns.mvn_args.split() if ns.mvn_args else [],
    )
    return AppConfig(
        repo_path=repo,
        report_dir=report_dir,
        gen=gen,
        only=[s.strip() for s in ns.only.split(",") if s.strip()],
        limit=ns.limit,
        verbose=bool(ns.verbose),
        dry_run=bool(ns.dry_run),
    )