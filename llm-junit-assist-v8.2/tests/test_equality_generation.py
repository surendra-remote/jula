from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from junitforge.context import build_context
from junitforge.models import (
    ClasspathProfile,
    EqualityAssertionMode,
    EqualityStrategy,
    GenerationContext,
    StackProfile,
    TemplateSpec,
    TestKind as JunitTestKind,
)
from junitforge.parser.java_symbols import parse_source
from junitforge.postprocess import finalize_java
from junitforge.prompts.generation import (
    _global_rules_block,
    _object_contract_block,
)
from junitforge.stack.classifier import classify


def _parsed(source: str, name: str):
    parsed = parse_source(Path(f"{name}.java"), source)
    assert parsed.primary_type is not None
    return parsed, parsed.primary_type


def _context(
    source: str,
    name: str,
    *,
    style: str = "dto-pojo",
    lookup=None,
) -> GenerationContext:
    source_file, symbol = _parsed(source, name)
    return build_context(
        source_file=source_file,
        symbol=symbol,
        collaborators=[],
        test_package="sample",
        test_class_name=f"{name}Test",
        stack=StackProfile(java_version="17"),
        classpath=ClasspathProfile(has_junit_jupiter=True),
        template=TemplateSpec(JunitTestKind.PURE_UNIT, style),
        source_path=Path(f"{name}.java"),
        symbol_lookup=lookup,
    )


def _lookup_for(*symbols):
    by_name = {}
    for symbol in symbols:
        by_name[symbol.name] = symbol
        by_name[symbol.fqcn] = symbol

    def lookup(type_name: str, owner=None):  # noqa: ARG001
        simple = type_name.split("<", 1)[0].replace("[]", "").rsplit(".", 1)[-1]
        return by_name.get(type_name) or by_name.get(simple)

    return lookup


class EqualityGenerationRegressionTest(unittest.TestCase):
    def test_01_identity_equality_does_not_receive_value_template(self) -> None:
        source = """package sample;
public class IdentityDto {
    public IdentityDto() {}
}
"""
        ctx = _context(source, "IdentityDto")
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertEqual(EqualityStrategy.OBJECT_IDENTITY, descriptor.strategy)
        self.assertEqual(EqualityAssertionMode.IDENTITY, descriptor.assertion_mode)
        contract = _object_contract_block(ctx)
        self.assertIn("Generate identity assertions only", contract)
        self.assertNotIn("Required assertions for the equal pair", contract)

        raw = """package sample;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class IdentityDtoTest {
    @Test
    void equalityContract() {
        IdentityDto first = new IdentityDto();
        IdentityDto second = new IdentityDto();
        assertEquals(first, second);
        assertNotEquals(first, second);
        assertNotEquals(first.hashCode(), second.hashCode());
    }
}
"""
        result = finalize_java(
            raw,
            test_package="sample",
            test_class_name="IdentityDtoTest",
            template=TemplateSpec(JunitTestKind.PURE_UNIT, "dto-pojo"),
            cut_symbol=ctx.symbol,
        )
        self.assertTrue(result.ok, result.reason)
        code = result.code or ""
        self.assertNotIn("assertEquals(first, second)", code)
        self.assertIn("assertNotEquals(first, second)", code)
        self.assertNotIn("assertNotEquals(first.hashCode()", code)

    def test_02_equals_and_hashcode_uses_only_participating_fields(self) -> None:
        source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
@EqualsAndHashCode
public class ContractDto {
    private String code;
    private transient String cache;
    private static String TYPE;
}
"""
        descriptor = _context(source, "ContractDto").equality_descriptor
        assert descriptor is not None
        self.assertEqual(
            EqualityStrategy.LOMBOK_EQUALS_HASHCODE, descriptor.strategy
        )
        self.assertEqual(["code"], [member.name for member in descriptor.members])
        self.assertEqual("code", descriptor.difference_member)

    def test_03_data_equality_is_recognized(self) -> None:
        source = """package sample;
import lombok.Data;
@Data
public class DataDto {
    private String code;
}
"""
        descriptor = _context(source, "DataDto").equality_descriptor
        assert descriptor is not None
        self.assertEqual(EqualityStrategy.LOMBOK_DATA, descriptor.strategy)
        self.assertEqual(EqualityAssertionMode.VALUE, descriptor.assertion_mode)

    def test_04_value_equality_is_recognized(self) -> None:
        source = """package sample;
import lombok.Value;
@Value
public class ValueDto {
    String code;
}
"""
        ctx = _context(source, "ValueDto")
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertEqual(EqualityStrategy.LOMBOK_VALUE, descriptor.strategy)
        self.assertEqual(["code"], [member.name for member in descriptor.members])
        self.assertIn(
            "class-level Lombok @Value metadata verifies",
            _object_contract_block(ctx),
        )

    def test_05_record_equality_uses_record_components(self) -> None:
        source = """package sample;
