from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from junitforge.execution.analyzer import build_execution_context
from junitforge.execution.assembly import build_test_skeleton, validate_method_block
from junitforge.execution.models import BranchFact, ExecutionTargetKind, ResolutionStatus
from junitforge.execution.payload_schema import apply_selected_string_branch_constraints
from junitforge.execution.service_contracts import resolve_service_entries
from junitforge.models import (
    ClassSymbol,
    ClasspathProfile,
    FieldSig,
    GenerationContext,
    JavaSourceFile,
    MethodSig,
    StackProfile,
    TemplateSpec,
    TestKind as ModelTestKind,
)
from junitforge.prompts.generation import build_method_generation_messages


def _getter(field_name: str, type_name: str) -> MethodSig:
    suffix = field_name[:1].upper() + field_name[1:]
    return MethodSig(f"get{suffix}", modifiers={"public"}, return_type=type_name)


def _setter(field_name: str, type_name: str) -> MethodSig:
    suffix = field_name[:1].upper() + field_name[1:]
    return MethodSig(
        f"set{suffix}",
        modifiers={"public"},
        return_type="void",
        params=[(type_name, field_name)],
    )


def _dto(name: str, fields: list[tuple[str, str]]) -> ClassSymbol:
    methods = []
    for field_name, type_name in fields:
        methods.extend((_getter(field_name, type_name), _setter(field_name, type_name)))
    return ClassSymbol(
        name,
        f"sample.{name}",
        "class",
        modifiers={"public"},
        package_name="sample",
        fields=[FieldSig(field_name, type_name, modifiers={"private"}) for field_name, type_name in fields],
        methods=methods,
    )


class ServiceImplPrimaryGenerationRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = """package sample;
public class PolicyServiceImpl implements ChildService<Policy> {
    private MotorClient motorClient;
    private HomeClient homeClient;
    private AuditClient auditClient;

    public Response processMotor(Policy policy) {
        if (policy.getMotors().isEmpty()) {
            return new Response();
        } else {
            if ("MOTOR".equals(policy.getPerson().getName())) {
                return loadMotor(policy);
            }
            return new Response();
        }
    }

    public Response processHome(Policy policy) {
        if ("HOME".equals(policy.getPerson().getName())) {
            return homeClient.load(policy.getHomes().get(0));
        }
        return new Response();
    }

    public Response loadMotor(Policy policy) {
        Motor motor = policy.getMotors().get(0);
        auditClient.record(motor);
        return motorClient.load(motor);
    }
}
"""
        lines = cls.source.splitlines()

        def line_of(fragment: str) -> int:
            return next(index for index, line in enumerate(lines, 1) if fragment in line)

        process_motor = MethodSig(
            "processMotor",
            modifiers={"public"},
            return_type="Response",
            params=[("Policy", "policy")],
            line=line_of("processMotor("),
            end_line=line_of("public Response processHome") - 2,
        )
        process_home = MethodSig(
            "processHome",
            modifiers={"public"},
            return_type="Response",
            params=[("Policy", "policy")],
            line=line_of("processHome("),
            end_line=line_of("public Response loadMotor") - 2,
        )
        load_motor = MethodSig(
            "loadMotor",
            modifiers={"public"},
            annotations=["Override"],
            return_type="Response",
            params=[("Policy", "policy")],
            line=line_of("loadMotor(Policy"),
            end_line=len(lines) - 1,
        )
        cls.cut = ClassSymbol(
            "PolicyServiceImpl",
            "sample.PolicyServiceImpl",
            "class",
            modifiers={"public"},
            package_name="sample",
            implements=["ChildService<Policy>"],
            fields=[
                FieldSig("motorClient", "MotorClient", modifiers={"private"}),
                FieldSig("homeClient", "HomeClient", modifiers={"private"}),
                FieldSig("auditClient", "AuditClient", modifiers={"private"}),
            ],
            methods=[process_motor, process_home, load_motor],
        )

        cls.policy = _dto(
            "Policy",
            [
                ("person", "Person"),
                ("motors", "List<Motor>"),
                ("homes", "List<Home>"),
                ("travels", "List<Travel>"),
            ],
        )
        cls.person = _dto("Person", [("name", "String"), ("unusedAlias", "String")])
        cls.motor = _dto("Motor", [("registrationNo", "String")])
        cls.home = _dto("Home", [("riskAddress", "String")])
        cls.travel = _dto("Travel", [("destination", "String")])
        cls.response = _dto("Response", [("status", "String")])
        cls.motor_client = ClassSymbol(
            "MotorClient",
            "sample.MotorClient",
            "interface",
            package_name="sample",
            methods=[MethodSig(
                "load",
                modifiers={"public", "abstract"},
                return_type="Response",
                params=[("Motor", "motor")],
            )],
        )
        cls.home_client = ClassSymbol(
            "HomeClient",
            "sample.HomeClient",
            "interface",
            package_name="sample",
            methods=[MethodSig(
                "load",
                modifiers={"public", "abstract"},
                return_type="Response",
                params=[("Home", "home")],
            )],
        )
        cls.audit_client = ClassSymbol(
            "AuditClient",
            "sample.AuditClient",
            "interface",
            package_name="sample",
            methods=[MethodSig(
                "record",
                modifiers={"public", "abstract"},
                return_type="void",
                params=[("Motor", "motor")],
            )],
        )
        cls.parent_service = ClassSymbol(
            "ParentService",
            "sample.ParentService",
            "interface",
            package_name="sample",
            type_parameters=["T"],
            methods=[MethodSig(
                "processMotor",
                modifiers={"public", "abstract"},
                return_type="Response",
                params=[("T", "policy")],
            )],
        )
        cls.child_service = ClassSymbol(
            "ChildService",
            "sample.ChildService",
            "interface",
            package_name="sample",
            type_parameters=["T"],
            extends="ParentService<T>",
            extends_types=["ParentService<T>"],
            methods=[MethodSig(
                "processHome",
                modifiers={"public", "abstract"},
                return_type="Response",
                params=[("T", "policy")],
            )],
        )
        all_symbols = (
            cls.cut,
            cls.policy,
            cls.person,
            cls.motor,
            cls.home,
            cls.travel,
            cls.response,
            cls.motor_client,
            cls.home_client,
            cls.audit_client,
            cls.parent_service,
            cls.child_service,
        )
        cls.symbols = {
            key: symbol
            for symbol in all_symbols
            for key in (symbol.name, symbol.fqcn)
        }

        def lookup(type_name: str, owner=None):
            raw = re.sub(r"<.*>", "", type_name or "").replace("[]", "").replace("...", "").strip()
            return cls.symbols.get(raw) or cls.symbols.get(raw.rsplit(".", 1)[-1])

        cls.lookup = staticmethod(lookup)
        source_file = JavaSourceFile(
            path=Path("PolicyServiceImpl.java"),
            package="sample",
            imports=["java.util.List"],
            types=[cls.cut],
            source=cls.source,
        )
        cls.execution = build_execution_context(
            source_file=source_file,
            symbol=cls.cut,
            lookup=lookup,
            schema_lookup=lookup,
            target_kind=ExecutionTargetKind.SERVICE_IMPL,
        )
        assert cls.execution is not None
        cls.ctx = GenerationContext(
            cut_source=cls.source,
            symbol=cls.cut,
            collaborators=[],
            test_package="sample",
            test_class_name="PolicyServiceImplTest",
            stack=StackProfile(java_version="17"),
            classpath=ClasspathProfile(has_junit_jupiter=True, has_mockito=True),
            template=TemplateSpec(ModelTestKind.PURE_UNIT, "unit"),
            source_path=Path("PolicyServiceImpl.java"),
            source_imports=["java.util.List"],
            execution_context=cls.execution,
        )

    def method_context(self, name: str):
        return next(method for method in self.execution.methods if method.method_id.startswith(name + "("))

    def production_method(self, name: str) -> MethodSig:
        return next(method for method in self.cut.methods if method.name == name)

    def fixture(self, simple_name: str):
        return next(
            fixture for fixture in self.execution.fixtures
            if fixture.fixture_id == f"sample.{simple_name}"
        )

    def test_01_exactly_one_entry_context_per_inherited_service_contract(self) -> None:
        self.assertEqual(
            ["processMotor(Policy)", "processHome(Policy)"],
            [method.method_id for method in self.execution.methods],
        )
        resolved = [
            contract for contract in self.execution.service_contracts
            if contract.resolution_status == ResolutionStatus.RESOLVED
        ]
        self.assertEqual(2, len(resolved))
        self.assertTrue(all(contract.normalized_signature for contract in resolved))

    def test_02_public_helper_is_not_an_entry_and_unresolved_contracts_are_reported(self) -> None:
        self.assertNotIn("loadMotor(Policy)", {method.method_id for method in self.execution.methods})
        broken = ClassSymbol(
            "BrokenServiceImpl",
            "sample.BrokenServiceImpl",
            "class",
            modifiers={"public"},
            implements=["MissingService"],
            methods=[MethodSig("publicHelper", modifiers={"public"}, return_type="Response")],
        )
        resolution = resolve_service_entries(broken, self.lookup)
        self.assertEqual((), resolution.matched_methods)
        self.assertTrue(any("Service interface unresolved" in value for value in resolution.diagnostics))
        unresolved_interface = ClassSymbol(
            "UnresolvedService",
            "sample.UnresolvedService",
            "interface",
            methods=[MethodSig(
                "process",
                modifiers={"public", "abstract"},
                return_type="Response",
                params=[("MissingRequest", "request")],
            )],
        )
        unresolved_impl = ClassSymbol(
            "UnresolvedServiceImpl",
            "sample.UnresolvedServiceImpl",
            "class",
            modifiers={"public"},
            implements=["UnresolvedService"],
            methods=[MethodSig(
                "process",
                modifiers={"public"},
                return_type="Response",
                params=[("MissingRequest", "request")],
            )],
        )
        symbols = {**self.symbols, "UnresolvedService": unresolved_interface}

        def lookup(type_name: str, owner=None):
            raw = re.sub(r"<.*>", "", type_name or "").replace("[]", "").replace("...", "").strip()
            return symbols.get(raw) or symbols.get(raw.rsplit(".", 1)[-1])

        unresolved = resolve_service_entries(unresolved_impl, lookup)
        self.assertEqual((), unresolved.matched_methods)
        self.assertTrue(any("parameter type unresolved" in value for value in unresolved.diagnostics))

    def test_03_varargs_are_normalized_and_longest_success_path_is_selected(self) -> None:
        vararg_interface = ClassSymbol(
            "NotificationService",
            "sample.NotificationService",
            "interface",
            methods=[MethodSig(
                "notify",
                modifiers={"public", "abstract"},
                return_type="Response",
                params=[("String...", "recipients")],
            )],
        )
        vararg_impl_method = MethodSig(
            "notify", modifiers={"public"}, return_type="Response", params=[("String[]", "recipients")]
        )
        vararg_impl = ClassSymbol(
            "NotificationServiceImpl",
            "sample.NotificationServiceImpl",
            "class",
            modifiers={"public"},
            implements=["NotificationService"],
            methods=[vararg_impl_method],
        )
        symbols = {**self.symbols, "NotificationService": vararg_interface}

        def lookup(type_name: str, owner=None):
            raw = re.sub(r"<.*>", "", type_name or "").replace("[]", "").replace("...", "").strip()
            return symbols.get(raw) or symbols.get(raw.rsplit(".", 1)[-1])

        resolution = resolve_service_entries(vararg_impl, lookup)
        self.assertEqual((vararg_impl_method,), resolution.matched_methods)
        motor_context = self.method_context("processMotor")
        choices = {choice.branch_id: choice.arm for choice in motor_context.selected_primary_path.branch_choices}
        self.assertIn("false", choices.values())
        self.assertIn("loadMotor(Policy)", motor_context.selected_primary_path.reachable_method_ids)
        person_schema = next(schema for schema in motor_context.payload_schemas if schema.type_name == "Person")
        constrained, diagnostics = apply_selected_string_branch_constraints(
            [person_schema],
            [BranchFact(
                branch_id="guard",
                owner_method_id="processMotor(Policy)",
                kind="if",
                condition='"ERROR".equals(policy.getPerson().getName())',
            )],
            {"guard": "false"},
        )
        name_property = next(prop for prop in constrained[0].properties if prop.name == "name")
        self.assertEqual('"A"', name_property.baseline_value)
        self.assertTrue(any("non-equal String witness" in value for value in diagnostics))

    def test_04_same_root_type_reuses_one_root_fixture_definition(self) -> None:
        policy_fixtures = [
            fixture for fixture in self.execution.fixtures
            if fixture.fixture_id == "sample.Policy"
        ]
        self.assertEqual(1, len(policy_fixtures))
        self.assertEqual(1, policy_fixtures[0].method_source.count("private Policy validPolicy("))

    def test_05_shared_nested_type_reuses_one_nested_helper(self) -> None:
        person_fixtures = [
            fixture for fixture in self.execution.fixtures
            if fixture.fixture_id == "sample.Person"
        ]
        self.assertEqual(1, len(person_fixtures))
        self.assertEqual(1, person_fixtures[0].method_source.count("private Person validPerson("))

    def test_06_projection_helpers_create_fresh_mutable_instances(self) -> None:
        policy_source = self.fixture("Policy").method_source
        self.assertIn("Policy policy = new Policy();", policy_source)
        self.assertIn("new ArrayList<>(List.of(validMotor(", policy_source)
        self.assertNotIn("static final Policy", policy_source)
        self.assertNotIn("static final List", policy_source)
        fixture_sources = "\n\n".join(
            fixture.method_source
            for fixture in self.execution.fixtures
            if fixture.supported and fixture.method_source
        )
        java_source = f"""import java.util.ArrayList;
import java.util.List;

class ProjectionFixtureCompileTest {{
    static class Policy {{
        void setPerson(Person value) {{}}
        void setMotors(List<Motor> value) {{}}
        void setHomes(List<Home> value) {{}}
    }}
    static class Person {{ void setName(String value) {{}} }}
    static class Motor {{}}
    static class Home {{}}
    static class Response {{}}

{fixture_sources}
}}
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ProjectionFixtureCompileTest.java"
            path.write_text(java_source, encoding="utf-8")
            result = subprocess.run(["javac", str(path)], capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr + "\n" + java_source)

    def test_07_unused_nested_association_and_fields_are_excluded(self) -> None:
        policy_schema = next(schema for schema in self.execution.payload_schemas if schema.type_name == "Policy")
        self.assertEqual({"person", "motors", "homes"}, {prop.name for prop in policy_schema.properties})
        self.assertNotIn("Travel", {schema.type_name for schema in self.execution.payload_schemas})
        person_schema = next(schema for schema in self.execution.payload_schemas if schema.type_name == "Person")
        self.assertEqual({"name"}, {prop.name for prop in person_schema.properties})

    def test_08_method_specific_slices_contain_only_required_associations(self) -> None:
        motor_projection = next(
            projection for projection in self.method_context("processMotor").fixture_projections
            if projection.fixture_id == "sample.Policy"
        )
        home_projection = next(
            projection for projection in self.method_context("processHome").fixture_projections
            if projection.fixture_id == "sample.Policy"
        )
        self.assertIn("person.name", motor_projection.property_paths)
        self.assertIn("motors", motor_projection.property_paths)
        self.assertNotIn("homes", motor_projection.property_paths)
        self.assertIn("person.name", home_projection.property_paths)
        self.assertIn("homes", home_projection.property_paths)
        self.assertNotIn("motors", home_projection.property_paths)
        self.assertTrue(motor_projection.call_expression.startswith('validPolicy("processMotor(Policy)",'))
        self.assertTrue(home_projection.call_expression.startswith('validPolicy("processHome(Policy)",'))
        person_source = self.fixture("Person").method_source
        self.assertIn('"processMotor(Policy)".equals(fixtureScenario) ? "MOTOR"', person_source)
        self.assertIn('"processHome(Policy)".equals(fixtureScenario) ? "HOME"', person_source)

    def test_09_selected_collection_has_a_branch_compatible_element(self) -> None:
        policy_source = self.fixture("Policy").method_source
        self.assertRegex(
            policy_source,
            r"setMotors\(new ArrayList<>\(List\.of\(validMotor\(fixtureScenario, fixtureChildPaths\(requiredPaths, \"motors\"\)\)\)\)\)",
        )
        motor_schema = next(schema for schema in self.execution.payload_schemas if schema.type_name == "Policy")
        motors = next(prop for prop in motor_schema.properties if prop.name == "motors")
        self.assertGreaterEqual(motors.minimum_size, 1)

    def test_10_mocks_are_declared_once_and_stubs_remain_method_specific(self) -> None:
        skeleton = build_test_skeleton(self.ctx)
        self.assertEqual(1, skeleton.count("private MotorClient motorClient;"))
        self.assertEqual(1, skeleton.count("private HomeClient homeClient;"))
        self.assertEqual(1, skeleton.count("private AuditClient auditClient;"))
        self.assertNotIn("when(", skeleton)
        motor_prompt = build_method_generation_messages(
            self.ctx, self.production_method("processMotor"), skeleton
        )[-1]["content"]
        home_prompt = build_method_generation_messages(
            self.ctx, self.production_method("processHome"), skeleton
        )[-1]["content"]
        self.assertIn("motorClient.load", motor_prompt)
        self.assertIn("auditClient.record", motor_prompt)
        self.assertIn("doNothing().when", motor_prompt)
        self.assertNotIn("homeClient.load", motor_prompt)
        self.assertIn("homeClient.load", home_prompt)
        self.assertNotIn("motorClient.load", home_prompt)
        self.assertNotIn("auditClient.record", home_prompt)

    def test_11_helper_origin_collaborator_is_in_the_entry_stub_plan(self) -> None:
        motor_context = self.method_context("processMotor")
        invocation = next(
            invocation for invocation in motor_context.dependency_invocations
            if invocation.dependency_field == "motorClient"
        )
        self.assertEqual("loadMotor(Policy)", invocation.caller_method_id)
        block = """@Test
        void processMotor_primarySuccess() {
            Policy policy = validPolicy("processMotor(Policy)", "person.name", "motors");
            doNothing().when(auditClient).record(any(Motor.class));
            when(motorClient.load(any(Motor.class))).thenReturn(validResponse("processMotor(Policy)"));
            Response response = policyServiceImpl.processMotor(policy);
            assertNotNull(response);
        }"""
        validation = validate_method_block(
            self.ctx,
            self.production_method("processMotor"),
            motor_context,
            block,
            set(),
        )
        self.assertTrue(validation.ok, validation.reason)
        missing_void_stub = block.replace(
            "            doNothing().when(auditClient).record(any(Motor.class));\n",
            "",
        )
        self.assertFalse(validate_method_block(
            self.ctx,
            self.production_method("processMotor"),
            motor_context,
            missing_void_stub,
            set(),
        ).ok)
        wrong_non_void_syntax = block.replace(
            'when(motorClient.load(any(Motor.class))).thenReturn(validResponse("processMotor(Policy)"));',
            'doNothing().when(motorClient).load(any(Motor.class));',
        )
        self.assertFalse(validate_method_block(
            self.ctx,
            self.production_method("processMotor"),
            motor_context,
            wrong_non_void_syntax,
            set(),
        ).ok)

    def test_12_primary_service_test_enforces_one_assert_not_null_only(self) -> None:
        method = self.production_method("processHome")
        method_context = self.method_context("processHome")
        valid = """@Test
        void processHome_primarySuccess() {
            Policy policy = validPolicy("processHome(Policy)", "person.name", "homes");
            when(homeClient.load(any(Home.class))).thenReturn(validResponse("processHome(Policy)"));
            Response response = policyServiceImpl.processHome(policy);
            assertNotNull(response);
        }"""
        self.assertTrue(validate_method_block(self.ctx, method, method_context, valid, set()).ok)
        extra_assertion = valid.replace(
            "assertNotNull(response);",
            "assertNotNull(response);\n            assertEquals(\"SUCCESS\", response.getStatus());",
        )
        rejected = validate_method_block(self.ctx, method, method_context, extra_assertion, set())
        self.assertFalse(rejected.ok)
        two_tests = valid + "\n\n" + valid.replace("processHome_primarySuccess", "processHome_second")
        rejected = validate_method_block(self.ctx, method, method_context, two_tests, set())
        self.assertFalse(rejected.ok)
        parameterized = valid + "\n\n@ParameterizedTest\nvoid extra(String value) {}"
        self.assertFalse(validate_method_block(
            self.ctx, method, method_context, parameterized, set()
        ).ok)
        prompt = build_method_generation_messages(self.ctx, method, "")[-1]["content"]
        self.assertIn("exactly one initial @Test", prompt)
        self.assertIn("assertNotNull(response)", prompt)
        self.assertNotIn("Include the normal success path and all source-proven null/empty/error paths", prompt)

    def test_13_controller_method_selection_and_assertion_rules_are_unchanged(self) -> None:
        controller_source = """package sample;
