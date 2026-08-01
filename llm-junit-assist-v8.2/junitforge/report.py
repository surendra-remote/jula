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
        "tests_executed": any(
            o.line_pct is not None
            or (
                bool(o.class_validation)
                and o.class_validation.get("initial_complete_class_execution_result") != "not_run"
            )
            for o in outcomes
        ),
        "summary": _summary(outcomes),
        "remediations": _remediations(outcomes),
        "targets": [asdict(o) for o in outcomes],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(payload, outcomes), encoding="utf-8")
    _write_class_validation_reports(report_dir, outcomes)
    return json_path, md_path


def _write_class_validation_reports(report_dir: Path, outcomes: list[TargetOutcome]) -> None:
    """Write one concise lifecycle report for every generated ServiceImpl class."""
    destination = report_dir / "class-validation"
    for outcome in outcomes:
        validation = outcome.class_validation
        if not validation:
            continue
        destination.mkdir(parents=True, exist_ok=True)
        target = str(validation.get("target_test_class") or outcome.fqcn)
        safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in target)
        (destination / f"{safe_name}.json").write_text(
            json.dumps(validation, indent=2),
            encoding="utf-8",
        )
        (destination / f"{safe_name}.md").write_text(
            _class_validation_markdown(validation),
            encoding="utf-8",
        )


def _class_validation_markdown(validation: dict[str, object]) -> str:
    lines = [
        f"# Class-level validation: {validation.get('target_test_class', '-')}",
        "",
        f"- Initial test count: {validation.get('initial_test_count', 0)}",
        f"- Expected primary method count: {validation.get('expected_primary_method_count', 0)}",
        "- All primary tests present before compilation: "
        f"{validation.get('all_primary_tests_generated_before_compilation', False)}",
        f"- Initial compilation: {validation.get('initial_compilation_result', '-')}",
        f"- Compilation repair rounds: {validation.get('compilation_repair_rounds_attempted', 0)}",
        f"- Final compilation: {validation.get('final_compilation_result', '-')}",
        f"- Initial complete-class execution: {validation.get('initial_complete_class_execution_result', 'not_run')}",
        f"- Execution repair rounds: {validation.get('execution_repair_rounds_attempted', 0)}",
        f"- Final complete-class execution: {validation.get('final_complete_class_execution_result', 'not_run')}",
        f"- Publication: {validation.get('publication_result', '-')}",
        f"- Publication reason: {validation.get('publication_reason', '-')}",
        "",
        "## Initial compilation evidence",
        "",
    ]
    diagnostics = list(validation.get("compilation_diagnostics_found", []) or [])
    if diagnostics:
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            location = f"{diagnostic.get('file')}:{diagnostic.get('line')}"
            if diagnostic.get("column") is not None:
                location += f":{diagnostic.get('column')}"
            lines.append(
                f"- `{diagnostic.get('scope_id') or 'unmapped'}` at `{location}`: "
                f"{diagnostic.get('message')} — `{diagnostic.get('offending_statement')}`"
            )
    else:
        lines.append("- No compiler diagnostics.")

    lines.extend(["", "## Compilation repair history", ""])
    compile_history = list(validation.get("compilation_repair_history", []) or [])
    if not compile_history:
        lines.append("- No compilation repair was required.")
    for round_record in compile_history:
        if not isinstance(round_record, dict):
            continue
        applied = [
            str(repair.get("scope_id"))
            for repair in round_record.get("repairs", [])
            if isinstance(repair, dict) and repair.get("applied")
        ]
        lines.append(
            f"- Round {round_record.get('round')}: repaired "
            f"{', '.join(applied) if applied else 'no scope'}; "
            f"resolved {len(round_record.get('resolved_diagnostics', []) or [])}; "
            f"unresolved {len(round_record.get('unresolved_diagnostics', []) or [])}."
        )
        rejected = [
            f"{repair.get('scope_id') or 'unmapped'}: {repair.get('reason')}"
            for repair in round_record.get("repairs", [])
            if isinstance(repair, dict) and not repair.get("applied") and repair.get("reason")
        ]
        for item in rejected:
            lines.append(f"  - Not applied: {item}")
        if round_record.get("stop_reason"):
            lines.append(f"  - Stop reason: {round_record.get('stop_reason')}")

    lines.extend(["", "## Complete-class execution", ""])
    passing = list(validation.get("passing_test_methods", []) or [])
    failed = list(validation.get("failed_or_erroneous_test_methods", []) or [])
    lines.append("- Passing: " + (", ".join(map(str, passing)) if passing else "none"))
    lines.append("- Failed/erroneous/skipped: " + (", ".join(map(str, failed)) if failed else "none"))

    lines.extend(["", "## Execution repair history", ""])
    execution_history = list(validation.get("execution_repair_history", []) or [])
    if not execution_history:
        lines.append("- No execution repair was required.")
    for round_record in execution_history:
        if not isinstance(round_record, dict):
            continue
        applied = [
            str(repair.get("scope_id"))
            for repair in round_record.get("repairs", [])
            if isinstance(repair, dict) and repair.get("applied")
        ]
        lines.append(
            f"- Round {round_record.get('round')}: repaired "
            f"{', '.join(applied) if applied else 'no scope'}; "
            f"resolved {len(round_record.get('resolved_diagnostics', []) or [])}; "
            f"unresolved {len(round_record.get('latest_unresolved', []) or [])}."
        )
        rejected = [
            f"{repair.get('scope_id') or 'unmapped'}: {repair.get('reason')}"
            for repair in round_record.get("repairs", [])
            if isinstance(repair, dict) and not repair.get("applied") and repair.get("reason")
        ]
        for item in rejected:
            lines.append(f"  - Not applied: {item}")
        if round_record.get("compilation_corrections"):
            lines.append("  - Compilation errors introduced by this round were handled within the same round.")
        if round_record.get("stop_reason"):
            lines.append(f"  - Stop reason: {round_record.get('stop_reason')}")

    lines.extend(["", "## Unresolved issues", ""])
    unresolved = list(validation.get("unresolved_issues", []) or [])
    if not unresolved:
        lines.append("- None.")
    for issue in unresolved:
        if not isinstance(issue, dict):
            continue
        lines.append(
            f"- `{issue.get('scope', 'unmapped')}`: {issue.get('diagnostic', '-')}; "
            f"repair attempts={issue.get('repair_attempts', 0)}; "
            f"final reason={issue.get('final_reason', validation.get('publication_reason', '-'))}."
        )
    return "\n".join(lines) + "\n"


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

    failed = [o for o in outcomes if o.status in ("compile_failed", "generation_failed", "test_failed")]
    if failed:
        lines += ["", "## Failed", ""]
        for o in failed:
            lines.append(f"- **{o.fqcn}** ({o.status})")
            for e in o.compile_errors[:6]:
                lines.append(f"  - {e}")
    return "\n".join(lines) + "\n"
