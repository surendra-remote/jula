from __future__ import annotations

from pathlib import Path
import unittest

from junitforge.execution.analyzer import build_execution_context
from junitforge.execution.models import ExecutionTargetKind
from junitforge.models import (
    ClassSymbol,
    FieldSig,
    JavaSourceFile,
    MethodSig,
    TemplateSpec,
    TestKind,
)
from junitforge.postprocess import finalize_java
from junitforge.stack.classifier import classify_execution_target


def _getter(field_name: str, type_name: str) -> MethodSig:
    suffix = field_name[0].upper() + field_name[1:]
    return MethodSig(f"get{suffix}", {"public"}, type_name, [])


def _setter(field_name: str, type_name: str) -> MethodSig:
    suffix = field_name[0].upper() + field_name[1:]
    return MethodSig(f"set{suffix}", {"public"}, "void", [(type_name, field_name)])


def _dto(name: str, fields: list[tuple[str, str]]) -> ClassSymbol:
    field_sigs = [FieldSig(field_name, type_name, {"private"}) for field_name, type_name in fields]
    methods: list[MethodSig] = []
    for field_name, type_name in fields:
        methods.extend((_getter(field_name, type_name), _setter(field_name, type_name)))
    return ClassSymbol(
        name=name,
        fqcn=f"sample.{name}",
        kind="class",
        modifiers={"public"},
        package_name="sample",
        fields=field_sigs,
        methods=methods,
        constructors=[MethodSig(name, {"public"}, None, [], is_constructor=True)],
    )


