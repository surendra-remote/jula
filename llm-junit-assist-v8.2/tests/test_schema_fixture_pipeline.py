from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from junitforge.execution.fixture_builder_emitter import emit_fixture_catalog
from junitforge.execution.models import LocalVariableFact, PropertyKind
from junitforge.execution.payload_schema import (
    apply_map_usage_constraints,
    apply_usage_constraints,
    build_payload_schemas,
    extract_collection_requirements,
    extract_map_schemas,
    project_root_schema_ids,
)
from junitforge.models import ClassSymbol, FieldSig, MethodSig


def _getter(name: str, type_name: str) -> MethodSig:
    return MethodSig(name=f"get{name[:1].upper()}{name[1:]}", modifiers={"public"}, return_type=type_name)


def _setter(name: str, type_name: str) -> MethodSig:
    return MethodSig(
        name=f"set{name[:1].upper()}{name[1:]}",
        modifiers={"public"},
        return_type="void",
        params=[(type_name, name)],
    )


def _dto(name: str, fields: list[tuple[str, str]], *, extends: str | None = None) -> ClassSymbol:
    fs = [FieldSig(field_name, field_type, modifiers={"private"}) for field_name, field_type in fields]
    methods = []
    for field_name, field_type in fields:
        methods.extend((_getter(field_name, field_type), _setter(field_name, field_type)))
    return ClassSymbol(
        name=name,
        fqcn=f"sample.{name}",
        kind="class",
        modifiers={"public"},
        package_name="sample",
        fields=fs,
        methods=methods,
        extends=extends,
    )


class SchemaFixturePipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.contact = _dto("ContactDto", [
            ("contactType", "String"),
            ("contactValue", "String"),
        ])
        self.address = _dto("AddressDto", [
            ("lane1", "String"),
            ("postalCode", "String"),
            ("contacts", "List<ContactDto>"),
        ])
        self.person = _dto("PersonDto", [
            ("surname", "String"),
            ("gender", "String"),
            ("addresses", "List<AddressDto>"),
        ])
        self.base = _dto("BasePolicyDto", [("createdBy", "String")])
        self.policy = _dto("PolicyDto", [
            ("policyId", "Long"),
            ("policyNo", "String"),
            ("person", "PersonDto"),
        ], extends="BasePolicyDto")
        self.symbols = {
            s.name: s for s in (self.contact, self.address, self.person, self.base, self.policy)
        }
        self.symbols.update({s.fqcn: s for s in tuple(self.symbols.values())})

    def lookup(self, type_name: str, owner=None):
        simple = type_name.split("<", 1)[0].replace("[]", "").rsplit(".", 1)[-1]
        return self.symbols.get(type_name) or self.symbols.get(simple)

    def test_recursive_catalog_includes_all_fields_and_inheritance(self) -> None:
        schemas, diagnostics = build_payload_schemas(
            ["PolicyDto"], self.lookup, max_depth=5, max_types=20
        )
        by_name = {schema.type_name: schema for schema in schemas}
        self.assertEqual({"PolicyDto", "BasePolicyDto", "PersonDto", "AddressDto", "ContactDto"}, set(by_name))
        policy_fields = {field.name: field for field in by_name["PolicyDto"].properties}
        self.assertIn("createdBy", policy_fields)
        self.assertTrue(policy_fields["createdBy"].inherited)
        self.assertEqual(PropertyKind.LIST, next(p for p in by_name["PersonDto"].properties if p.name == "addresses").kind)
        self.assertFalse(any("unresolved" in d for d in diagnostics), diagnostics)

    def test_cycle_stops_without_recursive_emission(self) -> None:
        a = _dto("A", [("b", "B")])
        b = _dto("B", [("a", "A")])
        symbols = {"A": a, "B": b, a.fqcn: a, b.fqcn: b}
        schemas, diagnostics = build_payload_schemas(["A"], lambda t, owner=None: symbols.get(t) or symbols.get(t.rsplit(".", 1)[-1]))
        emitted = emit_fixture_catalog(schemas)
        self.assertEqual(2, len([f for f in emitted.fixtures if f.supported]))
        self.assertTrue(emitted.back_edges)
        self.assertTrue(any("cycle" in d for d in (*diagnostics, *emitted.diagnostics)))

    def test_dynamic_map_schema_preserves_literal_keys_and_nested_map(self) -> None:
        source = '''
            Map<String, Object> quotationMap = client.getQuotation();
            Integer id = (Integer) quotationMap.get("policyId");
            Map<String, Object> personMap = (Map<String, Object>) quotationMap.get("giPerson");
            String name = (String) personMap.get("fullName");
        '''
        locals_ = (
            LocalVariableFact("quotationMap", "Map<String, Object>", "client.getQuotation()"),
            LocalVariableFact("id", "Integer", '(Integer) quotationMap.get("policyId")'),
            LocalVariableFact("personMap", "Map<String, Object>", '(Map<String, Object>) quotationMap.get("giPerson")'),
            LocalVariableFact("name", "String", '(String) personMap.get("fullName")'),
        )
        schemas = extract_map_schemas("m()", source, locals_)
        by_var = {schema.variable_name: schema for schema in schemas}
        quotation_entries = {entry.key_literal: entry for entry in by_var["quotationMap"].entries}
        self.assertEqual("Integer", quotation_entries["policyId"].runtime_type)
        self.assertEqual("map:personMap", quotation_entries["giPerson"].nested_schema_id)
        person_entries = {entry.key_literal: entry for entry in by_var["personMap"].entries}
        self.assertEqual("String", person_entries["fullName"].runtime_type)

    def test_dynamic_map_collection_element_is_deeply_hydrated(self) -> None:
        source = r"""
            Map<String, Object> quotationMap = client.getQuotation();
            Map<String, Object> personMap = (Map<String, Object>) quotationMap.get("person");
            List<?> addressList = (List<?>) personMap.get("addresses");
            addressList.forEach(a -> {
                Map<String, Object> add = (Map<String, Object>) a;
                String line1 = (String) add.get("line1");
            });
        """
        locals_ = (
            LocalVariableFact("quotationMap", "Map<String, Object>", "client.getQuotation()"),
            LocalVariableFact("personMap", "Map<String, Object>", '(Map<String, Object>) quotationMap.get("person")'),
            LocalVariableFact("addressList", "List<?>", '(List<?>) personMap.get("addresses")'),
            LocalVariableFact("add", "Map<String, Object>", '(Map<String, Object>) a'),
        )
        schemas = extract_map_schemas("m()", source, locals_)
        by_var = {schema.variable_name: schema for schema in schemas}
        person_entries = {entry.key_literal: entry for entry in by_var["personMap"].entries}
        self.assertEqual("map:add", person_entries["addresses"].collection_element_schema_id)
        emitted = emit_fixture_catalog((), schemas)
        person_fixture = next(f for f in emitted.fixtures if f.fixture_id == "map:personMap")
        self.assertIn("validAddMap()", person_fixture.method_source)
        self.assertIn("map:add", person_fixture.dependent_fixture_ids)

    def test_dynamic_map_date_format_uses_reachable_simple_date_format(self) -> None:
        source = 'Map<String,Object> personMap = x; String dob = (String) personMap.get("dob");'
        schemas = extract_map_schemas(
            "m()", source,
            (LocalVariableFact("personMap", "Map<String,Object>", "x"),),
        )
        constrained = apply_map_usage_constraints(
            schemas,
            ('SimpleDateFormat f = new SimpleDateFormat("dd/MM/yyyy"); f.parse(person.getBirthDate());',),
        )
        entry = next(e for s in constrained for e in s.entries if e.key_literal == "dob")
        self.assertEqual('"01/01/1990"', entry.baseline_value)

    def test_fixture_root_projection_does_not_expand_entire_graph(self) -> None:
        schemas, _ = build_payload_schemas(["PolicyDto"], self.lookup, max_depth=5, max_types=20)
        roots = project_root_schema_ids(["PolicyDto"], schemas)
        self.assertEqual(("sample.PolicyDto",), roots)

    def test_collection_cardinality(self) -> None:
        source = "response.getRiders().get(1).getCode(); response.getItems().stream().findFirst().orElseThrow();"
        requirements = {req.path: req for req in extract_collection_requirements(source)}
        self.assertEqual(2, requirements["response.riders"].minimum_size)
        self.assertEqual(1, requirements["response.items"].minimum_size)

    def test_usage_constraints_set_literal_and_collection_size(self) -> None:
        schemas, _ = build_payload_schemas(["PersonDto"], self.lookup, max_depth=5, max_types=20)
        requirements = extract_collection_requirements("person.getAddresses().get(1).getLane1();")
        constrained = apply_usage_constraints(
            schemas,
            ('"M".equals(person.getGender());',),
            requirements,
        )
        by_name = {schema.type_name: schema for schema in constrained}
        person_fields = {field.name: field for field in by_name["PersonDto"].properties}
        self.assertEqual('"M"', person_fields["gender"].baseline_value)
        self.assertEqual(2, person_fields["addresses"].minimum_size)
        emitted = emit_fixture_catalog(constrained)
        helper = next(f.method_source for f in emitted.fixtures if f.schema_id == "sample.PersonDto")
        self.assertGreaterEqual(helper.count("validAddressDto()"), 2)

    def test_explicit_null_initializer_does_not_override_nested_fixture(self) -> None:
        child = _dto("ChildDto", [("value", "String")])
        parent = _dto("ParentDto", [("child", "ChildDto")])
        parent.fields[0].initializer = "null"
        symbols = {"ChildDto": child, "ParentDto": parent, child.fqcn: child, parent.fqcn: parent}
        schemas, _ = build_payload_schemas(
            ["ParentDto"],
            lambda t, owner=None: symbols.get(t) or symbols.get(t.rsplit(".", 1)[-1]),
        )
        emitted = emit_fixture_catalog(schemas)
        helper = next(f.method_source for f in emitted.fixtures if f.fixture_id == "sample.ParentDto")
        self.assertIn("setChild(validChildDto())", helper)
        self.assertNotIn("setChild(null)", helper)

    def test_byte_and_short_fixture_values_use_explicit_narrowing_casts(self) -> None:
        narrow = _dto("NarrowDto", [
            ("primitiveByte", "byte"),
            ("boxedByte", "Byte"),
            ("primitiveShort", "short"),
            ("boxedShort", "Short"),
        ])
        symbols = {"NarrowDto": narrow, narrow.fqcn: narrow}
        schemas, diagnostics = build_payload_schemas(
            ["NarrowDto"],
            lambda t, owner=None: symbols.get(t) or symbols.get(t.rsplit(".", 1)[-1]),
        )
        self.assertFalse(diagnostics, diagnostics)
        emitted = emit_fixture_catalog(schemas)
        helper = next(f.method_source for f in emitted.fixtures if f.fixture_id == "sample.NarrowDto")
        self.assertIn("setPrimitiveByte((byte) 1)", helper)
        self.assertIn("setBoxedByte((byte) 1)", helper)
        self.assertIn("setPrimitiveShort((short) 1)", helper)
        self.assertIn("setBoxedShort((short) 1)", helper)

        source = f"""package sample;
public class NarrowFixtureCompileTest {{
{helper}
    static class NarrowDto {{
        void setPrimitiveByte(byte value) {{}}
        void setBoxedByte(Byte value) {{}}
        void setPrimitiveShort(short value) {{}}
        void setBoxedShort(Short value) {{}}
    }}
}}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample" / "NarrowFixtureCompileTest.java"
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding="utf-8")
            result = subprocess.run(["javac", str(path)], capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr + "\n" + source)

    def test_emitted_fixture_java_compiles_and_is_deep(self) -> None:
        schemas, _ = build_payload_schemas(["PolicyDto"], self.lookup, max_depth=5, max_types=20)
        emitted = emit_fixture_catalog(schemas)
        helpers = "\n\n".join(emitted.helpers)
        self.assertIn("validContactDto()", helpers)
        self.assertIn("validAddressDto()", helpers)
        self.assertIn("validPersonDto()", helpers)
        self.assertIn("validPolicyDto()", helpers)
        source = f'''package sample;
import java.util.*;
public class FixtureCompileTest {{
{helpers}
    static class BasePolicyDto {{ private String createdBy; public void setCreatedBy(String v) {{ createdBy=v; }} public String getCreatedBy() {{ return createdBy; }} }}
    static class PolicyDto extends BasePolicyDto {{ private Long policyId; private String policyNo; private PersonDto person; public void setPolicyId(Long v) {{policyId=v;}} public void setPolicyNo(String v) {{policyNo=v;}} public void setPerson(PersonDto v) {{person=v;}} public PersonDto getPerson() {{return person;}} }}
    static class PersonDto {{ private String surname; private String gender; private List<AddressDto> addresses; public void setSurname(String v){{surname=v;}} public void setGender(String v){{gender=v;}} public void setAddresses(List<AddressDto> v){{addresses=v;}} public List<AddressDto> getAddresses(){{return addresses;}} }}
    static class AddressDto {{ private String lane1; private String postalCode; private List<ContactDto> contacts; public void setLane1(String v){{lane1=v;}} public void setPostalCode(String v){{postalCode=v;}} public void setContacts(List<ContactDto> v){{contacts=v;}} public List<ContactDto> getContacts(){{return contacts;}} }}
    static class ContactDto {{ private String contactType; private String contactValue; public void setContactType(String v){{contactType=v;}} public void setContactValue(String v){{contactValue=v;}} }}
}}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample" / "FixtureCompileTest.java"
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding="utf-8")
            result = subprocess.run(["javac", str(path)], capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr + "\n" + source)


class SymbolModelCompatibilityTest(unittest.TestCase):
    def test_class_symbol_declares_and_accepts_source_context_metadata(self) -> None:
        symbol = ClassSymbol("PolicyDto", "sample.PolicyDto", "class")
        symbol.package_name = "sample"
        symbol.imports = ["sample.PersonDto"]
        symbol.source_path = "src/main/java/sample/PolicyDto.java"
        symbol.enclosing_fqcn = "sample.Outer"

        self.assertEqual("sample", symbol.package_name)
        self.assertEqual(["sample.PersonDto"], symbol.imports)
        self.assertEqual("src/main/java/sample/PolicyDto.java", symbol.source_path)
        self.assertEqual("sample.Outer", symbol.enclosing_fqcn)
        self.assertTrue(hasattr(symbol, "__dict__"))

    def test_attach_source_context_populates_root_and_nested_symbols(self) -> None:
        from junitforge.models import JavaSourceFile
        from junitforge.parser.java_symbols import _attach_source_context

        nested = ClassSymbol("Inner", "sample.Outer.Inner", "class")
        root = ClassSymbol("Outer", "sample.Outer", "class", nested=[nested])
        java_file = JavaSourceFile(
            path=Path("src/main/java/sample/Outer.java"),
            package="sample",
            imports=["sample.PersonDto"],
            types=[root],
        )

        _attach_source_context(java_file)

        self.assertEqual("sample", root.package_name)
        self.assertEqual(["sample.PersonDto"], root.imports)
        self.assertEqual("src/main/java/sample/Outer.java", root.source_path)
        self.assertIsNone(root.enclosing_fqcn)
        self.assertEqual("sample", nested.package_name)
        self.assertEqual("sample.Outer", nested.enclosing_fqcn)


if __name__ == "__main__":
    unittest.main()

class ResolverAndSkeletonIntegrationTest(unittest.TestCase):
    def test_context_aware_resolver_selects_import_and_indexes_nested(self) -> None:
        from junitforge.parser.lazy_symbol_resolver import LazySymbolResolver

        resolver = LazySymbolResolver([])
        motor = ClassSymbol("Premium", "motor.dto.Premium", "class", package_name="motor.dto")
        travel = ClassSymbol("Premium", "travel.dto.Premium", "class", package_name="travel.dto")
        inner = ClassSymbol("Inner", "sample.Outer.Inner", "class", package_name="sample")
        outer = ClassSymbol("Outer", "sample.Outer", "class", package_name="sample", nested=[inner])
        resolver._index_symbol(motor)
        resolver._index_symbol(travel)
        resolver._index_symbol(outer)
        owner = ClassSymbol(
            "TravelServiceImpl", "sample.TravelServiceImpl", "class",
            package_name="sample", imports=["travel.dto.Premium"],
        )
        self.assertEqual("travel.dto.Premium", resolver.lookup_context("Premium", owner).fqcn)
        self.assertEqual("sample.Outer.Inner", resolver.lookup_context("sample.Outer.Inner", owner).fqcn)
        self.assertIsNone(resolver.lookup("Premium"), "ambiguous simple-name fallback must not guess")

    def test_skeleton_contains_only_verified_fixture_sources(self) -> None:
        from junitforge.execution.assembly import build_test_skeleton
        from junitforge.execution.models import (
            ExecutionContext, ExecutionTargetKind, FixtureSpec, MethodExecutionContext,
        )
        from junitforge.models import (
            ClasspathProfile, GenerationContext, StackProfile, TemplateSpec, TestKind,
        )

        fixture = FixtureSpec(
            fixture_id="sample.RequestDto",
            method_name="validRequestDto",
            return_type="RequestDto",
            schema_id="sample.RequestDto",
            imports=("java.util.ArrayList",),
            method_source=(
                "    private RequestDto validRequestDto() {\n"
                "        RequestDto value = new RequestDto();\n"
                "        return value;\n"
                "    }"
            ),
        )
        execution = ExecutionContext(
            target_kind=ExecutionTargetKind.SERVICE_IMPL,
            target_fqcn="sample.SampleServiceImpl",
            methods=(MethodExecutionContext(
                method_id="run(RequestDto)", signature="public void run(RequestDto request)",
                source_range=(1, 1), method_source="public void run(RequestDto request) {}", endpoint=None,
                required_fixture_ids=("sample.RequestDto",),
            ),),
            fixtures=(fixture,),
        )
        symbol = ClassSymbol("SampleServiceImpl", "sample.SampleServiceImpl", "class", modifiers={"public"})
        ctx = GenerationContext(
            cut_source="", symbol=symbol, collaborators=[], test_package="sample",
            test_class_name="SampleServiceImplTest", stack=StackProfile(),
            classpath=ClasspathProfile(), template=TemplateSpec(TestKind.PURE_UNIT, "unit"),
            source_path=Path("SampleServiceImpl.java"), execution_context=execution,
        )
        skeleton = build_test_skeleton(ctx)
        self.assertIn("private RequestDto validRequestDto()", skeleton)
        self.assertIn("Reusable parser-derived fixtures", skeleton)
        self.assertEqual(1, skeleton.count("validRequestDto()"))

    def test_method_prompt_lists_verified_fixture_and_does_not_claim_unlisted_helpers(self) -> None:
        from junitforge.execution.models import (
            ExecutionContext, ExecutionTargetKind, FixtureSpec, MethodExecutionContext,
        )
        from junitforge.models import (
            ClasspathProfile, GenerationContext, StackProfile, TemplateSpec, TestKind,
        )
        from junitforge.prompts.generation import build_method_generation_messages

        method = MethodSig("run", modifiers={"public"}, return_type="void", params=[("RequestDto", "request")])
        fixture = FixtureSpec(
            fixture_id="sample.RequestDto", method_name="validRequestDto",
            return_type="RequestDto", schema_id="sample.RequestDto",
            method_source="private RequestDto validRequestDto() { return new RequestDto(); }",
        )
        method_context = MethodExecutionContext(
            method_id="run(RequestDto)", signature=method.render(), source_range=(1, 1),
            method_source="public void run(RequestDto request) {}", endpoint=None,
            required_fixture_ids=("sample.RequestDto",),
        )
        unrelated = FixtureSpec(
            fixture_id="sample.UnrelatedDto", method_name="validUnrelatedDto",
            return_type="UnrelatedDto", schema_id="sample.UnrelatedDto",
            method_source="private UnrelatedDto validUnrelatedDto() { return new UnrelatedDto(); }",
        )
        execution = ExecutionContext(
            target_kind=ExecutionTargetKind.SERVICE_IMPL,
            target_fqcn="sample.SampleServiceImpl", methods=(method_context,), fixtures=(fixture, unrelated),
        )
        symbol = ClassSymbol(
            "SampleServiceImpl", "sample.SampleServiceImpl", "class",
            modifiers={"public"}, methods=[method],
        )
        ctx = GenerationContext(
            cut_source="", symbol=symbol, collaborators=[], test_package="sample",
            test_class_name="SampleServiceImplTest", stack=StackProfile(),
            classpath=ClasspathProfile(), template=TemplateSpec(TestKind.PURE_UNIT, "unit"),
            source_path=Path("SampleServiceImpl.java"), execution_context=execution,
        )
        prompt = build_method_generation_messages(ctx, method, "")[1]["content"]
        self.assertIn("RequestDto validRequestDto()", prompt)
        self.assertIn("Call these existing fixture methods", prompt)
        self.assertIn("Never call an unlisted fixture method", prompt)
        self.assertNotIn("validUnrelatedDto", prompt)


class ReachabilityAndRepairRegressionTest(unittest.TestCase):
    def test_package_visible_same_class_call_is_recognized_as_reachable(self) -> None:
        from junitforge.execution.analyzer import _cli_helper_calls

        entry = MethodSig(
            "getRenewalPolicy", modifiers={"public"}, return_type="Object", params=[],
            body_facts={"methodCalls": [{
                "receiverKind": "this", "scope": None, "name": "getQuotation",
                "arguments": [],
            }]},
        )
        helper = MethodSig("getQuotation", modifiers=set(), return_type="Map<String,Object>", params=[])
        calls = _cli_helper_calls(entry, {(helper.name, helper.arity): "getQuotation()"})
        self.assertEqual(["getQuotation()"], [call.helper_method_id for call in calls])

    def test_structural_repair_prompt_targets_cut_stubbing(self) -> None:
        from junitforge.execution.models import ExecutionContext, ExecutionTargetKind, MethodExecutionContext
        from junitforge.models import ClasspathProfile, GenerationContext, StackProfile, TemplateSpec, TestKind
        from junitforge.prompts.repair import build_method_generation_validation_repair_messages

        method = MethodSig("run", modifiers={"public"}, return_type="void", params=[])
        method_context = MethodExecutionContext(
            method_id="run()", signature=method.render(), source_range=(1, 1),
            method_source="public void run() {}", endpoint=None,
        )
        execution = ExecutionContext(
            target_kind=ExecutionTargetKind.SERVICE_IMPL,
            target_fqcn="sample.SampleServiceImpl", methods=(method_context,),
        )
        symbol = ClassSymbol("SampleServiceImpl", "sample.SampleServiceImpl", "class", modifiers={"public"}, methods=[method])
        ctx = GenerationContext(
            cut_source="", symbol=symbol, collaborators=[], test_package="sample",
            test_class_name="SampleServiceImplTest", stack=StackProfile(),
            classpath=ClasspathProfile(), template=TemplateSpec(TestKind.PURE_UNIT, "unit"),
            source_path=Path("SampleServiceImpl.java"), execution_context=execution,
        )
        messages = build_method_generation_validation_repair_messages(
            ctx, method,
            "@Test void bad(){ doReturn(1).when(sampleServiceImpl).run(); }",
            "response stubs or verifies the class under test",
        )
        prompt = messages[1]["content"]
        self.assertIn("REJECTION REASON", prompt)
        self.assertIn("Never use the CUT as a receiver", prompt)
        self.assertIn("response stubs or verifies", prompt)

class PublicHelperAndWritableMapRegressionTest(unittest.TestCase):
    def test_implicit_receiver_public_helper_is_recognized(self) -> None:
        from junitforge.execution.analyzer import _cli_helper_calls

        entry = MethodSig(
            "getRenewalSummary", modifiers={"public"}, return_type="Object", params=[],
            body_facts={"methodCalls": [{
                "receiverKind": "implicit", "scope": None, "name": "getQuotation",
                "arguments": [
                    {"expr": "quotationNo", "kind": "name"},
                    {"expr": "transactionType", "kind": "name"},
                ],
            }]},
        )
        calls = _cli_helper_calls(
            entry,
            {("getQuotation", 2): "getQuotation(String,String)"},
        )
        self.assertEqual(
            ["getQuotation(String,String)"],
            [call.helper_method_id for call in calls],
        )

    def test_source_fallback_finds_bare_public_helper_call(self) -> None:
        from junitforge.execution.analyzer import _helper_calls

        entry = MethodSig(
            "getRenewalSummary", modifiers={"public"}, return_type="Object",
            params=[("Request", "request")],
        )
        source = '''
        public Object getRenewalSummary(Request request) {
            String quotationNo = request.getQuotationNo();
            String transactionType = request.getTransactionType();
            Map<?, ?> quotation = getQuotation(quotationNo, transactionType);
            return quotation;
        }
        '''
        calls = _helper_calls(
            entry,
            source,
            1,
            {("getQuotation", 2): "getQuotation(String,String)"},
            [],
        )
        self.assertEqual(
            ["getQuotation(String,String)"],
            [call.helper_method_id for call in calls],
        )

    def _validation_context(self):
        from junitforge.execution.models import (
            ExecutionContext, ExecutionTargetKind, MethodExecutionContext,
        )
        from junitforge.models import (
            ClasspathProfile, GenerationContext, StackProfile, TemplateSpec, TestKind,
        )

        method = MethodSig(
            "getRenewalSummary", modifiers={"public"}, return_type="Object", params=[]
        )
        method_context = MethodExecutionContext(
            method_id="getRenewalSummary()",
            signature=method.render(),
            source_range=(1, 1),
            method_source="public Object getRenewalSummary() { return null; }",
            endpoint=None,
        )
        execution = ExecutionContext(
            target_kind=ExecutionTargetKind.SERVICE_IMPL,
            target_fqcn="sample.TravelServiceImpl",
            methods=(method_context,),
        )
        symbol = ClassSymbol(
            "TravelServiceImpl", "sample.TravelServiceImpl", "class",
            modifiers={"public"}, methods=[method],
        )
        ctx = GenerationContext(
            cut_source="", symbol=symbol, collaborators=[], test_package="sample",
            test_class_name="TravelServiceImplTest", stack=StackProfile(),
            classpath=ClasspathProfile(), template=TemplateSpec(TestKind.PURE_UNIT, "unit"),
            source_path=Path("TravelServiceImpl.java"), execution_context=execution,
        )
        return ctx, method, method_context

    def test_wildcard_map_put_is_rejected_before_javac(self) -> None:
        from junitforge.execution.assembly import validate_method_block

        ctx, method, method_context = self._validation_context()
        block = '''
        @Test
        void familyPlan() {
            Object result = travelServiceImpl.getRenewalSummary();
            Map<?, ?> quotationMap = validQuotationMap();
            Map<?, ?> packageTypeMap = (Map<?, ?>) quotationMap.get("packageType");
            packageTypeMap.put("code", "F");
        }
        '''
        validation = validate_method_block(ctx, method, method_context, block, set())
        self.assertFalse(validation.ok)
        self.assertIn("mutates wildcard Map<?, ?>", validation.reason)

    def test_typed_mutable_map_put_is_accepted_structurally(self) -> None:
        from junitforge.execution.assembly import validate_method_block

        ctx, method, method_context = self._validation_context()
        block = '''
        @Test
        void familyPlan() {
            Object result = travelServiceImpl.getRenewalSummary();
            Map<String, Object> quotationMap = validQuotationMap();
            Map<String, Object> packageTypeMap =
                    (Map<String, Object>) quotationMap.get("packageType");
            packageTypeMap.put("code", "F");
        }
        '''
        validation = validate_method_block(ctx, method, method_context, block, set())
        self.assertTrue(validation.ok, validation.reason)

    def test_generation_prompt_requires_public_helper_collaborator_stubs_and_writable_maps(self) -> None:
        from junitforge.prompts.generation import build_method_generation_messages

        ctx, method, _ = self._validation_context()
        prompt = build_method_generation_messages(ctx, method, "")[1]["content"]
        self.assertIn("public, protected, or package-visible helper", prompt)
        self.assertIn("Never call put/putAll/replace/compute/merge on Map<?, ?>", prompt)

class DeterministicWritableMapNormalizationTest(unittest.TestCase):
    def test_wildcard_map_branch_mutation_is_normalized_before_validation(self) -> None:
        from junitforge.execution.assembly import (
            normalize_writable_map_mutations,
            validate_method_block,
        )

        helper = PublicHelperAndWritableMapRegressionTest()
        ctx, method, method_context = helper._validation_context()
        broken = '''
        @Test
        void familyPlan() {
            Object result = travelServiceImpl.getRenewalSummary();
            Map<?, ?> quotationMap = validQuotationMap();
            Map<?, ?> packageTypeMap = (Map) quotationMap.get("packageType");
            packageTypeMap.put("code", "F");
        }
        '''
        normalized = normalize_writable_map_mutations(broken)
        self.assertIn("Map<String, Object> packageTypeMap", normalized)
        self.assertIn(
            '(Map<String, Object>) quotationMap.get("packageType")',
            normalized,
        )
        validation = validate_method_block(
            ctx, method, method_context, normalized, set()
        )
        self.assertTrue(validation.ok, validation.reason)

class PublicHelperContractPropagationRegressionTest(unittest.TestCase):
    def test_method_declaration_detection_ignores_generic_whitespace(self) -> None:
        from junitforge.execution.analyzer import _method_declared_in_source

        source = '''
        public class TravelServiceImpl {
            public Map<?, ?> getQuotation(String quotationNo, String transactionType) {
                return null;
            }
        }
        '''
        helper = MethodSig(
            "getQuotation",
            modifiers={"public"},
            return_type="Map<?,?>",
            params=[("String", "quotationNo"), ("String", "transactionType")],
            line=3,
            end_line=5,
        )
        self.assertTrue(_method_declared_in_source(source, helper))

    def _context_with_helper_collaborators(self):
        from junitforge.execution.models import (
            DependencyInvocation,
            DependencyMethodContract,
            ExecutionContext,
            ExecutionTargetKind,
            InvocationArgument,
            MethodExecutionContext,
            ResolutionStatus,
        )
        from junitforge.models import (
            ClasspathProfile, GenerationContext, StackProfile, TemplateSpec, TestKind,
        )

        method = MethodSig(
            "getRenewalSummary",
            modifiers={"public"},
            return_type="Object",
            params=[("Request", "request")],
        )
        encode_contract = DependencyMethodContract(
            dependency_field="aesEncUtils",
            dependency_type="AESEncUtils",
            method_name="encode",
            return_type="String",
            parameter_types=("String",),
            resolution_status=ResolutionStatus.RESOLVED,
        )
        quotation_contract = DependencyMethodContract(
            dependency_field="iQuotationClient",
            dependency_type="IQuotationClient",
            method_name="getQuotationByRefNoTransType",
            return_type="Map<String, Object>",
            parameter_types=("String", "String"),
            resolution_status=ResolutionStatus.RESOLVED,
        )
        invocations = (
            DependencyInvocation(
                invocation_id="getQuotation(String,String):I1",
                caller_method_id="getQuotation(String,String)",
                dependency_field="aesEncUtils",
                dependency_type="AESEncUtils",
                method_name="encode",
                arguments=(InvocationArgument("quotationNo", inferred_type="String"),),
                contract=encode_contract,
                assigned_to="encryptedQuotationNo",
            ),
            DependencyInvocation(
                invocation_id="getQuotation(String,String):I2",
                caller_method_id="getQuotation(String,String)",
                dependency_field="iQuotationClient",
                dependency_type="IQuotationClient",
                method_name="getQuotationByRefNoTransType",
                arguments=(
                    InvocationArgument("encryptedQuotationNo", inferred_type="String"),
                    InvocationArgument("transactionType", inferred_type="String"),
                ),
                contract=quotation_contract,
                assigned_to="quotationMap",
            ),
        )
        method_context = MethodExecutionContext(
            method_id="getRenewalSummary(Request)",
            signature=method.render(),
            source_range=(1, 4),
            method_source=(
                "public Object getRenewalSummary(Request request) { "
                "return getQuotation(request.getQuotationNo(), request.getTransactionType()); }"
            ),
            endpoint=None,
            dependency_invocations=invocations,
            reachable_private_methods=("getQuotation(String,String)",),
        )
        execution = ExecutionContext(
            target_kind=ExecutionTargetKind.SERVICE_IMPL,
            target_fqcn="sample.TravelServiceImpl",
            methods=(method_context,),
        )
        symbol = ClassSymbol(
            "TravelServiceImpl",
            "sample.TravelServiceImpl",
            "class",
            modifiers={"public"},
            methods=[method],
        )
        ctx = GenerationContext(
            cut_source="",
            symbol=symbol,
            collaborators=[],
            test_package="sample",
            test_class_name="TravelServiceImplTest",
            stack=StackProfile(),
            classpath=ClasspathProfile(),
            template=TemplateSpec(TestKind.PURE_UNIT, "unit"),
            source_path=Path("TravelServiceImpl.java"),
            execution_context=execution,
        )
        return ctx, method, method_context

    def test_suite_is_rejected_when_public_helper_collaborator_stubs_are_absent(self) -> None:
        from junitforge.execution.assembly import validate_method_block

        ctx, method, method_context = self._context_with_helper_collaborators()
        block = '''
        @Test
        void baseline() {
            Object result = travelServiceImpl.getRenewalSummary(validRequest());
            assertNotNull(result);
        }
        '''
        validation = validate_method_block(ctx, method, method_context, block, set())
        self.assertFalse(validation.ok)
        self.assertIn("aesEncUtils.encode", validation.reason)
        self.assertIn("iQuotationClient.getQuotationByRefNoTransType", validation.reason)

    def test_suite_is_accepted_when_public_helper_collaborators_are_stubbed(self) -> None:
        from junitforge.execution.assembly import validate_method_block

        ctx, method, method_context = self._context_with_helper_collaborators()
        block = '''
        @Test
        void baseline() {
            when(aesEncUtils.encode(anyString())).thenReturn("encrypted-ref");
            when(iQuotationClient.getQuotationByRefNoTransType(eq("encrypted-ref"), anyString()))
                    .thenReturn(new HashMap<>());
            Object result = travelServiceImpl.getRenewalSummary(validRequest());
            assertNotNull(result);
        }
        '''
        validation = validate_method_block(ctx, method, method_context, block, set())
        self.assertTrue(validation.ok, validation.reason)

class PublicHelperExecutionContextIntegrationTest(unittest.TestCase):
    def test_generic_map_public_helper_invocations_are_aggregated_into_entry_context(self) -> None:
        from junitforge.execution.analyzer import build_execution_context
        from junitforge.execution.models import ExecutionTargetKind
        from junitforge.models import JavaSourceFile

        source = '''package sample;
public class TravelServiceImpl implements TravelService {
    private AESEncUtils aesEncUtils;
    private IQuotationClient iQuotationClient;
    public Object getRenewalSummary(Request request) {
        return getQuotation(request.getQuotationNo(), request.getTransactionType());
    }
    public Map<?, ?> getQuotation(String quotationNo, String transactionType) {
        String encrypted = aesEncUtils.encode(quotationNo);
        return iQuotationClient.getQuotationByRefNoTransType(encrypted, transactionType);
    }
}
'''
        entry = MethodSig(
            "getRenewalSummary", modifiers={"public"}, return_type="Object",
            params=[("Request", "request")], line=5, end_line=7,
            body_facts={
                "methodCalls": [{
                    "receiverKind": "implicit", "scope": None,
                    "name": "getQuotation",
                    "arguments": [
                        {"expr": "request.getQuotationNo()", "kind": "methodCall"},
                        {"expr": "request.getTransactionType()", "kind": "methodCall"},
                    ],
                }],
                "branches": [],
            },
        )
        helper = MethodSig(
            "getQuotation", modifiers={"public"}, return_type="Map<?,?>",
            params=[("String", "quotationNo"), ("String", "transactionType")],
            line=8, end_line=11,
            body_facts={
                "methodCalls": [
                    {
                        "receiverKind": "field", "scope": "aesEncUtils",
                        "name": "encode",
                        "arguments": [{"expr": "quotationNo", "kind": "name"}],
                        "assignedTo": "encrypted",
                    },
                    {
                        "receiverKind": "field", "scope": "iQuotationClient",
                        "name": "getQuotationByRefNoTransType",
                        "arguments": [
                            {"expr": "encrypted", "kind": "name"},
                            {"expr": "transactionType", "kind": "name"},
                        ],
                    },
                ],
                "branches": [],
            },
        )
        cut = ClassSymbol(
            "TravelServiceImpl", "sample.TravelServiceImpl", "class",
            modifiers={"public"}, package_name="sample",
            implements=["TravelService"],
            fields=[
                FieldSig("aesEncUtils", "AESEncUtils", modifiers={"private"}),
                FieldSig("iQuotationClient", "IQuotationClient", modifiers={"private"}),
            ],
            methods=[entry, helper],
        )
        aes = ClassSymbol(
            "AESEncUtils", "sample.AESEncUtils", "class",
            methods=[MethodSig(
                "encode", modifiers={"public"}, return_type="String",
                params=[("String", "value")],
            )],
        )
        quotation_client = ClassSymbol(
            "IQuotationClient", "sample.IQuotationClient", "interface",
            methods=[MethodSig(
                "getQuotationByRefNoTransType",
                modifiers={"public", "abstract"},
                return_type="Map<String,Object>",
                params=[("String", "refNo"), ("String", "transactionType")],
            )],
        )
        request = ClassSymbol("Request", "sample.Request", "class")
        service = ClassSymbol(
            "TravelService", "sample.TravelService", "interface",
            methods=[MethodSig(
                "getRenewalSummary",
                modifiers={"public", "abstract"},
                return_type="Object",
                params=[("Request", "request")],
            )],
        )
        symbols = {
            "AESEncUtils": aes,
            "IQuotationClient": quotation_client,
            "Request": request,
            "TravelService": service,
            aes.fqcn: aes,
            quotation_client.fqcn: quotation_client,
            request.fqcn: request,
            service.fqcn: service,
        }

        def lookup(type_name: str, owner=None):
            return symbols.get(type_name) or symbols.get(type_name.rsplit(".", 1)[-1])

        java_file = JavaSourceFile(
            path=Path("TravelServiceImpl.java"), package="sample", imports=[],
            types=[cut], source=source,
        )
        context = build_execution_context(
            source_file=java_file,
            symbol=cut,
            lookup=lookup,
            target_kind=ExecutionTargetKind.SERVICE_IMPL,
        )
        self.assertIsNotNone(context)
        entry_context = next(
            method for method in context.methods
            if method.method_id == "getRenewalSummary(Request)"
        )
        self.assertIn("getQuotation(String,String)", entry_context.reachable_private_methods)
        self.assertEqual(
            {
                ("aesEncUtils", "encode"),
                ("iQuotationClient", "getQuotationByRefNoTransType"),
            },
            {
                (invocation.dependency_field, invocation.method_name)
                for invocation in entry_context.dependency_invocations
            },
        )
