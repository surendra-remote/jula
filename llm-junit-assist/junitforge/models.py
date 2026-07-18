"""Central typed models for junitforge.

Symbol table, stack/classpath profiles, classification/templates, generation
context, coverage, compile errors, post-processing, and per-target outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# ---------------------------------------------------------------------------
# Symbol table
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MethodSig:
    name: str
    modifiers: set[str] = field(default_factory=set)
    return_type: str | None = None
    params: list[tuple[str, str]] = field(default_factory=list)
    throws: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    line: int | None = None
    end_line: int | None = None
    is_constructor: bool = False
    body_facts: dict[str, object] = field(default_factory=dict)

    @property
    def is_public(self) -> bool:
        return "public" in self.modifiers

    @property
    def is_static(self) -> bool:
        return "static" in self.modifiers

    @property
    def is_abstract(self) -> bool:
        return "abstract" in self.modifiers

    @property
    def arity(self) -> int:
        return len(self.params)

    def render(self) -> str:
        mods = " ".join(
            m for m in ("public", "protected", "private", "static", "abstract", "final")
            if m in self.modifiers
        )
        ret = (self.return_type + " ") if self.return_type else ("" if self.is_constructor else "void ")
        ps = ", ".join(f"{t} {n}".strip() for t, n in self.params)
        thr = (" throws " + ", ".join(self.throws)) if self.throws else ""
        prefix = (mods + " ") if mods else ""
        return f"{prefix}{ret}{self.name}({ps}){thr}".strip()


@dataclass(slots=True)
class FieldSig:
    name: str
    type: str
    modifiers: set[str] = field(default_factory=set)
    annotations: list[str] = field(default_factory=list)
    annotation_exprs: list[str] = field(default_factory=list)
    line: int | None = None


@dataclass(slots=True)
class ClassSymbol:
    name: str
    fqcn: str
    kind: str
    modifiers: set[str] = field(default_factory=set)
    extends: str | None = None
    implements: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    fields: list[FieldSig] = field(default_factory=list)
    constructors: list[MethodSig] = field(default_factory=list)
    methods: list[MethodSig] = field(default_factory=list)
    nested: list["ClassSymbol"] = field(default_factory=list)
    enum_constants: list[str] = field(default_factory=list)
    line: int | None = None
    end_line: int | None = None

    def all_method_names(self) -> set[str]:
        names = {m.name for m in self.methods}
        for n in self.nested:
            names |= n.all_method_names()
        return names

    def public_methods(self) -> list[MethodSig]:
        if self.kind == "interface":
            return [m for m in self.methods if not m.is_abstract]
        return [m for m in self.methods if m.is_public and not m.is_abstract]

    def enclosing_member(self, line_no: int) -> MethodSig | None:
        best: MethodSig | None = None
        for m in (*self.constructors, *self.methods):
            if m.line is None:
                continue
            end = m.end_line or m.line
            if m.line <= line_no <= end:
                if best is None or (m.line >= (best.line or 0)):
                    best = m
        return best


@dataclass(slots=True)
class ModuleInfo:
    name: str
    root: Path
    pom: Path


@dataclass(slots=True)
class JavaSourceFile:
    path: Path
    package: str | None = None
    imports: list[str] = field(default_factory=list)
    types: list[ClassSymbol] = field(default_factory=list)
    parse_ok: bool = True
    parse_error: str | None = None
    source: str = ""

    @property
    def primary_type(self) -> ClassSymbol | None:
        for t in self.types:
            if "public" in t.modifiers:
                return t
        return self.types[0] if self.types else None


@dataclass(slots=True)
class Collaborator:
    fqcn: str
    simple: str
    signatures: list[MethodSig] = field(default_factory=list)
    origin: str = ""


# ---------------------------------------------------------------------------
# Stack + classpath
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StackProfile:
    boot_major: int | None = None
    boot_version: str | None = None
    spring_major: int | None = None
    spring_version: str | None = None
    java_version: str | None = None
    jakarta: bool = True
    has_mockitobean: bool = True


@dataclass(slots=True)
class ClasspathProfile:
    has_junit_jupiter: bool = False
    has_mockito: bool = False
    has_spring_test: bool = False
    has_spring_web: bool = False
    has_webflux: bool = False
    has_data_jpa: bool = False
    has_reactor_test: bool = False
    has_webmvc_slice: bool = False
    has_webflux_slice: bool = False
    entries: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification + template
# ---------------------------------------------------------------------------


class TestKind(str, Enum):
    PURE_UNIT = "pure_unit"
    UNIT = PURE_UNIT
    WEBMVC = "webmvc"
    WEBFLUX = "webflux"
    DATA_JPA = "datajpa"
    REACTIVE_UNIT = "reactive_unit"
    SMOKE_CONTEXT = "smoke"
    INTEGRATION_ONLY = "integration_only"


@dataclass(slots=True)
class TemplateSpec:
    kind: TestKind
    style: str
    class_level_annotations: list[str] = field(default_factory=list)
    mock_annotation: str = "@Mock"
    required_imports: list[str] = field(default_factory=list)
    forbidden_imports: list[str] = field(default_factory=list)
    reactive: bool = False
    testable: bool = True
    line_target: float = 100.0
    branch_target: float = 90.0
    reasons: list[str] = field(default_factory=list)
    guidance: str = ""


# ---------------------------------------------------------------------------
# Generation context
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GenerationContext:
    cut_source: str
    symbol: ClassSymbol
    collaborators: list[Collaborator]
    test_package: str
    test_class_name: str
    stack: StackProfile
    classpath: ClasspathProfile
    template: TemplateSpec
    source_path: Path
    source_imports: list[str] = field(default_factory=list)
    truncated_source: bool = False
    execution_context: "ExecutionContext | None" = None


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LineCoverage:
    nr: int
    mi: int = 0
    ci: int = 0
    mb: int = 0
    cb: int = 0

    @property
    def status(self) -> str:
        if self.mb and self.cb:
            return "branch_partial"
        if self.mb and not self.cb:
            return "branch_missed"
        if self.mi and not self.ci:
            return "missed"
        if self.mi and self.ci:
            return "partial"
        return "covered"


@dataclass(slots=True)
class MethodCoverage:
    name: str
    desc: str = ""
    first_line: int | None = None
    missed_instr: int = 0
    covered_instr: int = 0
    missed_branch: int = 0
    covered_branch: int = 0
    missed_line: int = 0
    covered_line: int = 0

    @property
    def fully_uncovered(self) -> bool:
        return self.covered_instr == 0


@dataclass(slots=True)
class ClassCoverage:
    fqcn: str
    binary_name: str
    source_file: Path | None = None
    module: str = ""
    methods: list[MethodCoverage] = field(default_factory=list)
    lines: dict[int, LineCoverage] = field(default_factory=dict)
    missed_instr: int = 0
    covered_instr: int = 0
    missed_branch: int = 0
    covered_branch: int = 0
    missed_line: int = 0
    covered_line: int = 0

    @property
    def line_pct(self) -> float:
        total = self.missed_line + self.covered_line
        return 100.0 * self.covered_line / total if total else 100.0

    @property
    def branch_pct(self) -> float:
        total = self.missed_branch + self.covered_branch
        return 100.0 * self.covered_branch / total if total else 100.0

    @property
    def instr_pct(self) -> float:
        total = self.missed_instr + self.covered_instr
        return 100.0 * self.covered_instr / total if total else 100.0

    def uncovered_lines(self) -> list[int]:
        return sorted(n for n, l in self.lines.items() if l.status in ("missed", "partial"))

    def uncovered_branches(self) -> list[int]:
        return sorted(n for n, l in self.lines.items() if l.status in ("branch_missed", "branch_partial"))


@dataclass(slots=True)
class CoverageReport:
    classes: dict[str, ClassCoverage] = field(default_factory=dict)
    jacoco_version: str = ""
    measured: bool = True
    note: str | None = None

    def by_fqcn(self, fqcn: str) -> ClassCoverage | None:
        return self.classes.get(fqcn)


# ---------------------------------------------------------------------------
# Compile errors + post-processing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CompileError:
    file: Path | None
    line: int | None
    col: int | None
    message: str
    detail: list[str] = field(default_factory=list)
    raw: str = ""

    def render(self) -> str:
        loc = ""
        if self.file:
            loc = str(self.file)
            if self.line is not None:
                loc += f":{self.line}"
                if self.col is not None:
                    loc += f":{self.col}"
            loc += ": "

        parts = [loc + (self.message or "").strip()]
        for d in self.detail:
            d = (d or "").strip()
            if d:
                parts.append(d)

        if self.raw and self.raw.strip() and self.raw.strip() not in "\n".join(parts):
            parts.append(self.raw.strip())

        return "\n".join(parts).strip()


@dataclass(slots=True)
class FinalizeResult:
    ok: bool
    code: str | None = None
    reason: str | None = None
    needs_reask: bool = False
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Outcome / report
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TargetOutcome:
    fqcn: str
    module: str
    source_path: str
    test_path: str
    classification: str
    testable: bool
    status: str = "pending"
    rounds: int = 0
    line_pct: float | None = None
    branch_pct: float | None = None
    uncovered_lines: list[int] = field(default_factory=list)
    uncovered_branches: list[int] = field(default_factory=list)
    compile_errors: list[str] = field(default_factory=list)
    tokens_used: int = 0
    notes: list[str] = field(default_factory=list)