public record PolicyRecord(String policyNo, int version) {}
"""
        ctx = _context(source, "PolicyRecord")
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertEqual("record", ctx.symbol.kind)
        self.assertEqual(
            "dto-pojo",
            classify(
                ctx.symbol,
                StackProfile(java_version="17"),
                ClasspathProfile(has_junit_jupiter=True),
            ).style,
        )
        self.assertEqual(EqualityStrategy.RECORD, descriptor.strategy)
        self.assertEqual(
            ["policyNo", "version"],
            [member.name for member in descriptor.members],
        )
        self.assertIn("canonical record constructor", _object_contract_block(ctx))

    def test_06_excluded_field_is_not_an_inequality_member(self) -> None:
        source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
@EqualsAndHashCode
public class ExcludedDto {
    private String code;
    @EqualsAndHashCode.Exclude
    private String displayName;
}
"""
        ctx = _context(source, "ExcludedDto")
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertEqual(["code"], [member.name for member in descriptor.members])
        self.assertNotEqual("displayName", descriptor.difference_member)
        self.assertIn(
            "Members that must not be used to create inequality: displayName",
            _object_contract_block(ctx),
        )

    def test_07_only_explicitly_included_uses_only_include_members(self) -> None:
        source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
@EqualsAndHashCode(onlyExplicitlyIncluded = true)
public class ExplicitDto {
    @EqualsAndHashCode.Include
    private String businessKey;
    private String description;
}
"""
        ctx = _context(source, "ExplicitDto")
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertTrue(descriptor.only_explicitly_included)
        self.assertEqual(
            ["businessKey"], [member.name for member in descriptor.members]
        )
        self.assertEqual("businessKey", descriptor.difference_member)
        self.assertIn(
            "ordinary unannotated fields do not participate",
            _object_contract_block(ctx),
        )

    def test_08_call_super_false_does_not_use_parent_fields(self) -> None:
        parent_source = """package sample;
import lombok.Data;
@Data
public class EqualityBase {
    private String baseCode;
}
"""
        _, parent = _parsed(parent_source, "EqualityBase")
        child_source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
@EqualsAndHashCode(callSuper = false)
public class LocalDto extends EqualityBase {
    private String localCode;
}
"""
        ctx = _context(
            child_source,
            "LocalDto",
            lookup=_lookup_for(parent),
        )
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertFalse(descriptor.call_super)
        self.assertEqual(
            ["localCode"], [member.name for member in descriptor.members]
        )
        self.assertIn(
            "do not initialize or vary parent fields",
            _object_contract_block(ctx),
        )

    def test_09_call_super_true_uses_resolved_parent_state(self) -> None:
        parent_source = """package sample;
import lombok.Data;
@Data
public class EqualityBase {
    private String baseCode;
}
"""
        _, parent = _parsed(parent_source, "EqualityBase")
        child_source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
@EqualsAndHashCode(callSuper = true)
public class CombinedDto extends EqualityBase {
    private String localCode;
}
"""
        descriptor = _context(
            child_source,
            "CombinedDto",
            lookup=_lookup_for(parent),
        ).equality_descriptor
        assert descriptor is not None
        self.assertTrue(descriptor.call_super)
        self.assertEqual(
            {"localCode", "baseCode"},
            {member.name for member in descriptor.members},
        )
        base = next(
            member for member in descriptor.members if member.name == "baseCode"
        )
        self.assertTrue(base.origin.startswith("inherited:"))

    def test_10_inherited_equality_ignores_child_only_fields(self) -> None:
        parent_source = """package sample;
import lombok.Data;
@Data
public class EqualityBase {
    private String baseCode;
}
"""
        _, parent = _parsed(parent_source, "EqualityBase")
        child_source = """package sample;
public class InheritedDto extends EqualityBase {
    private String childOnly;
    public void setChildOnly(String childOnly) { this.childOnly = childOnly; }
}
"""
        ctx = _context(
            child_source,
            "InheritedDto",
            lookup=_lookup_for(parent),
        )
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertEqual(EqualityStrategy.INHERITED, descriptor.strategy)
        self.assertEqual(["baseCode"], [member.name for member in descriptor.members])
        self.assertNotEqual("childOnly", descriptor.difference_member)
        self.assertIn(
            "Child-only fields are unrelated to equality",
            _object_contract_block(ctx),
        )

    def test_11_equal_objects_must_have_equal_hash_codes(self) -> None:
        source = """package sample;
import lombok.Data;
@Data
public class HashContractDto {
    private String code;
}
"""
        contract = _object_contract_block(_context(source, "HashContractDto"))
        self.assertIn(
            "assertEquals(first.hashCode(), second.hashCode())", contract
        )

    def test_12_unequal_objects_never_require_different_hash_codes(self) -> None:
        source = """package sample;