class FluentNullAssertionRepairTest(unittest.TestCase):
    def test_default_null_assertion_is_repaired_with_verified_fluent_mutator(self) -> None:
        symbol = ClassSymbol(
            name="Channel",
            fqcn="sample.Channel",
            kind="class",
            modifiers={"public"},
            package_name="sample",
            fields=[FieldSig("policyPlanType", "String", {"private"})],
            methods=[
                MethodSig("getPolicyPlanType", {"public"}, "String", []),
                MethodSig(
                    "policyPlanType",
                    {"public"},
                    "Channel",
                    [("String", "policyPlanType")],
                ),
            ],
        )
        raw = """package sample;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class ChannelTest {
    @Test
    void policyPlanType_nullPath() {
        Channel channel = new Channel();
        assertNull(channel.getPolicyPlanType());
    }
}
"""

        result = finalize_java(
            raw,
            test_package="sample",
            test_class_name="ChannelTest",
            template=TemplateSpec(TestKind.PURE_UNIT, "dto-pojo"),
            cut_symbol=symbol,
        )

        self.assertTrue(result.ok, result.reason)
        self.assertIn("channel.policyPlanType(null);", result.code or "")
        self.assertNotIn("unsafe default-null", result.reason or "")

    def test_assertall_and_multiline_default_null_assertion_is_repaired(self) -> None:
        symbol = ClassSymbol(
            name="Channel",
            fqcn="sample.Channel",
            kind="class",
            modifiers={"public"},
            package_name="sample",
            fields=[FieldSig("policyPlanType", "String", {"private"})],
            methods=[
                MethodSig("getPolicyPlanType", {"public"}, "String", []),
                MethodSig("setPolicyPlanType", {"public"}, "void", [("String", "policyPlanType")]),
            ],
        )
        raw = """package sample;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class ChannelTest {
    @Test
    void policyPlanType_nullPath() {
        Channel channel = new Channel();
        assertAll(
            () -> assertNull(
                channel.getPolicyPlanType(),
                "policy plan type should be null"
            )
        );
    }
}
"""
        result = finalize_java(
            raw,
            test_package="sample",
            test_class_name="ChannelTest",
            template=TemplateSpec(TestKind.PURE_UNIT, "dto-pojo"),
            cut_symbol=symbol,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertIn("channel.setPolicyPlanType(null);", result.code or "")

    def test_explicit_fluent_null_assignment_is_accepted(self) -> None:
        symbol = ClassSymbol(
            name="Channel",
            fqcn="sample.Channel",
            kind="class",
            modifiers={"public"},
            package_name="sample",
            fields=[FieldSig("policyPlanType", "String", {"private"})],
            methods=[
                MethodSig("getPolicyPlanType", {"public"}, "String", []),
                MethodSig("policyPlanType", {"public"}, "Channel", [("String", "policyPlanType")]),
            ],
        )
        raw = """package sample;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class ChannelTest {
    @Test
    void policyPlanType_nullPath() {
        Channel channel = new Channel();
        channel.policyPlanType(null);
        assertNull(channel.getPolicyPlanType());
    }
}
"""
        result = finalize_java(
            raw,
            test_package="sample",
            test_class_name="ChannelTest",
            template=TemplateSpec(TestKind.PURE_UNIT, "dto-pojo"),
            cut_symbol=symbol,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(1, (result.code or "").count("channel.policyPlanType(null);"))


class GeneralizedStructuredFixtureContextTest(unittest.TestCase):
    def test_static_utility_method_receives_recursive_parameter_fixtures(self) -> None:
        policy = _dto("PolicyDto", [("policyNo", "String")])
        request = _dto("ValidationRequest", [("policy", "PolicyDto")])
        utility_method = MethodSig(
            "validate",
            {"public", "static"},
            "boolean",
            [("ValidationRequest", "request")],
            line=3,
            end_line=5,
        )
        utility = ClassSymbol(
            name="PolicyValidationUtils",
            fqcn="sample.PolicyValidationUtils",
            kind="class",
            modifiers={"public", "final"},
            package_name="sample",
            methods=[utility_method],
        )
        source = """package sample;
public final class PolicyValidationUtils {
    public static boolean validate(ValidationRequest request) {
        return SecondaryValidationUtils.validate(request);
    }
}
"""
        source_file = JavaSourceFile(
            path=Path("PolicyValidationUtils.java"),
            package="sample",
            types=[utility],
            source=source,
        )
        symbols = {
            utility.name: utility,
            utility.fqcn: utility,
            request.name: request,
            request.fqcn: request,
            policy.name: policy,
            policy.fqcn: policy,
        }

        def lookup(type_name: str, owner=None):
            simple = type_name.split("<", 1)[0].replace("[]", "").rsplit(".", 1)[-1]
            return symbols.get(type_name) or symbols.get(simple)

        target_kind = classify_execution_target(utility)
        self.assertEqual(ExecutionTargetKind.UTILITY, target_kind)

        context = build_execution_context(
            source_file=source_file,
            symbol=utility,
            lookup=lookup,
            target_kind=target_kind,
            schema_lookup=lookup,
        )

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(["validate(ValidationRequest)"], [method.method_id for method in context.methods])
        self.assertEqual((), context.methods[0].reachable_private_methods,
                         "class-qualified calls on another utility must not become self-helper edges")
        self.assertEqual(
            {"ValidationRequest", "PolicyDto"},
            {schema.type_name for schema in context.payload_schemas},
        )
        supported_fixture_names = {
            fixture.method_name for fixture in context.fixtures if fixture.supported
        }
        self.assertIn("validValidationRequest", supported_fixture_names)
        self.assertIn("validPolicyDto", supported_fixture_names)
        request_fixture = next(
            fixture.method_source
            for fixture in context.fixtures
            if fixture.method_name == "validValidationRequest"
        )
        self.assertIn("setPolicy(validPolicyDto())", request_fixture)


    def test_classwide_utility_output_receives_deterministic_fixture_methods(self) -> None:
        from junitforge.execution.assembly import inject_reusable_fixtures
        from junitforge.execution.models import (
            ExecutionContext, FixtureSpec, MethodExecutionContext,
        )
        from junitforge.models import (
            ClasspathProfile, GenerationContext, StackProfile,
        )

        method = MethodSig(
            "validate", {"public", "static"}, "boolean",
            [("ValidationRequest", "request")],
        )
        fixture = FixtureSpec(
            fixture_id="sample.ValidationRequest",
            method_name="validValidationRequest",
            return_type="ValidationRequest",
            schema_id="sample.ValidationRequest",
            imports=("java.util.ArrayList",),
            method_source=(
                "    private ValidationRequest validValidationRequest() {\n"
                "        ValidationRequest value = new ValidationRequest();\n"
                "        return value;\n"
                "    }"
            ),
        )
        method_context = MethodExecutionContext(
            method_id="validate(ValidationRequest)",
            signature=method.render(),
            source_range=(1, 1),
            method_source="public static boolean validate(ValidationRequest request) { return true; }",
            endpoint=None,
            required_fixture_ids=("sample.ValidationRequest",),
        )
        execution = ExecutionContext(
            target_kind=ExecutionTargetKind.UTILITY,
            target_fqcn="sample.PolicyValidationUtils",
            methods=(method_context,),
            fixtures=(fixture,),
        )
        symbol = ClassSymbol(
            "PolicyValidationUtils", "sample.PolicyValidationUtils", "class",
            modifiers={"public", "final"}, methods=[method],
        )
        ctx = GenerationContext(
            cut_source="", symbol=symbol, collaborators=[], test_package="sample",
            test_class_name="PolicyValidationUtilsTest", stack=StackProfile(),
            classpath=ClasspathProfile(), template=TemplateSpec(TestKind.PURE_UNIT, "utility-unit"),
            source_path=Path("PolicyValidationUtils.java"), execution_context=execution,
        )
        llm_output = """package sample;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class PolicyValidationUtilsTest {
    @Test
    void validates() {
        assertTrue(PolicyValidationUtils.validate(validValidationRequest()));
    }
}
"""

        injected = inject_reusable_fixtures(llm_output, ctx)

        self.assertIn("private ValidationRequest validValidationRequest()", injected)
        self.assertEqual(
            2, injected.count("validValidationRequest("),
            "one test call plus one deterministic helper declaration are expected",
        )
        self.assertIn("import java.util.ArrayList;", injected)

    def test_validator_and_mapper_are_not_blocked_by_serviceimpl_gate(self) -> None:
        structured_method = MethodSig(
            "apply",
            {"public"},
            "boolean",
            [("ValidationRequest", "request")],
        )
        validator = ClassSymbol(
            "RequestValidator",
            "sample.RequestValidator",
            "class",
            modifiers={"public"},
            methods=[structured_method],
        )
        mapper = ClassSymbol(
            "RequestMapper",
            "sample.RequestMapper",
            "class",
            modifiers={"public"},
            methods=[structured_method],
        )
        self.assertEqual(ExecutionTargetKind.VALIDATOR, classify_execution_target(validator))
        self.assertEqual(ExecutionTargetKind.MAPPER, classify_execution_target(mapper))

    def test_static_utility_entry_propagates_private_static_helper(self) -> None:
        entry = MethodSig(
            "validate", {"public", "static"}, "boolean", [("String", "value")],
            line=3, end_line=3,
            body_facts={
                "methodCalls": [{
                    "receiverKind": "this", "scope": None, "name": "normalize",
                    "arguments": ["value"],
                }],
                "branches": [], "memberReads": [], "nullGuards": [], "exits": [],
            },
        )
        helper = MethodSig(
            "normalize", {"private", "static"}, "String", [("String", "value")],
            line=4, end_line=4,
            body_facts={
                "methodCalls": [], "branches": [], "memberReads": [],
                "nullGuards": [], "exits": [],
            },
        )
        utility = ClassSymbol(
            "ValueUtils", "sample.ValueUtils", "class",
            modifiers={"public", "final"}, package_name="sample",
            methods=[entry, helper],
        )
        source = """package sample;
public final class ValueUtils {
    public static boolean validate(String value) { return normalize(value) != null; }
    private static String normalize(String value) { return value; }
}
"""
        context = build_execution_context(
            source_file=JavaSourceFile(
                path=Path("ValueUtils.java"), package="sample", types=[utility], source=source,
            ),
            symbol=utility,
            lookup=lambda type_name, owner=None: None,
            target_kind=ExecutionTargetKind.UTILITY,
        )
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(("normalize(String)",), context.methods[0].reachable_private_methods)

    def test_untyped_plain_class_with_structured_parameter_uses_general_context(self) -> None:
        processor = ClassSymbol(
            "RenewalDecision",
            "sample.RenewalDecision",
            "class",
            modifiers={"public"},
            methods=[MethodSig("decide", {"public"}, "boolean", [("PolicyDto", "policy")])],
        )
        self.assertEqual(
            ExecutionTargetKind.STRUCTURED_UNIT,
            classify_execution_target(processor),
        )


if __name__ == "__main__":
    unittest.main()