public class SampleController {
    public Response first(Policy policy) { return new Response(); }
    public Response second(Policy policy) { return new Response(); }
}
"""
        methods = [
            MethodSig("first", modifiers={"public"}, return_type="Response", params=[("Policy", "policy")], line=3),
            MethodSig("second", modifiers={"public"}, return_type="Response", params=[("Policy", "policy")], line=4),
        ]
        controller = ClassSymbol(
            "SampleController",
            "sample.SampleController",
            "class",
            modifiers={"public"},
            package_name="sample",
            methods=methods,
        )
        context = build_execution_context(
            source_file=JavaSourceFile(
                path=Path("SampleController.java"),
                package="sample",
                types=[controller],
                source=controller_source,
            ),
            symbol=controller,
            lookup=self.lookup,
            schema_lookup=self.lookup,
            target_kind=ExecutionTargetKind.CONTROLLER,
        )
        self.assertIsNotNone(context)
        self.assertEqual(2, len(context.methods))
        controller_ctx = GenerationContext(
            cut_source=controller_source,
            symbol=controller,
            collaborators=[],
            test_package="sample",
            test_class_name="SampleControllerTest",
            stack=StackProfile(java_version="17"),
            classpath=ClasspathProfile(has_junit_jupiter=True, has_mockito=True),
            template=TemplateSpec(ModelTestKind.PURE_UNIT, "unit"),
            source_path=Path("SampleController.java"),
            execution_context=context,
        )
        prompt = build_method_generation_messages(controller_ctx, methods[0], "")[-1]["content"]
        self.assertIn("Generate all compile-safe JUnit 5 @Test methods", prompt)
        self.assertIn("every meaningful if/else arm", prompt)


if __name__ == "__main__":
    unittest.main()
