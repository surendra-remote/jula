"""Central typed models for junitforge.

Symbol table, stack/classpath profiles, classification/templates, generation
context, coverage, compile errors, post-processing, and per-target outcomes.
"""

from __future__ import annotations

import re
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
    annotation_exprs: list[str] = field(default_factory=list)

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
    initializer: str | None = None
    declaring_type: str | None = None


class EqualityStrategy(str, Enum):
    """Effective equality implementation visible from source/metadata."""

    OBJECT_IDENTITY = "object-identity"
    LOMBOK_EQUALS_HASHCODE = "lombok-equals-and-hash-code"
    LOMBOK_DATA = "lombok-data"
    LOMBOK_VALUE = "lombok-value"
    RECORD = "java-record"
    MANUAL = "manual"
    INHERITED = "inherited"
    UNRESOLVED = "unresolved"


class EqualityAssertionMode(str, Enum):
    """Assertions that can be generated without guessing."""

    IDENTITY = "identity"
    VALUE = "resolved-value"
    CONSERVATIVE = "conservative"


@dataclass(slots=True)
class EqualityMember:
    """One verified input to the effective equals/hashCode implementation."""

    name: str
    type_name: str
    declaring_type: str
    origin: str = "field"
    accessor: str | None = None
    mutator: str | None = None
    assignable: bool = False
    equal_value: str | None = None
    different_value: str | None = None
    shared_reference: bool = False
    nested_equality_resolved: bool = True
    identifier: bool = False
    generated_identifier: bool = False
    relationship: bool = False

    @property
    def has_verified_difference(self) -> bool:
        return self.equal_value is not None and self.different_value is not None


@dataclass(slots=True)
class EqualityDescriptor:
    """Small, source-backed plan consumed by the existing DTO/entity generator."""

    strategy: EqualityStrategy
    assertion_mode: EqualityAssertionMode
    members: list[EqualityMember] = field(default_factory=list)
    difference_member: str | None = None
    only_explicitly_included: bool = False
    call_super: bool | None = None
    parent_type: str | None = None
    entity: bool = False
    entity_key_kind: str | None = None
    unrelated_type_safe: bool = False
    warnings: list[str] = field(default_factory=list)


# ClassSymbol intentionally remains unslotted. Parser and resolver stages attach
# verified owner/source metadata while normalizing symbols from both the
# JavaParser CLI and the fallback parser. Keeping a normal __dict__ makes that
# enrichment backward-compatible when a cached or older symbol payload omits a
# newly introduced metadata field. All supported metadata is still declared
# explicitly below.
@dataclass
class ClassSymbol:
    name: str
    fqcn: str
    kind: str
    modifiers: set[str] = field(default_factory=set)
    package_name: str | None = None
    imports: list[str] = field(default_factory=list)
    source_path: str | None = None
    enclosing_fqcn: str | None = None
    type_parameters: list[str] = field(default_factory=list)
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
    # Kept at the end so existing positional ClassSymbol construction remains
    # backward-compatible. Interfaces can extend multiple parent interfaces;
    # ``extends`` remains the legacy first-parent view.
    extends_types: list[str] = field(default_factory=list)
    annotation_exprs: list[str] = field(default_factory=list)
    equality_descriptor: EqualityDescriptor | None = None

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
    equality_descriptor: EqualityDescriptor | None = None
    symbol_lookup: object | None = None


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


class GeneratedScopeKind(str, Enum):
    """Stable repair units inside one generated Java test class."""

    IMPORT_BLOCK = "import_block"
    CLASS_FIELD = "class_field"
    MOCK_DECLARATION = "mock_declaration"
    SETUP_METHOD = "setup_method"
    FIXTURE_HELPER = "fixture_helper"
    STUB_HELPER = "stub_helper"
    TEST_METHOD = "test_method"
    OTHER_CLASS_DECLARATION = "other_class_declaration"


@dataclass(slots=True, frozen=True)
class GeneratedScope:
    """Exact source span that may be repaired without rewriting the class."""

    scope_id: str
    kind: GeneratedScopeKind
    name: str
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
    source: str
    owner_method_id: str | None = None


class TestMethodStatus(str, Enum):
    PASSED = "passed"
    ASSERTION_FAILURE = "assertion_failure"
    RUNTIME_ERROR = "runtime_error"
    MOCKITO_FAILURE = "mockito_failure"
    SKIPPED = "skipped"
    MISSING = "missing"


@dataclass(slots=True)
class TestMethodResult:
    """One Surefire/JUnit result with repair-grade, filtered evidence."""

    method_name: str
    status: TestMethodStatus
    class_name: str = ""
    duration_seconds: float | None = None
    exception_type: str | None = None
    message: str | None = None
    expected: str | None = None
    actual: str | None = None
    source_line: int | None = None
    root_cause: str | None = None
    cause_chain: list[str] = field(default_factory=list)
    filtered_stack_trace: list[str] = field(default_factory=list)
    generated_test_frame: str | None = None
    cut_frame: str | None = None
    null_path: str | None = None
    mockito_subtype: str | None = None
    stub_declaration: str | None = None
    stub_source_location: str | None = None
    actual_invocation: str | None = None
    actual_invocation_source_location: str | None = None
    expected_arguments: list[str] = field(default_factory=list)
    actual_arguments: list[str] = field(default_factory=list)
    unused_stub_locations: list[str] = field(default_factory=list)
    raw_diagnostic: str = ""

    def fingerprint(self) -> str:
        parts = (
            self.method_name,
            self.status.value,
            self.exception_type or "",
            self.message or "",
            self.root_cause or "",
            self.expected or "",
            self.actual or "",
            self.mockito_subtype or "",
            self.stub_declaration or "",
            self.actual_invocation or "",
            ",".join(self.expected_arguments),
            ",".join(self.actual_arguments),
        )
        return "|".join(re.sub(r"\s+", " ", part).strip() for part in parts)


@dataclass(slots=True)
class ClassExecutionResult:
    """Outcome of one complete-class JUnit/Surefire execution."""

    compiled: bool
    executed: bool
    ok: bool
    test_methods: list[TestMethodResult] = field(default_factory=list)
    compiler_errors: list[CompileError] = field(default_factory=list)
    note: str = ""
    raw_log: str = ""

    @property
    def passing_methods(self) -> list[str]:
        return [
            result.method_name
            for result in self.test_methods
            if result.status is TestMethodStatus.PASSED
        ]

    @property
    def unresolved_methods(self) -> list[TestMethodResult]:
        return [
            result
            for result in self.test_methods
            if result.status is not TestMethodStatus.PASSED
        ]


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
    class_validation: dict[str, object] = field(default_factory=dict)
