"""Honest JSON + Markdown reporting.

Coverage numbers come only from parsed JaCoCo. Integration-only / unmeasured
classes are reported as such with reasons and exact uncovered lines - never a
fabricated percentage.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from junitforge.models import StackProfile, TargetOutcome


def write_reports(report_dir: Path, repo_path: Path, stack: StackProfile,
                  outcomes: list[TargetOutcome], timestamp: str | None = None) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = report_dir / f"report_{ts}.json"
    md_path = report_dir / f"report_{ts}.md"

    payload = {
        "timestamp_utc": ts,
        "repo_path": str(repo_path),
        "stack": asdict(stack),
        "tests_executed": any(o.line_pct is not None for o in outcomes),
        "summary": _summary(outcomes),
        "remediations": _remediations(outcomes),
        "targets": [asdict(o) for o in outcomes],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(payload, outcomes), encoding="utf-8")
    return json_path, md_path


def _summary(outcomes: list[TargetOutcome]) -> dict:
    by_status: dict[str, int] = {}
    for o in outcomes:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    measured = [o for o in outcomes if o.line_pct is not None]
    avg_line = round(sum(o.line_pct or 0 for o in measured) / len(measured), 1) if measured else None
    avg_branch = round(sum(o.branch_pct or 0 for o in measured) / len(measured), 1) if measured else None
    return {
        "total": len(outcomes),
        "by_status": by_status,
        "measured": len(measured),
        "avg_line_pct": avg_line,
        "avg_branch_pct": avg_branch,
        "tokens_used": sum(o.tokens_used for o in outcomes),
    }


# Detectable "missing test dependency" signals -> maven coordinate to add.
_DEP_SIGNALS = {
    "reactor-test": "io.projectreactor:reactor-test",
    "spring-boot-starter-test": "org.springframework.boot:spring-boot-starter-test",
    "spring-boot-webmvc-test": "org.springframework.boot:spring-boot-webmvc-test",
    "spring-boot-webflux-test": "org.springframework.boot:spring-boot-webflux-test",
}


def _remediations(outcomes: list[TargetOutcome]) -> dict[str, dict[str, list[str]]]:
    """dep coordinate -> {module -> [fqcns]} aggregated from outcome notes."""
    out: dict[str, dict[str, list[str]]] = {}
    for o in outcomes:
        blob = " ".join(o.notes).lower()
        for signal, coord in _DEP_SIGNALS.items():
            if signal in blob:
                out.setdefault(coord, {}).setdefault(o.module, []).append(o.fqcn)
    return out


def _markdown(payload: dict, outcomes: list[TargetOutcome]) -> str:
    s = payload["summary"]
    st = payload["stack"]
    lines = [
        "# junitforge report",
        "",
        f"- Timestamp (UTC): {payload['timestamp_utc']}",
        f"- Repository: {payload['repo_path']}",
        f"- Stack: Spring Boot {st.get('boot_major')} / Spring {st.get('spring_major')} / "
        f"Java {st.get('java_version')} (jakarta={st.get('jakarta')}, "
        f"@MockitoBean={st.get('has_mockitobean')})",
        f"- Tests executed: {payload['tests_executed']}",
        f"- Targets: {s['total']} | measured: {s['measured']} | "
        f"avg line: {s['avg_line_pct']}% | avg branch: {s['avg_branch_pct']}%",
        f"- Status: {', '.join(f'{k}={v}' for k, v in s['by_status'].items())}",
        f"- Tokens used: {s['tokens_used']:,}",
        "",
        "## Targets",
        "",
        "| Class | Kind | Status | Line% | Branch% | Rounds | Test |",
        "|---|---|---|---|---|---|---|",
    ]
    for o in sorted(outcomes, key=lambda x: (x.module, x.fqcn)):
        line = "-" if o.line_pct is None else f"{o.line_pct:.0f}"
        branch = "-" if o.branch_pct is None else f"{o.branch_pct:.0f}"
        test = Path(o.test_path).name if o.test_path else "-"
        lines.append(f"| {o.fqcn} | {o.classification} | {o.status} | {line} | {branch} | {o.rounds} | {test} |")

    # Honest gaps: list exact uncovered lines for below-threshold classes.
    gaps = [o for o in outcomes if o.uncovered_lines]
    if gaps:
        lines += ["", "## Uncovered code (exact lines from JaCoCo)", ""]
        for o in gaps:
            ul = ", ".join(map(str, o.uncovered_lines[:40]))
            extra = " ..." if len(o.uncovered_lines) > 40 else ""
            lines.append(f"- **{o.fqcn}** ({o.line_pct:.0f}% line): lines {ul}{extra}")
            if o.uncovered_branches:
                ub = ", ".join(map(str, o.uncovered_branches[:30]))
                lines.append(f"  - partial/uncovered branches at lines: {ub}")

    integ = [o for o in outcomes if o.status == "integration_only"]
    if integ:
        lines += ["", "## Integration-only (not unit-generated)", ""]
        for o in integ:
            lines.append(f"- **{o.fqcn}**: {'; '.join(o.notes) or 'integration test recommended'}")

    rem = _remediations(outcomes)
    if rem:
        lines += ["", "## Suggested remediations (would unlock more unit coverage)", ""]
        for dep, info in rem.items():
            for module, classes in info.items():
                lines.append(f"- Add `{dep}` (test scope) to module **{module}** "
                             f"→ enables unit tests for {len(classes)} class(es): "
                             + ", ".join(sorted(c.rsplit('.', 1)[-1] for c in classes)))

    failed = [o for o in outcomes if o.status in ("compile_failed", "generation_failed")]
    if failed:
        lines += ["", "## Failed", ""]
        for o in failed:
            lines.append(f"- **{o.fqcn}** ({o.status})")
            for e in o.compile_errors[:6]:
                lines.append(f"  - {e}")
    return "\n".join(lines) + "\n"
