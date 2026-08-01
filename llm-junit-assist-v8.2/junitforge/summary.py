"""Human-readable run summary.

The raw log is three interleaved stderr streams (logging, timing events,
heartbeats) written by three concurrent workers.  That is useful when you are
watching a run live and useless afterwards.  This module renders one digestible
block per target, plus a final run block, from data already on TargetOutcome.

Nothing here changes generation behaviour.  It only reads.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_W = 78
_RULE = "=" * _W
_THIN = "-" * _W

# Note prefixes emitted by loop.py / assembly.py.
_RE_REJECT = re.compile(r"^method generation rejected \[(?P<mid>[^\]]+)\]: (?P<reason>.*)$")
_RE_ACCEPT = re.compile(r"^method generated \[(?P<mid>[^\]]+)\]: (?P<n>\d+) test")
_RE_ADVISORY = re.compile(r"^advisory \[(?P<mid>[^\]]+)\]: (?P<reason>.*)$")


def _fmt_secs(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _generalise(reason: str) -> str:
    """Collapse a specific reason to its class so counts are meaningful.

    'unobserved collaborator calls on giPolicyRepository: findByX (advisory...)'
    -> 'unobserved collaborator calls'
    """
    reason = reason.strip()
    for head in (
        "unobserved collaborator calls",
        "response calls a different CUT method",
        "response stubs or verifies the class under test",
        "response defines private helper methods",
        "no @Test methods returned",
        "method response contains package/import/class wrapper",
        "ServiceImpl tests do not call the selected CUT method",
        "unable to identify generated test method names",
        "duplicate test method names",
        "duplicate test names",
        "llm error",
    ):
        if reason.startswith(head):
            return head
    return reason.split(":", 1)[0][:60]


@dataclass
class PhaseTimings:
    """Wall-clock seconds per phase, accumulated by the caller."""

    analysis: float = 0.0
    llm: float = 0.0
    assembly: float = 0.0
    compile: float = 0.0
    coverage: float = 0.0
    total: float = 0.0
    llm_calls: int = 0
    compile_runs: int = 0

    def rows(self) -> list[tuple[str, float, int | None]]:
        return [
            ("analysis", self.analysis, None),
            ("llm", self.llm, self.llm_calls),
            ("assembly", self.assembly, None),
            ("compile", self.compile, self.compile_runs),
            ("coverage", self.coverage, None),
        ]


@dataclass
class TargetSummary:
    """One target's outcome, rendered from TargetOutcome.notes."""

    fqcn: str
    status: str
    classification: str = ""
    tokens: int = 0
    methods_total: int = 0
    methods_accepted: int = 0
    tests_generated: int = 0
    timings: PhaseTimings = field(default_factory=PhaseTimings)
    rejections: Counter = field(default_factory=Counter)
    advisories: Counter = field(default_factory=Counter)
    advisory_detail: list[str] = field(default_factory=list)
    compile_errors: list[str] = field(default_factory=list)

    @classmethod
    def from_outcome(cls, outcome, timings: PhaseTimings | None = None) -> "TargetSummary":
        s = cls(
            fqcn=outcome.fqcn,
            status=outcome.status,
            classification=getattr(outcome, "classification", "") or "",
            tokens=getattr(outcome, "tokens_used", 0) or 0,
            timings=timings or PhaseTimings(),
            compile_errors=list(getattr(outcome, "compile_errors", []) or []),
        )
        for note in getattr(outcome, "notes", []) or []:
            m = _RE_REJECT.match(note)
            if m:
                s.rejections[_generalise(m.group("reason"))] += 1
                s.methods_total += 1
                continue
            m = _RE_ACCEPT.match(note)
            if m:
                s.methods_accepted += 1
                s.methods_total += 1
                s.tests_generated += int(m.group("n"))
                continue
            m = _RE_ADVISORY.match(note)
            if m:
                s.advisories[_generalise(m.group("reason"))] += 1
                s.advisory_detail.append(f"{m.group('mid')}: {m.group('reason')}")
        return s

    def render(self) -> str:
        ok = self.status in {"ok", "passed", "generated"}
        badge = "OK" if ok else self.status.upper()
        rejected = self.methods_total - self.methods_accepted

        out = [_RULE, f" {self.fqcn}".ljust(_W - len(badge) - 1) + badge, _RULE]

        if self.classification:
            out.append(f" classification  {self.classification}")
        if self.methods_total:
            out.append(
                f" methods         {self.methods_total} total | "
                f"{self.methods_accepted} accepted | {rejected} rejected"
            )
        out.append(f" tests           {self.tests_generated} generated")
        out.append(f" tokens          {self.tokens:,}")
        out.append(f" wall clock      {_fmt_secs(self.timings.total)}")

        rows = [r for r in self.timings.rows() if r[1] > 0.05]
        if rows and self.timings.total > 0:
            out += ["", " TIME BREAKDOWN"]
            for name, secs, count in sorted(rows, key=lambda r: -r[1]):
                pct = 100.0 * secs / self.timings.total
                label = f"{name} ({count} calls)" if count else name
                out.append(f"   {label:<28} {_fmt_secs(secs):>8}  {pct:4.0f}%")

        if self.rejections:
            out += ["", f" REJECTIONS ({sum(self.rejections.values())})  [block dropped]"]
            for reason, n in self.rejections.most_common():
                out.append(f"   {n:>3}x  {reason}")

        if self.advisories:
            out += ["", f" ADVISORY ({sum(self.advisories.values())})  [block kept; javac arbitrates]"]
            for reason, n in self.advisories.most_common():
                out.append(f"   {n:>3}x  {reason}")
            # These are the measured analyzer gaps. Always show them in full.
            for detail in self.advisory_detail[:12]:
                out.append(f"        - {detail}")
            if len(self.advisory_detail) > 12:
                out.append(f"        ... {len(self.advisory_detail) - 12} more")

        if self.compile_errors:
            out += ["", f" COMPILE ERRORS ({len(self.compile_errors)})"]
            for err in self.compile_errors[:10]:
                out.append(f"   {err[:_W - 5]}")
            if len(self.compile_errors) > 10:
                out.append(f"   ... {len(self.compile_errors) - 10} more")

        out.append(_RULE)
        return "\n".join(out)


