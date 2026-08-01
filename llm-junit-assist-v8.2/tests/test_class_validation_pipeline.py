from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from junitforge.config import GenConfig
from junitforge.coverage_run import parse_surefire_reports
from junitforge.execution.assembly import (
    generated_scopes,
    replace_method_block,
    validate_scoped_class_repair,
)
from junitforge.execution.models import (
    ConfigurationField,
    ConfigurationRequirement,
    ConstructionKind,
    DependencyInvocation,
    DependencyMethodContract,
    DependencyRef,
    ExecutionContext,
    ExecutionTargetKind,
    FixtureSpec,
    InvocationArgument,
    MethodExecutionContext,
    PayloadProperty,
    PayloadSchema,
    PrimaryPathSelection,
    ResolutionStatus,
)
from junitforge.loop import Engine
from junitforge.models import (
    ClassExecutionResult,
    ClassSymbol,
    ClasspathProfile,
    CompileError,
    GeneratedScopeKind,
    GenerationContext,
    MethodSig,
    ModuleInfo,
    StackProfile,
    TargetOutcome,
    TemplateSpec,
    TestKind,
    TestMethodResult,
    TestMethodStatus,
)
from junitforge.prompts.repair import (
    build_scoped_compile_repair_messages,
    build_scoped_execution_repair_messages,
)
from junitforge.report import write_reports
from junitforge.vendor.javac import CompileResult
from junitforge.vendor.llm.base import LLMResponse
from junitforge.vendor.llm.stub import StubLLMClient

# These production model names intentionally begin with ``Test``; they are not
# pytest test containers when imported into this regression module.
TestKind.__test__ = False
TestMethodResult.__test__ = False
TestMethodStatus.__test__ = False


GENERATED_CLASS = """package sample;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import sample.Request;
import sample.UnrelatedDto;

class SampleServiceImplTest {
    @Mock
    private Repo repo;

    @InjectMocks
    private SampleServiceImpl sampleServiceImpl;

    @BeforeEach
    void setUp() {
        ReflectionTestUtils.setField(sampleServiceImpl, "successMessage", "OK");
    }

    private Request createRequest() {
        Request request = new Request();
        request.setCode("OK");
        request.setAmount(1);
        return request;
    }

    private UnrelatedDto createUnrelated() {
        return new UnrelatedDto();
    }

    private void stubShared() {
        when(repo.load("OK")).thenReturn(new Object());
    }

    // JUNITFORGE_METHOD_BEGIN: process(Request)
    @Test
    void process_success() {
        Request request = createRequest();
        stubShared();
        Object response = sampleServiceImpl.process(request);
        assertNotNull(response);
    }
    // JUNITFORGE_METHOD_END: process(Request)

    // JUNITFORGE_METHOD_BEGIN: second(Request)
    @Test
    void second_success() {
        Request request = createRequest();
        Object response = sampleServiceImpl.second(request);
        assertNotNull(response);
    }
    // JUNITFORGE_METHOD_END: second(Request)
}
"""


CUT_SOURCE = """package sample;
class SampleServiceImpl {
    private Repo repo;
    private String successMessage;
    public Object process(Request request) {
        return repo.load(request.getCode());
    }
    public Object second(Request request) {
        return request == null ? new Object() : repo.load(request.getCode());
    }
}
"""


def _line(source: str, needle: str, occurrence: int = 1) -> int:
    seen = 0
    for number, text in enumerate(source.splitlines(), start=1):
        if needle in text:
            seen += 1
            if seen == occurrence:
                return number
    raise AssertionError(f"line not found: {needle}")


def _scope(source: str, name: str):
    return next(scope for scope in generated_scopes(source, fixture_names={"createRequest", "createUnrelated"}) if scope.name == name)


class ScriptedCompileGate:
    def __init__(self, script):
        self.script = deque(script)
        self.calls: list[str] = []

    def _next(self, test_file: Path):
        source = test_file.read_text(encoding="utf-8")
        self.calls.append(source)
        if not self.script:
            raise AssertionError("unexpected compilation")
        item = self.script.popleft()
        if callable(item):
            return item(test_file, source)
        if item is True:
            return CompileResult(ok=True), []
        if isinstance(item, tuple):
            return item
        raise AssertionError(f"unsupported compile script item: {item!r}")

    def check_complete_class(self, test_file: Path, module: ModuleInfo):
        return self._next(test_file)

    def check(self, test_file: Path, module: ModuleInfo):
        return self._next(test_file)


class ScriptedClassRunner:
    def __init__(self, results):
        self.results = deque(results)
        self.calls: list[dict[str, object]] = []

    def execute_class(self, module, **kwargs):
        self.calls.append({
            **kwargs,
            "source": Path(kwargs["test_file"]).read_text(encoding="utf-8"),
        })
        if not self.results:
            raise AssertionError("unexpected complete-class execution")
        return self.results.popleft()