import lombok.Data;
@Data
public class HashContractDto {
    private String code;
}
"""
        ctx = _context(source, "HashContractDto")
        contract = _object_contract_block(ctx)
        self.assertIn(
            "Never generate assertNotEquals(first.hashCode(), different.hashCode())",
            contract,
        )
        raw = """package sample;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class HashContractDtoTest {
    @Test
    void equalityContract() {
        HashContractDto first = new HashContractDto();
        first.setCode("A");
        HashContractDto second = new HashContractDto();
        second.setCode("A");
        HashContractDto different = new HashContractDto();
        different.setCode("B");
        assertEquals(first, second);
        assertEquals(first.hashCode(), second.hashCode());
        assertNotEquals(first, different);
        assertNotEquals(first.hashCode(), different.hashCode());
    }
}
"""
        result = finalize_java(
            raw,
            test_package="sample",
            test_class_name="HashContractDtoTest",
            template=TemplateSpec(JunitTestKind.PURE_UNIT, "dto-pojo"),
            cut_symbol=ctx.symbol,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertNotIn(
            "assertNotEquals(first.hashCode()", result.code or ""
        )
        self.assertIn(
            "assertEquals(first.hashCode(), second.hashCode())",
            result.code or "",
        )

    def test_13_equality_fixture_excludes_unrelated_recursive_state(self) -> None:
        source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
public class MinimalDto {
    private String code;
    @EqualsAndHashCode.Exclude
    private UnrelatedGraph unrelatedGraph;
}
"""
        ctx = _context(source, "MinimalDto")
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertEqual(["code"], [member.name for member in descriptor.members])
        contract = _object_contract_block(ctx)
        self.assertIn(
            "Do not call or reuse complete recursive DTO/entity fixture helpers",
            contract,
        )
        self.assertIn(
            "Do not populate unrelated nested objects", contract
        )

    def test_14_unresolved_nested_equality_reuses_same_instance(self) -> None:
        nested_source = """package sample;
public class PersonDto extends UnknownParent {
    public PersonDto() {}
}
"""
        _, nested = _parsed(nested_source, "PersonDto")
        source = """package sample;
import lombok.Data;
@Data
public class PolicyDto {
    private String code;
    private PersonDto person;
}
"""
        ctx = _context(
            source,
            "PolicyDto",
            lookup=_lookup_for(nested),
        )
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        person = next(
            member for member in descriptor.members if member.name == "person"
        )
        self.assertTrue(person.shared_reference)
        self.assertFalse(person.nested_equality_resolved)
        self.assertEqual(EqualityAssertionMode.VALUE, descriptor.assertion_mode)
        contract = _object_contract_block(ctx)
        self.assertIn(
            "assign that exact same instance to first and second", contract
        )
        self.assertIn("person (unresolved)", contract)

    def test_15_generated_id_entity_uses_non_null_identifier_values(self) -> None:
        source = """package sample;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.Id;
import lombok.Data;
@Entity
@Data
public class PolicyEntity {
    @Id
    @GeneratedValue
    private Long id;
}
"""
        ctx = _context(source, "PolicyEntity", style="entity-pojo")
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertTrue(descriptor.entity)
        self.assertEqual("identifier", descriptor.entity_key_kind)
        identifier = descriptor.members[0]
        self.assertTrue(identifier.identifier)
        self.assertTrue(identifier.generated_identifier)
        contract = _object_contract_block(ctx)
        self.assertIn(
            "must use verified non-null values", contract
        )
        self.assertIn(
            "Do not test two transient entities with null generated identifiers",
            contract,
        )

    def test_16_relationship_heavy_entity_is_conservative(self) -> None:
        customer_source = """package sample;
public class CustomerEntity {
    public CustomerEntity() {}
}
"""
        _, customer = _parsed(customer_source, "CustomerEntity")
        source = """package sample;
import jakarta.persistence.Entity;
import jakarta.persistence.ManyToOne;
import lombok.Data;
@Entity
@Data
public class RelationshipEntity {
    private String code;
    @ManyToOne
    private CustomerEntity customer;
}
"""
        ctx = _context(
            source,
            "RelationshipEntity",
            style="entity-pojo",
            lookup=_lookup_for(customer),
        )
        descriptor = ctx.equality_descriptor
        assert descriptor is not None
        self.assertEqual(
            EqualityAssertionMode.CONSERVATIVE, descriptor.assertion_mode
        )
        self.assertTrue(
            any(
                "relationship-heavy entity equality is unsafe" in warning
                for warning in descriptor.warnings
            )
        )
        contract = _object_contract_block(ctx)
        self.assertIn(
            "Do not generate an independent equal pair", contract
        )
        self.assertIn(
            "Do not simulate Hibernate proxies", contract
        )

    def test_17_serviceimpl_and_validation_remain_outside_equality_phase(self) -> None:
        source = """package sample;
public class PaymentServiceImpl {
    public String process(String request) { return request; }
}
"""
        with patch(
            "junitforge.context.resolve_equality_descriptor",
            side_effect=AssertionError("equality resolver must not run"),
        ):
            ctx = _context(source, "PaymentServiceImpl", style="service-unit")
        self.assertIsNone(ctx.equality_descriptor)
        self.assertEqual("(not applicable)", _object_contract_block(ctx))
        global_rules = _global_rules_block(ctx)
        self.assertIn(
            "For @EqualsAndHashCode(callSuper = true), follow its special rule exactly",
            global_rules,
        )
        self.assertNotIn(
            "Never apply a generic three-object equality template", global_rules
        )

    def test_18_simple_manual_equality_uses_only_visible_members(self) -> None:
        source = """package sample;
import java.util.Objects;
public class ManualDto {
    private String code;
    private String displayName;
    public ManualDto() {}
    public void setCode(String code) { this.code = code; }
    @Override
    public boolean equals(Object other) {
        if (this == other) return true;
        if (!(other instanceof ManualDto)) return false;
        ManualDto that = (ManualDto) other;
        return Objects.equals(code, that.code);
    }
    @Override
    public int hashCode() {
        return Objects.hash(code);
    }
}
"""
        descriptor = _context(source, "ManualDto").equality_descriptor
        assert descriptor is not None
        self.assertEqual(EqualityStrategy.MANUAL, descriptor.strategy)
        self.assertEqual(EqualityAssertionMode.VALUE, descriptor.assertion_mode)
        self.assertEqual(["code"], [member.name for member in descriptor.members])
        self.assertEqual("code", descriptor.difference_member)

    def test_19_one_sided_manual_contract_is_conservative_and_warned(self) -> None:
        source = """package sample;
public class EqualsOnlyDto {
    private String code;
    @Override
    public boolean equals(Object other) {
        return other instanceof EqualsOnlyDto
            && code.equals(((EqualsOnlyDto) other).code);
    }
}
"""
        descriptor = _context(source, "EqualsOnlyDto").equality_descriptor
        assert descriptor is not None
        self.assertEqual(EqualityStrategy.UNRESOLVED, descriptor.strategy)
        self.assertEqual(
            EqualityAssertionMode.CONSERVATIVE, descriptor.assertion_mode
        )
        self.assertIn(
            "equals is overridden but hashCode is not visibly overridden",
            descriptor.warnings,
        )

    def test_20_simple_included_replacement_method_is_resolved(self) -> None:
        source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