def render_run_summary(summaries: list[TargetSummary]) -> str:
    """Final block for the whole run."""
    if not summaries:
        return ""
    ok = [s for s in summaries if s.status in {"ok", "passed", "generated"}]
    failed = [s for s in summaries if s not in ok]
    tokens = sum(s.tokens for s in summaries)
    tests = sum(s.tests_generated for s in summaries)
    secs = sum(s.timings.total for s in summaries)

    out = ["", _RULE, " RUN SUMMARY".ljust(_W), _RULE]
    out.append(f" targets         {len(summaries)} | {len(ok)} ok | {len(failed)} failed")
    out.append(f" tests           {tests} generated")
    out.append(f" tokens          {tokens:,}")
    out.append(f" wall clock      {_fmt_secs(secs)}")

    if failed:
        out += ["", " FAILED TARGETS"]
        for s in failed:
            top = s.rejections.most_common(1)
            why = f" ({top[0][0]})" if top else ""
            out.append(f"   {s.fqcn.split('.')[-1]:<40} {s.status}{why}")

    agg: Counter = Counter()
    for s in summaries:
        agg.update(s.rejections)
    if agg:
        out += ["", " REJECTION REASONS (all targets)"]
        for reason, n in agg.most_common():
            out.append(f"   {n:>3}x  {reason}")

    adv: Counter = Counter()
    for s in summaries:
        adv.update(s.advisories)
    if adv:
        out += ["", " ADVISORY REASONS (all targets)  [analyzer extraction gaps]"]
        for reason, n in adv.most_common():
            out.append(f"   {n:>3}x  {reason}")

    out += [_RULE, ""]
    return "\n".join(out)