def _passed(*names: str) -> ClassExecutionResult:
    return ClassExecutionResult(
        compiled=True,
        executed=True,
        ok=True,
        test_methods=[
            TestMethodResult(name, TestMethodStatus.PASSED, class_name="sample.SampleServiceImplTest")
            for name in names
        ],
        note="all passed",
    )


def _failed(*results: TestMethodResult) -> ClassExecutionResult:
    return ClassExecutionResult(
        compiled=True,
        executed=True,
        ok=False,
        test_methods=list(results),
        note="failed",
    )


def _assertion_failure(name: str, message: str = "expected: not <null> but was: <null>") -> TestMethodResult:
    return TestMethodResult(
        method_name=name,
        status=TestMethodStatus.ASSERTION_FAILURE,
        exception_type="org.opentest4j.AssertionFailedError",
        message=message,
        expected="non-null",
        actual="null",
        source_line=_line(GENERATED_CLASS, f"void {name}()"),
        generated_test_frame=f"at sample.SampleServiceImplTest.{name}(SampleServiceImplTest.java:{_line(GENERATED_CLASS, f'void {name}()')})",
    )


def _runtime_failure(name: str, message: str = 'Cannot invoke "Request.getCode()" because "request" is null') -> TestMethodResult:
    return TestMethodResult(
        method_name=name,
        status=TestMethodStatus.RUNTIME_ERROR,
        exception_type="java.lang.NullPointerException",
        message=message,
        source_line=_line(GENERATED_CLASS, f"void {name}()"),
        root_cause=f"java.lang.NullPointerException: {message}",
        cause_chain=[f"java.lang.NullPointerException: {message}"],
        generated_test_frame=f"at sample.SampleServiceImplTest.{name}(SampleServiceImplTest.java:{_line(GENERATED_CLASS, f'void {name}()')})",
        cut_frame="at sample.SampleServiceImpl.process(SampleServiceImpl.java:6)",
        filtered_stack_trace=[
            "at sample.SampleServiceImpl.process(SampleServiceImpl.java:6)",
            f"at sample.SampleServiceImplTest.{name}(SampleServiceImplTest.java:{_line(GENERATED_CLASS, f'void {name}()')})",
        ],
        null_path="request",
    )


def _compile_failure(message: str, needle: str):
    def result(path: Path, source: str):
        error = CompileError(
            file=path,
            line=_line(source, needle),
            col=9,
            message=message,
        )
        return CompileResult(ok=False, errors=[message]), [error]

    return result


def _two_compile_failures(message1: str, needle1: str, message2: str, needle2: str):
    def result(path: Path, source: str):
        errors = [
            CompileError(path, _line(source, needle1), 9, message1),
            CompileError(path, _line(source, needle2), 13, message2),
        ]
        return CompileResult(ok=False, errors=[message1, message2]), errors

    return result