@EqualsAndHashCode(onlyExplicitlyIncluded = true)
public class ReplacementDto {
    private String code;
    @EqualsAndHashCode.Include(replaces = "code")
    public String equalityCode() {
        return code;
    }
}
"""
        descriptor = _context(source, "ReplacementDto").equality_descriptor
        assert descriptor is not None
        self.assertEqual(EqualityAssertionMode.VALUE, descriptor.assertion_mode)
        self.assertEqual(["code"], [member.name for member in descriptor.members])
        self.assertEqual("included-method:equalityCode", descriptor.members[0].origin)

    def test_21_complex_included_method_is_conservative_and_warned(self) -> None:
        source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
@EqualsAndHashCode(onlyExplicitlyIncluded = true)
public class NormalizedDto {
    private String code;
    @EqualsAndHashCode.Include(replaces = "code")
    public String equalityCode() {
        return code == null ? null : code.trim().toUpperCase();
    }
}
"""
        descriptor = _context(source, "NormalizedDto").equality_descriptor
        assert descriptor is not None
        self.assertEqual(
            EqualityAssertionMode.CONSERVATIVE, descriptor.assertion_mode
        )
        self.assertTrue(
            any(
                "unsupported included equality method(s): equalityCode" in warning
                for warning in descriptor.warnings
            )
        )

    def test_22_only_explicit_without_includes_is_conservative_and_warned(self) -> None:
        source = """package sample;
import lombok.Data;
import lombok.EqualsAndHashCode;
@Data
@EqualsAndHashCode(onlyExplicitlyIncluded = true)
public class EmptyExplicitDto {
    private String code;
}
"""
        descriptor = _context(source, "EmptyExplicitDto").equality_descriptor
        assert descriptor is not None
        self.assertEqual(
            EqualityAssertionMode.CONSERVATIVE, descriptor.assertion_mode
        )
        self.assertTrue(
            any(
                "no included equality member was resolved" in warning
                for warning in descriptor.warnings
            )
        )


if __name__ == "__main__":
    unittest.main()