def _context() -> GenerationContext:
    process_sig = MethodSig(
        name="process",
        modifiers={"public"},
        return_type="Object",
        params=[("Request", "request")],
        line=5,
        end_line=7,
    )
    second_sig = MethodSig(
        name="second",
        modifiers={"public"},
        return_type="Object",
        params=[("Request", "request")],
        line=8,
        end_line=10,
    )
    symbol = ClassSymbol(
        name="SampleServiceImpl",
        fqcn="sample.SampleServiceImpl",
        kind="class",
        methods=[process_sig, second_sig],
    )
    contract = DependencyMethodContract(
        dependency_field="repo",
        dependency_type="Repo",
        method_name="load",
        return_type="Object",
        parameter_types=("String",),
        parameter_names=("code",),
        declaring_type="sample.Repo",
        resolution_status=ResolutionStatus.RESOLVED,
    )

    def method_context(name: str, source: str, start: int, end: int):
        return MethodExecutionContext(
            method_id=f"{name}(Request)",
            signature=f"public Object {name}(Request request)",
            source_range=(start, end),
            method_source=source,
            endpoint=None,
            dependency_invocations=(DependencyInvocation(
                invocation_id=f"{name}:repo.load",
                caller_method_id=f"{name}(Request)",
                dependency_field="repo",
                dependency_type="Repo",
                method_name="load",
                arguments=(InvocationArgument("request.getCode()", inferred_type="String"),),
                contract=contract,
                line=start + 1,
            ),),
            required_fixture_ids=("sample.Request",),
            configuration_requirements=(ConfigurationRequirement("successMessage", "successMessage", line=start),),
            selected_primary_path=PrimaryPathSelection(
                reachable_method_ids=(),
                score=10,
                feasibility=ResolutionStatus.RESOLVED,
            ),
        )

    request_schema = PayloadSchema(
        type_name="Request",
        fqcn="sample.Request",
        kind="class",
        constructors=("Request()",),
        properties=(
            PayloadProperty("code", "String", True, True, getter="getCode", setter="setCode", resolved_type="String"),
            PayloadProperty("amount", "int", True, True, getter="getAmount", setter="setAmount", resolved_type="int"),
        ),
        resolution_status=ResolutionStatus.RESOLVED,
        construction_kind=ConstructionKind.NO_ARGS_SETTERS,
    )
    unrelated_schema = PayloadSchema(
        type_name="UnrelatedDto",
        fqcn="sample.UnrelatedDto",
        kind="class",
        constructors=("UnrelatedDto()",),
        resolution_status=ResolutionStatus.RESOLVED,
        construction_kind=ConstructionKind.NO_ARGS_SETTERS,
    )
    execution = ExecutionContext(
        target_kind=ExecutionTargetKind.SERVICE_IMPL,
        target_fqcn=symbol.fqcn,
        dependencies=(DependencyRef(
            field_name="repo",
            parameter_name=None,
            declared_type="Repo",
            simple_type="Repo",
            fqcn="sample.Repo",
            origin="field",
            resolution_status=ResolutionStatus.RESOLVED,
        ),),
        configuration_fields=(ConfigurationField(
            field_name="successMessage",
            type_name="String",
            property_key="service.success-message",
            default_value="OK",
            test_value='"OK"',
            annotation_expr='@Value("${service.success-message:OK}")',
        ),),
        methods=(
            method_context("process", "public Object process(Request request) {\n    return repo.load(request.getCode());\n}", 5, 7),
            method_context("second", "public Object second(Request request) {\n    return request == null ? new Object() : repo.load(request.getCode());\n}", 8, 10),
        ),
        payload_schemas=(request_schema, unrelated_schema),
        fixtures=(
            FixtureSpec("sample.Request", "createRequest", "Request", "sample.Request", method_source="private Request createRequest() { return new Request(); }"),
            FixtureSpec("sample.UnrelatedDto", "createUnrelated", "UnrelatedDto", "sample.UnrelatedDto", method_source="private UnrelatedDto createUnrelated() { return new UnrelatedDto(); }"),
        ),
        extraction_status=ResolutionStatus.RESOLVED,
    )
    return GenerationContext(
        cut_source=CUT_SOURCE,
        symbol=symbol,
        collaborators=[],
        test_package="sample",
        test_class_name="SampleServiceImplTest",
        stack=StackProfile(boot_major=3, spring_major=6, java_version="17"),
        classpath=ClasspathProfile(has_junit_jupiter=True, has_mockito=True),
        template=TemplateSpec(TestKind.PURE_UNIT, "unit"),
        source_path=Path("SampleServiceImpl.java"),
        source_imports=["sample.Request", "sample.UnrelatedDto", "sample.Repo"],
        execution_context=execution,
    )


def _outcome() -> TargetOutcome:
    return TargetOutcome(
        fqcn="sample.SampleServiceImpl",
        module="sample",
        source_path="SampleServiceImpl.java",
        test_path="SampleServiceImplTest.java",
        classification="pure_unit",
        testable=True,
    )


def _engine(compile_script, execution_script, llm_messages=(), *, compile_rounds=3, execution_rounds=3):
    gate = ScriptedCompileGate(compile_script)
    runner = ScriptedClassRunner(execution_script)
    llm = StubLLMClient([LLMResponse(message=message) for message in llm_messages])
    engine = Engine(
        llm=llm,
        compile_gate=gate,
        coverage_runner=runner,
        stack=StackProfile(),
        modules=[],
        repo_root=Path("."),
        lookup=lambda _name: None,
        cfg=GenConfig(max_compile_repairs=compile_rounds, max_failure_repairs=execution_rounds),
    )
    return engine, gate, runner, llm


class ClassValidationPipelineRegressionTest(unittest.TestCase):
    def _run(self, source, compile_script, execution_script, llm_messages=(), **limits):
        engine, gate, runner, llm = _engine(
            compile_script,
            execution_script,
            llm_messages,
            **limits,
        )
        outcome = _outcome()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "SampleServiceImplTest.java"
            path.write_text(source, encoding="utf-8")
            module = ModuleInfo("sample", root, root / "pom.xml")
            result = engine._validate_serviceimpl_class(_context(), path, module, outcome)
            final_source = path.read_text(encoding="utf-8")
            return result, final_source, outcome, engine, gate, runner, llm, path

    def test_01_all_primary_tests_exist_before_first_class_compilation(self):
        _, _, outcome, _, gate, runner, _, _ = self._run(
            GENERATED_CLASS,
            [True],
            [_passed("process_success", "second_success")],
        )
        self.assertEqual(1, len(gate.calls))
        self.assertIn("void process_success()", gate.calls[0])
        self.assertIn("void second_success()", gate.calls[0])
        self.assertEqual(2, outcome.class_validation["initial_test_count"])
        self.assertEqual(2, outcome.class_validation["expected_primary_method_count"])
        self.assertTrue(outcome.class_validation["all_primary_tests_generated_before_compilation"])
        self.assertEqual(1, len(runner.calls))

        incomplete = replace_method_block(GENERATED_CLASS, "second(Request)", "")
        result, _, incomplete_outcome, _, incomplete_gate, incomplete_runner, _, _ = self._run(
            incomplete,
            [],
            [],
        )
        self.assertIsNone(result)
        self.assertEqual([], incomplete_gate.calls)
        self.assertEqual([], incomplete_runner.calls)
        self.assertEqual(
            ["second(Request)"],
            incomplete_outcome.class_validation["missing_primary_method_ids"],
        )

    def test_02_complete_generated_class_is_compiled_as_one_unit(self):
        result, _, _, _, gate, _, _, _ = self._run(
            GENERATED_CLASS,
            [True],
            [_passed("process_success", "second_success")],
        )
        self.assertIsNotNone(result)
        self.assertEqual(GENERATED_CLASS, gate.calls[0])

    def test_03_multiple_errors_in_one_test_scope_make_one_repair_request(self):
        source = GENERATED_CLASS.replace(
            "Request request = createRequest();\n        stubShared();",
            "String code = BAD_CODE;\n        int amount = BAD_AMOUNT;\n        Request request = createRequest();\n        stubShared();",
            1,
        )
        repaired = _scope(source, "process_success").source.replace("BAD_CODE", '"OK"').replace("BAD_AMOUNT", "1")
        _, _, outcome, _, _, _, llm, _ = self._run(
            source,
            [_two_compile_failures("cannot find symbol BAD_CODE", "BAD_CODE", "cannot find symbol BAD_AMOUNT", "BAD_AMOUNT"), True],
            [_passed("process_success", "second_success")],
            [repaired],
        )
        self.assertEqual(1, len(llm.calls))
        prompt = llm.calls[0]["messages"][-1]["content"]
        self.assertIn("cannot find symbol BAD_CODE", prompt)
        self.assertIn("cannot find symbol BAD_AMOUNT", prompt)
        self.assertEqual(1, outcome.class_validation["compilation_repair_rounds_attempted"])

    def test_04_shared_helper_compiler_errors_are_repaired_once_at_helper_scope(self):
        source = GENERATED_CLASS.replace('request.setCode("OK");', "request.setCode(BAD_CODE);").replace(
            "request.setAmount(1);", "request.setAmount(BAD_AMOUNT);"
        )
        helper = _scope(source, "createRequest").source.replace("BAD_CODE", '"OK"').replace("BAD_AMOUNT", "1")
        _, _, _, _, _, _, llm, _ = self._run(
            source,
            [_two_compile_failures("bad code", "BAD_CODE", "bad amount", "BAD_AMOUNT"), True],
            [_passed("process_success", "second_success")],
            [helper],
        )
        self.assertEqual(1, len(llm.calls))
        prompt = llm.calls[0]["messages"][-1]["content"]
        self.assertIn("scope kind: fixture_helper", prompt)
        self.assertIn("bad code", prompt)
        self.assertIn("bad amount", prompt)

    def test_05_compile_prompt_contains_exact_minimal_verified_evidence(self):
        ctx = _context()
        scope = _scope(GENERATED_CLASS, "process_success")
        error = CompileError(Path("SampleServiceImplTest.java"), scope.start_line + 2, 17, "missing accessor getCode")
        prompt = build_scoped_compile_repair_messages(
            ctx,
            GENERATED_CLASS,
            scope,
            [error],
            ["Object response = sampleServiceImpl.process(request);"],
        )[-1]["content"]
        self.assertIn("missing accessor getCode", prompt)
        self.assertIn("offending generated statement", prompt)
        self.assertIn("void process_success()", prompt)
        self.assertIn("VERIFIED JAVA TYPE: sample.Request", prompt)
        self.assertIn("VERIFIED COLLABORATOR SIGNATURE", prompt)
        self.assertNotIn("void second_success()", prompt)
        self.assertNotIn("VERIFIED JAVA TYPE: sample.UnrelatedDto", prompt)

    def test_06_execution_never_starts_while_compilation_errors_remain(self):
        source = GENERATED_CLASS.replace("stubShared();", "stubShared(BAD);")
        unchanged = _scope(source, "process_success").source
        result, _, outcome, _, gate, runner, _, _ = self._run(
            source,
            [_compile_failure("cannot find symbol BAD", "BAD")],
            [],
            [unchanged],
        )
        self.assertIsNone(result)
        self.assertEqual(0, len(runner.calls))
        self.assertEqual("compile_failed", outcome.status)
        self.assertEqual(1, len(gate.calls))

    def test_07_compilation_repair_stops_immediately_when_class_compiles(self):
        source = GENERATED_CLASS.replace("stubShared();", "stubShared(BAD);")
        repaired = _scope(source, "process_success").source.replace("stubShared(BAD);", "stubShared();")
        _, _, outcome, _, gate, _, llm, _ = self._run(
            source,
            [_compile_failure("bad invocation", "stubShared(BAD)"), True],
            [_passed("process_success", "second_success")],
            [repaired, repaired, repaired],
        )
        self.assertEqual(2, len(gate.calls))
        self.assertEqual(1, len(llm.calls))
        self.assertEqual("passed", outcome.class_validation["final_compilation_result"])

    def test_08_compilation_repair_never_exceeds_three_rounds(self):
        source = GENERATED_CLASS.replace("stubShared();", "stubShared(BAD);")
        repairs = [
            _scope(source, "process_success").source.replace("stubShared(BAD);", f"stubShared(BAD);\n        // repair {index}")
            for index in range(1, 4)
        ]
        failures = [_compile_failure(f"compile error {index}", "stubShared(BAD)") for index in range(1, 5)]
        result, _, outcome, _, gate, runner, llm, _ = self._run(
            source,
            failures,
            [],
            repairs,
            compile_rounds=8,
        )
        self.assertIsNone(result)
        self.assertEqual(4, len(gate.calls))
        self.assertEqual(3, len(llm.calls))
        self.assertEqual(3, outcome.class_validation["compilation_repair_rounds_attempted"])
        self.assertEqual(0, len(runner.calls))

    def test_09_repeated_compiler_diagnostics_terminate_early(self):
        source = GENERATED_CLASS.replace("stubShared();", "stubShared(BAD);")
        repaired = _scope(source, "process_success").source.replace("stubShared(BAD);", "stubShared(BAD);\n        // changed")
        result, _, outcome, _, gate, _, _, _ = self._run(
            source,
            [_compile_failure("same compiler error", "stubShared(BAD)"), _compile_failure("same compiler error", "stubShared(BAD)")],
            [],
            [repaired],
        )
        self.assertIsNone(result)
        self.assertEqual(2, len(gate.calls))
        self.assertIn("same unresolved", outcome.class_validation["publication_reason"])

    def test_10_complete_compiled_class_executes_in_one_class_level_run(self):
        _, _, _, _, _, runner, _, _ = self._run(
            GENERATED_CLASS,
            [True],
            [_passed("process_success", "second_success")],
        )
        self.assertEqual(1, len(runner.calls))
        self.assertEqual(
            ("process_success", "second_success"),
            tuple(runner.calls[0]["expected_test_methods"]),
        )
        self.assertEqual("sample.SampleServiceImplTest", runner.calls[0]["test_class_name"])

        unavailable = ClassExecutionResult(
            compiled=True,
            executed=False,
            ok=False,
            test_methods=[
                TestMethodResult(
                    "process_success",
                    TestMethodStatus.MISSING,
                    message="Surefire report missing",
                ),
                TestMethodResult(
                    "second_success",
                    TestMethodStatus.MISSING,
                    message="Surefire report missing",
                ),
            ],
            note="Surefire did not execute",
        )
        result, _, unavailable_outcome, _, _, unavailable_runner, unavailable_llm, _ = self._run(
            GENERATED_CLASS,
            [True],
            [unavailable],
        )
        self.assertIsNone(result)
        self.assertEqual(1, len(unavailable_runner.calls))
        self.assertEqual(0, len(unavailable_llm.calls))
        self.assertEqual("failing_artifact", unavailable_outcome.class_validation["publication_result"])

    def test_11_passing_tests_are_not_sent_for_execution_repair(self):
        failure = _runtime_failure("process_success")
        repaired = _scope(GENERATED_CLASS, "process_success").source.replace("stubShared();", "stubShared();\n        // runtime repaired")
        self._run(
            GENERATED_CLASS,
            [True, True],
            [
                _failed(failure, TestMethodResult("second_success", TestMethodStatus.PASSED)),
                _passed("process_success", "second_success"),
            ],
            [repaired],
        )
        _, _, _, _, _, _, llm, _ = self._run(
            GENERATED_CLASS,
            [True, True],
            [
                _failed(failure, TestMethodResult("second_success", TestMethodStatus.PASSED)),
                _passed("process_success", "second_success"),
            ],
            [repaired],
        )
        prompt = llm.calls[0]["messages"][-1]["content"]
        self.assertIn("process_success", prompt)
        self.assertNotIn("void second_success()", prompt)

    def test_12_assertion_repair_has_actual_evidence_and_preserves_assert_not_null(self):
        failure = _assertion_failure("process_success")
        scope = _scope(GENERATED_CLASS, "process_success")
        prompt = build_scoped_execution_repair_messages(
            _context(), GENERATED_CLASS, scope, [failure]
        )[-1]["content"]
        self.assertIn("EXPECTED VALUE: non-null", prompt)
        self.assertIn("ACTUAL VALUE: null", prompt)
        self.assertIn("Preserve assertNotNull(response)", prompt)
        weakened = GENERATED_CLASS.replace("assertNotNull(response);", "assertTrue(true);", 1)
        reason = validate_scoped_class_repair(
            GENERATED_CLASS,
            weakened,
            modified_scope_ids={scope.scope_id},
            strict_serviceimpl=True,
        )
        self.assertIn("assertNotNull", reason or "")
        bypassed = GENERATED_CLASS.replace(
            "Object response = sampleServiceImpl.process(request);",
            "Object response = new Object();",
            1,
        )
        bypass_reason = validate_scoped_class_repair(
            GENERATED_CLASS,
            bypassed,
            modified_scope_ids={scope.scope_id},
            strict_serviceimpl=True,
        )
        self.assertIn("bypassed", bypass_reason or "")

    def test_13_runtime_repair_receives_root_cause_cut_test_frames_and_null_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            test_file = root / "SampleServiceImplTest.java"
            test_file.write_text(GENERATED_CLASS, encoding="utf-8")
            report_dir = root / "target" / "surefire-reports"
            report_dir.mkdir(parents=True)
            suite = ET.Element("testsuite", name="sample.SampleServiceImplTest")
            # ConsoleLauncher uses ``method()`` while Surefire uses ``method``.
            case = ET.SubElement(suite, "testcase", name="process_success()", classname="sample.SampleServiceImplTest", time="0.01")
            error = ET.SubElement(case, "error", type="java.lang.NullPointerException", message='Cannot invoke "Request.getCode()" because "request" is null')
            error.text = """java.lang.NullPointerException: Cannot invoke "Request.getCode()" because "request" is null
\tat sample.SampleServiceImpl.process(SampleServiceImpl.java:6)
\tat sample.SampleServiceImplTest.process_success(SampleServiceImplTest.java:42)
\tat org.junit.platform.engine.support.hierarchical.NodeTestTask.execute(NodeTestTask.java:1)
Caused by: java.lang.NullPointerException: request was null"""
            ET.ElementTree(suite).write(report_dir / "TEST-sample.SampleServiceImplTest.xml", encoding="unicode")
            parsed = parse_surefire_reports(
                report_dirs={report_dir},
                test_class_name="sample.SampleServiceImplTest",
                test_file=test_file,
                cut_class_name="SampleServiceImpl",
                expected_test_methods=("process_success",),
            )
        result = parsed.test_methods[0]
        self.assertEqual("process_success", result.method_name)
        self.assertEqual(TestMethodStatus.RUNTIME_ERROR, result.status)
        self.assertIn("NullPointerException", result.root_cause or "")
        self.assertIn("SampleServiceImplTest.java:42", result.generated_test_frame or "")
        self.assertIn("SampleServiceImpl.java:6", result.cut_frame or "")
        self.assertEqual("request", result.null_path)
        self.assertFalse(any("junit.platform" in frame for frame in result.filtered_stack_trace))

    def test_14_mockito_repair_receives_stub_invocation_arguments_locations_and_signature(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            test_file = root / "SampleServiceImplTest.java"
            test_file.write_text(GENERATED_CLASS, encoding="utf-8")
            report_dir = root / "target" / "surefire-reports"
            report_dir.mkdir(parents=True)
            suite = ET.Element("testsuite", name="sample.SampleServiceImplTest")
            case = ET.SubElement(suite, "testcase", name="process_success", classname="sample.SampleServiceImplTest")
            failure_node = ET.SubElement(
                case,
                "failure",
                type="org.mockito.exceptions.misusing.PotentialStubbingProblem",
                message="Strict stubbing argument mismatch",
            )
            stub_line = _line(GENERATED_CLASS, 'when(repo.load("OK"))')
            failure_node.text = f"""Strict stubbing argument mismatch. Please check:
 - this invocation of 'load' method:
    repo.load("ACTUAL");
    -> at sample.SampleServiceImpl.process(SampleServiceImpl.java:6)
 - has following stubbing(s) with different arguments:
    1. repo.load("EXPECTED");
      -> at sample.SampleServiceImplTest.stubShared(SampleServiceImplTest.java:{stub_line})
Typically, stubbing argument mismatch indicates a test setup mistake."""
            ET.ElementTree(suite).write(report_dir / "TEST-sample.SampleServiceImplTest.xml", encoding="unicode")
            parsed = parse_surefire_reports(
                report_dirs={report_dir},
                test_class_name="sample.SampleServiceImplTest",
                test_file=test_file,
                cut_class_name="SampleServiceImpl",
                expected_test_methods=("process_success",),
            )
        failure = parsed.test_methods[0]
        self.assertEqual(TestMethodStatus.MOCKITO_FAILURE, failure.status)
        self.assertEqual('repo.load("ACTUAL")', failure.actual_invocation)
        self.assertEqual(['"ACTUAL"'], failure.actual_arguments)
        self.assertEqual(['"EXPECTED"'], failure.expected_arguments)
        self.assertIn(f"SampleServiceImplTest.java:{stub_line}", failure.stub_source_location or "")
        self.assertEqual(
            'when(repo.load("OK")).thenReturn(new Object());',
            failure.stub_declaration,
        )
        scope = _scope(GENERATED_CLASS, "stubShared")
        prompt = build_scoped_execution_repair_messages(
            _context(), GENERATED_CLASS, scope, [failure]
        )[-1]["content"]
        for text in (
            "PotentialStubbingProblem",
            'when(repo.load("OK")).thenReturn(new Object());',
            'repo.load("ACTUAL")',
            "EXPECTED/STUBBED ARGUMENTS",
            "ACTUAL INVOCATION ARGUMENTS",
            "UNUSED STUB LOCATIONS",
            "VERIFIED COLLABORATOR SIGNATURE",
        ):
            self.assertIn(text, prompt)
        engine, _, _, _ = _engine([], [])
        broad = scope.source.replace('repo.load("OK")', "repo.load(any())")
        self.assertIn(
            "every repo.load argument with raw any()",
            engine._validate_execution_repair_contract(_context(), broad) or "",
        )
        invalid_void_syntax = scope.source.replace(
            'when(repo.load("OK")).thenReturn(new Object());',
            'doNothing().when(repo).load("OK");',
        )
        self.assertIn(
            "verified non-void method repo.load",
            engine._validate_execution_repair_contract(_context(), invalid_void_syntax) or "",
        )

    def test_15_repair_context_excludes_unrelated_tests_fixtures_and_types(self):
        scope = _scope(GENERATED_CLASS, "process_success")
        prompt = build_scoped_execution_repair_messages(
            _context(), GENERATED_CLASS, scope, [_runtime_failure("process_success")]
        )[-1]["content"]
        self.assertNotIn("void second_success()", prompt)
        self.assertNotIn("createUnrelated", prompt)
        self.assertNotIn("VERIFIED JAVA TYPE: sample.UnrelatedDto", prompt)

    def test_16_all_current_failure_repairs_apply_before_one_recompile_and_rerun(self):
        first = _scope(GENERATED_CLASS, "process_success").source.replace("stubShared();", "stubShared();\n        // fixed process")
        second = _scope(GENERATED_CLASS, "second_success").source.replace(
            "Object response", "// fixed second\n        Object response"
        )
        initial = _failed(_runtime_failure("process_success"), _runtime_failure("second_success", "second failed"))
        _, _, _, _, gate, runner, llm, _ = self._run(
            GENERATED_CLASS,
            [True, True],
            [initial, _passed("process_success", "second_success")],
            [first, second],
        )
        self.assertEqual(2, len(llm.calls))
        self.assertEqual(2, len(gate.calls))
        self.assertIn("// fixed process", gate.calls[1])
        self.assertIn("// fixed second", gate.calls[1])
        self.assertEqual(2, len(runner.calls))
        self.assertIn("// fixed process", runner.calls[1]["source"])
        self.assertIn("// fixed second", runner.calls[1]["source"])

    def test_17_execution_repair_never_exceeds_three_rounds(self):
        repairs = [
            _scope(GENERATED_CLASS, "process_success").source.replace(
                "stubShared();", f"stubShared();\n        // execution repair {index}"
            )
            for index in range(1, 4)
        ]
        results = [
            _failed(_runtime_failure("process_success", f"runtime failure {index}"), TestMethodResult("second_success", TestMethodStatus.PASSED))
            for index in range(1, 5)
        ]
        result, _, outcome, _, gate, runner, llm, _ = self._run(
            GENERATED_CLASS,
            [True, True, True, True],
            results,
            repairs,
            execution_rounds=9,
        )
        self.assertIsNone(result)
        self.assertEqual(3, outcome.class_validation["execution_repair_rounds_attempted"])
        self.assertEqual(3, len(llm.calls))
        self.assertEqual(4, len(gate.calls))
        self.assertEqual(4, len(runner.calls))

        repeated = _runtime_failure("process_success", "unchanged runtime failure")
        changed_source = _scope(GENERATED_CLASS, "process_success").source.replace(
            "stubShared();", "stubShared();\n        // source changed but failure did not"
        )
        result, _, repeated_outcome, _, repeated_gate, repeated_runner, repeated_llm, _ = self._run(
            GENERATED_CLASS,
            [True, True],
            [
                _failed(repeated, TestMethodResult("second_success", TestMethodStatus.PASSED)),
                _failed(repeated, TestMethodResult("second_success", TestMethodStatus.PASSED)),
            ],
            [changed_source],
        )
        self.assertIsNone(result)
        self.assertEqual(1, repeated_outcome.class_validation["execution_repair_rounds_attempted"])
        self.assertEqual(2, len(repeated_gate.calls))
        self.assertEqual(2, len(repeated_runner.calls))
        self.assertEqual(1, len(repeated_llm.calls))

    def test_18_execution_repair_compile_error_stays_in_same_execution_round(self):
        execution_repair = _scope(GENERATED_CLASS, "process_success").source.replace(
            "stubShared();", "stubShared(BAD);"
        )
        compile_repair = execution_repair.replace("stubShared(BAD);", "stubShared();\n        // compile correction")
        _, _, outcome, _, gate, runner, llm, _ = self._run(
            GENERATED_CLASS,
            [True, _compile_failure("cannot find symbol BAD", "stubShared(BAD)"), True],
            [
                _failed(_runtime_failure("process_success"), TestMethodResult("second_success", TestMethodStatus.PASSED)),
                _passed("process_success", "second_success"),
            ],
            [execution_repair, compile_repair],
        )
        self.assertEqual(1, outcome.class_validation["execution_repair_rounds_attempted"])
        self.assertEqual(0, outcome.class_validation["compilation_repair_rounds_attempted"])
        self.assertEqual(3, len(gate.calls))
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(2, len(llm.calls))
        self.assertTrue(outcome.class_validation["execution_repair_history"][0]["compilation_corrections"])

    def test_19_unresolved_tests_are_not_removed_or_commented_out(self):
        result, final_source, outcome, _, _, runner, _, _ = self._run(
            GENERATED_CLASS,
            [True],
            [_failed(_runtime_failure("process_success"), TestMethodResult("second_success", TestMethodStatus.PASSED))],
            ["void process_success() { }"],
        )
        self.assertIsNone(result)
        self.assertIn("@Test\n    void process_success()", final_source)
        self.assertIn("assertNotNull(response);", final_source)
        self.assertEqual("failing_artifact", outcome.class_validation["publication_result"])
        self.assertEqual(1, len(runner.calls))

    def test_20_normal_java_publication_requires_compile_and_all_tests_pass(self):
        result, _, outcome, _, _, _, _, _ = self._run(
            GENERATED_CLASS,
            [True],
            [_passed("process_success", "second_success")],
        )
        self.assertIsNotNone(result)
        self.assertEqual("normal_java", outcome.class_validation["publication_result"])
        self.assertEqual("passed", outcome.class_validation["final_complete_class_execution_result"])

    def test_21_failing_artifact_is_selected_and_written_when_issue_remains(self):
        engine, gate, runner, llm = _engine(
            [True],
            [_failed(_runtime_failure("process_success"), TestMethodResult("second_success", TestMethodStatus.PASSED))],
            [],
            execution_rounds=0,
        )
        outcome = _outcome()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "SampleServiceImplTest.java"
            path.write_text(GENERATED_CLASS, encoding="utf-8")
            module = ModuleInfo("sample", root, root / "pom.xml")
            result = engine._validate_serviceimpl_class(_context(), path, module, outcome)
            self.assertIsNone(result)
            engine._capture_failed_test_snapshot(path, outcome)
            self.assertTrue(path.with_name(path.name + ".failing").exists())
        self.assertEqual("failing_artifact", outcome.class_validation["publication_result"])

    def test_22_concise_report_explains_rounds_and_publication(self):
        _, _, outcome, _, _, _, _, _ = self._run(
            GENERATED_CLASS,
            [True],
            [_passed("process_success", "second_success")],
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, markdown = write_reports(root, root, StackProfile(), [outcome], timestamp="20260801T000000Z")
            class_report = root / "class-validation" / "sample_SampleServiceImplTest.md"
            self.assertTrue(class_report.exists())
            text = class_report.read_text(encoding="utf-8")
            self.assertIn("Initial compilation: passed", text)
            self.assertIn("Final complete-class execution: passed", text)
            self.assertIn("Publication: normal_java", text)
            self.assertNotIn("MAVEN COMPILER ERROR", text)
            self.assertTrue(markdown.exists())

    def test_23_non_serviceimpl_compile_behavior_remains_unchanged(self):
        engine, gate, runner, _ = _engine([True], [], [])
        ctx = _context()
        ctx.execution_context = None
        outcome = _outcome()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "UtilityTest.java"
            path.write_text("class UtilityTest { @Test void works() {} }", encoding="utf-8")
            module = ModuleInfo("sample", root, root / "pom.xml")
            result = engine._compile_with_repair(ctx, path, module, outcome)
        self.assertIsNotNone(result)
        self.assertEqual(1, len(gate.calls))
        self.assertEqual(0, len(runner.calls))


if __name__ == "__main__":
    unittest.main()
