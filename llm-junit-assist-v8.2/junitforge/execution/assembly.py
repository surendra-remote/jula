"""Deterministic Controller/ServiceImpl test-class assembly.

The LLM generates only @Test methods for one production method.  Python owns the
class wrapper, imports, mocks, standalone MockMvc setup, method markers, and
incremental validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from junitforge.execution.models import MethodExecutionContext
from junitforge.models import (
    GeneratedScope,
    GeneratedScopeKind,
    GenerationContext,
    MethodSig,
)

_BEGIN = "// JUNITFORGE_METHOD_BEGIN: "
_END = "// JUNITFORGE_METHOD_END: "


@dataclass(slots=True, frozen=True)
class MethodBlockValidation:
    ok: bool
    reason: str = ""
    test_names: tuple[str, ...] = ()
    # Advisory findings. NOT grounds for rejection: the execution analyzer is an
    # approximation of Java, so an "unobserved" call may be perfectly valid code
    # the analyzer failed to extract. javac is the authority (DEC-Q072).
    warnings: tuple[str, ...] = ()




def _mutated_wildcard_map_variables(block: str) -> tuple[str, ...]:
    wildcard_map_vars = set(re.findall(
        r"\b(?:java\.util\.)?Map\s*<\s*[^>]*\?[^>]*>\s+([A-Za-z_$][\w$]*)\b",
        block,
    ))
    return tuple(sorted(
        variable for variable in wildcard_map_vars
        if re.search(
            rf"\b{re.escape(variable)}\s*\.\s*(?:put|putAll|replace|replaceAll|compute|computeIfAbsent|computeIfPresent|merge)\s*\(",
            block,
        )
    ))


def normalize_writable_map_mutations(block: str) -> str:
    """Deterministically make mutated dynamic-map locals writable.

    LLMs frequently mirror production declarations such as ``Map<?, ?>`` and
    then attempt a branch mutation with ``put``. Java rejects non-null inserts
    through wildcard capture. For variables that are actually mutated, rewrite
    only the local declaration and its leading Map cast to ``Map<String,Object>``.
    Dynamic map fixtures are String-keyed by construction, so this is a verified
    code-generation normalization rather than repository fact inference.
    """
    normalized = block
    for variable in _mutated_wildcard_map_variables(block):
        declaration = re.compile(
            rf"(?P<qual>java\.util\.)?Map\s*<\s*[^>]*\?[^>]*>\s+"
            rf"{re.escape(variable)}\s*=\s*(?P<initializer>.*?);",
            flags=re.DOTALL,
        )

        def replace_declaration(match: re.Match[str]) -> str:
            qualifier = match.group("qual") or ""
            initializer = match.group("initializer")
            initializer = re.sub(
                r"^\s*\(\s*(?:java\.util\.)?Map(?:\s*<[^>]*>)?\s*\)",
                "(Map<String, Object>)",
                initializer,
                count=1,
                flags=re.DOTALL,
            )
            return (
                f"{qualifier}Map<String, Object> {variable} ="
                f"{initializer};"
            )

        normalized = declaration.sub(replace_declaration, normalized, count=1)
    return normalized


def cut_variable_name(class_name: str) -> str:
    if not class_name:
        return "classUnderTest"
    if len(class_name) >= 2 and class_name[:2].isupper():
        return class_name[0].lower() + class_name[1:]
    return class_name[0].lower() + class_name[1:]


def _format_import(value: str) -> str | None:
    text = (value or "").strip().rstrip(";")
    if not text:
        return None
    if text.startswith("import "):
        return text + ";"
    if text.startswith("static "):
        return "import " + text + ";"
    return "import " + text + ";"


def _class_package(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[0] if "." in fqcn else ""


def _imports(ctx: GenerationContext) -> list[str]:
    imports: set[str] = set()
    for value in ctx.template.required_imports or []:
        rendered = _format_import(value)
        if rendered:
            imports.add(rendered)
    for value in ctx.source_imports or []:
        rendered = _format_import(value)
        if rendered:
            imports.add(rendered)

    cut_package = _class_package(ctx.symbol.fqcn)
    if cut_package and cut_package != ctx.test_package:
        imports.add(f"import {ctx.symbol.fqcn};")

    execution = ctx.execution_context
    if execution is not None:
        for dependency in execution.dependencies:
            if dependency.fqcn and _class_package(dependency.fqcn) != ctx.test_package:
                imports.add(f"import {dependency.fqcn};")
        simple_counts: dict[str, int] = {}
        for schema in execution.payload_schemas:
            simple_counts[schema.type_name] = simple_counts.get(schema.type_name, 0) + 1
        for schema in execution.payload_schemas:
            # Two application classes with the same simple name cannot both be
            # imported.  Their fixture methods use FQCNs instead.
            if simple_counts.get(schema.type_name, 0) > 1:
                continue
            if schema.fqcn and _class_package(schema.fqcn) != ctx.test_package:
                imports.add(f"import {schema.fqcn};")
        for fixture in execution.fixtures:
            if not fixture.supported:
                continue
            for value in fixture.imports:
                rendered = _format_import(value)
                if rendered:
                    imports.add(rendered)

    # JDK staples the generated tests routinely use for fixtures/collections and
    # were previously left unimported (Collections/BigDecimal/Map/HashMap/List...).
    imports.update({
        "import java.util.List;",
        "import java.util.ArrayList;",
        "import java.util.Map;",
        "import java.util.HashMap;",
        "import java.util.LinkedHashMap;",
        "import java.util.LinkedHashSet;",
        "import java.util.Collections;",
        "import java.util.Optional;",
        "import java.math.BigDecimal;",
        "import java.math.BigInteger;",
    })

    # Nested payload/property types (e.g. CustomerParam, StatusEnum, TravelPremiumEntry)
    # are fields INSIDE a schema, not top-level schemas, so the schema.fqcn loop above
    # misses them and the generated body references a type with no import.
    #
    # The CUT compiles, so ITS import list is correct and complete by definition. Any
    # application type the test could legitimately use is a type the CUT already imports.
    # So re-emit ALL of the CUT's non-JDK imports -- no package guessing. Unused imports
    # are harmless (Java allows them; they do not fail compilation), whereas a missing
    # one is a hard compile error. Bias to over-import.
    for value in ctx.source_imports or []:
        rendered = _format_import(value)
        if not rendered:
            continue
        body = rendered[len("import "):].lstrip()
        if body.startswith("static "):
            body = body[len("static "):]
        # Skip java.*/javax.* (already covered by the JDK staples above and by
        # wildcard-safe defaults); keep everything else (the application types).
        if body.startswith(("java.", "javax.")):
            continue
        imports.add(rendered)

    # Stable method-level tests always need these even if the template omitted one.
    imports.update({
        "import org.junit.jupiter.api.Test;",
        "import org.junit.jupiter.api.extension.ExtendWith;",
        "import org.mockito.InjectMocks;",
        "import org.mockito.Mock;",
        "import org.mockito.junit.jupiter.MockitoExtension;",
        "import static org.junit.jupiter.api.Assertions.*;",
        "import static org.mockito.Mockito.*;",
    })
    has_injectable_configuration = bool(
        execution is not None
        and any(
            not field.static and field.test_value is not None
            for field in execution.configuration_fields
        )
    )
    if execution is not None and execution.target_kind.value == "controller":
        imports.update({
            "import org.junit.jupiter.api.BeforeEach;",
            "import org.springframework.test.web.servlet.MockMvc;",
            "import org.springframework.test.web.servlet.setup.MockMvcBuilders;",
            "import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;",
            "import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;",
        })
    if has_injectable_configuration:
        imports.update({
            "import org.junit.jupiter.api.BeforeEach;",
            "import org.springframework.test.util.ReflectionTestUtils;",
        })
    return sorted(imports, key=lambda item: (item.startswith("import static "), item))


def _fixture_closure(ctx: GenerationContext):
    execution = ctx.execution_context
    if execution is None:
        return ()
    by_id = {fixture.fixture_id: fixture for fixture in execution.fixtures if fixture.supported}
    roots = {
        fixture_id
        for method in execution.methods
        for fixture_id in method.required_fixture_ids
        if fixture_id in by_id
    }
    if not roots:
        roots = set(by_id)
    selected: set[str] = set()
    stack = list(roots)
    while stack:
        fixture_id = stack.pop()
        if fixture_id in selected or fixture_id not in by_id:
            continue
        selected.add(fixture_id)
        stack.extend(by_id[fixture_id].dependent_fixture_ids)
    return tuple(fixture for fixture in execution.fixtures if fixture.fixture_id in selected and fixture.supported)


def _inject_import_lines(code: str, import_lines: set[str]) -> str:
    if not import_lines:
        return code
    normalized_existing = {
        line.strip().rstrip(";")
        for line in code.splitlines()
        if line.strip().startswith("import ")
    }
    missing = sorted(
        line for line in import_lines
        if line.strip().rstrip(";") not in normalized_existing
    )
    if not missing:
        return code
    package_match = re.search(r"(?m)^\s*package\s+[^;]+;\s*", code)
    insertion = "\n".join(missing) + "\n"
    if package_match:
        return code[:package_match.end()] + "\n" + insertion + code[package_match.end():]
    return insertion + code


def inject_reusable_fixtures(code: str, ctx: GenerationContext) -> str:
    """Insert deterministic recursive fixture helpers into class-wide LLM output.

    Controller/ServiceImpl skeletons already contain these helpers. Utility,
    validator, mapper and other structured-input classes use the legacy
    whole-class generation path, so their final Java file must receive the same
    verified helpers before finalization/compilation.
    """
    execution = ctx.execution_context
    if execution is None or execution.target_kind.methodwise_generation:
        return code
    fixtures = _fixture_closure(ctx)
    if not fixtures:
        return code

    import_lines: set[str] = set()
    package_name = ctx.test_package or ""
    simple_counts: dict[str, int] = {}
    for schema in execution.payload_schemas:
        simple_counts[schema.type_name] = simple_counts.get(schema.type_name, 0) + 1
    for schema in execution.payload_schemas:
        if (
            schema.fqcn
            and simple_counts.get(schema.type_name, 0) == 1
            and _class_package(schema.fqcn) != package_name
        ):
            import_lines.add(f"import {schema.fqcn};")
    for fixture in fixtures:
        for value in fixture.imports:
            rendered = _format_import(value)
            if rendered:
                import_lines.add(rendered)

    out = _inject_import_lines(code, import_lines)
    def has_fixture_declaration(method_name: str) -> bool:
        return bool(re.search(
            rf"(?m)^\s*(?:public|protected|private)?\s*(?:static\s+)?"
            rf"[A-Za-z_$][\w$<>,.? \[\]]*\s+{re.escape(method_name)}\s*\(",
            out,
        ))

    missing_sources = [
        fixture.method_source.strip()
        for fixture in fixtures
        if fixture.method_source.strip() and not has_fixture_declaration(fixture.method_name)
    ]
    if not missing_sources:
        return out
    closing = out.rfind("}")
    if closing < 0:
        return out
    block = "\n\n    // Reusable parser-derived fixtures. Tests mutate only branch-specific fields.\n\n"
    block += "\n\n".join(missing_sources) + "\n"
    return out[:closing].rstrip() + block + out[closing:]


def build_test_skeleton(ctx: GenerationContext) -> str:
    execution = ctx.execution_context
    if execution is None or execution.target_kind.value not in {"controller", "service-impl"}:
        raise ValueError("method-wise skeleton is only valid for Controller/ServiceImpl")

    lines = [f"package {ctx.test_package};", ""]
    lines.extend(_imports(ctx))
    lines.extend(["", "@ExtendWith(MockitoExtension.class)", f"class {ctx.test_class_name} {{", ""])

    seen_fields: set[str] = set()
    for dependency in execution.dependencies:
        field_name = dependency.field_name or dependency.parameter_name
        if not field_name or field_name in seen_fields or not dependency.mockable:
            continue
        seen_fields.add(field_name)
        lines.extend([
            "    @Mock",
            f"    private {dependency.declared_type} {field_name};",
            "",
        ])

    cut_var = cut_variable_name(ctx.symbol.name)
    lines.extend([
        "    @InjectMocks",
        f"    private {ctx.symbol.name} {cut_var};",
        "",
    ])

    injectable_configuration = [
        field for field in execution.configuration_fields
        if not field.static and field.test_value is not None
    ]

    if execution.target_kind.value == "controller":
        lines.extend([
            "    private MockMvc mockMvc;",
            "",
        ])

    if execution.target_kind.value == "controller" or injectable_configuration:
        lines.extend([
            "    @BeforeEach",
            "    void setUp() {",
        ])
        for field in injectable_configuration:
            lines.append(
                f'        ReflectionTestUtils.setField({cut_var}, "{field.field_name}", {field.test_value});'
            )
        if execution.target_kind.value == "controller":
            lines.append(f"        mockMvc = MockMvcBuilders.standaloneSetup({cut_var}).build();")
        lines.extend([
            "    }",
            "",
        ])

    fixture_sources = [
        fixture.method_source.strip()
        for fixture in execution.fixtures
        if fixture.supported and fixture.method_source.strip()
    ]
    if fixture_sources:
        lines.append("    // Reusable parser-derived fixtures. Tests must mutate only target-branch fields.")
        lines.append("")
        for source in fixture_sources:
            lines.extend(source.splitlines())
            lines.append("")

    lines.append("}")
    return "\n".join(lines) + "\n"


def _strip_fences(raw: str) -> str:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:java)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1).strip() if fenced else text


def _matching_brace(text: str, open_index: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = open_index
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            index += 2
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def extract_test_method_block(raw: str) -> str:
    """Extract only @Test methods; class wrappers, imports, and fields are discarded."""
    text = _strip_fences(raw)
    methods: list[str] = []
    cursor = 0
    while True:
        match = re.search(r"(?m)^[ \t]*@Test\b", text[cursor:])
        if not match:
            break
        annotation_start = cursor + match.start()
        open_brace = text.find("{", annotation_start)
        if open_brace < 0:
            break
        close_brace = _matching_brace(text, open_brace)
        if close_brace is None:
            break
        method_text = text[annotation_start:close_brace + 1].strip()
        methods.append(method_text)
        cursor = close_brace + 1
    return "\n\n".join(methods).strip()


def test_method_names(code: str) -> tuple[str, ...]:
    names = re.findall(
        r"@Test(?:\s*\([^)]*\))?\s+(?:public\s+|protected\s+|private\s+)?"
        r"(?:static\s+)?void\s+([A-Za-z_$][\w$]*)\s*\(",
        code or "",
        flags=re.MULTILINE,
    )
    return tuple(names)


def _has_collaborator_stub(block: str, field: str, method_name: str) -> bool:
    """Return whether the generated suite stubs one exact collaborator call.

    Supports standard Mockito and BDDMockito forms. This is intentionally a
    suite-level check: branch-specific tests may omit downstream stubs when an
    earlier collaborator throws, but a required unconditional collaborator must
    not be absent from the complete generated method suite.
    """
    field_re = re.escape(field)
    method_re = re.escape(method_name)
    patterns = (
        rf"\b(?:when|given)\s*\(\s*{field_re}\s*\.\s*{method_re}\s*\(",
        rf"\b(?:doReturn|doThrow|doAnswer|doNothing)\s*\([^;]*?\)\s*\.\s*when\s*\(\s*{field_re}\s*\)\s*\.\s*{method_re}\s*\(",
        rf"\b(?:willReturn|willThrow|willAnswer|willDoNothing)\s*\([^;]*?\)\s*\.\s*given\s*\(\s*{field_re}\s*\)\s*\.\s*{method_re}\s*\(",
    )
    return any(re.search(pattern, block, flags=re.DOTALL) for pattern in patterns)


def _has_primary_non_void_stub(block: str, field: str, method_name: str) -> bool:
    field_re = re.escape(field)
    method_re = re.escape(method_name)
    patterns = (
        rf"\b(?:when|given)\s*\(\s*{field_re}\s*\.\s*{method_re}\s*\(",
        rf"\b(?:doReturn|doAnswer)\s*\([^;]*?\)\s*\.\s*when\s*\(\s*{field_re}\s*\)\s*\.\s*{method_re}\s*\(",
        rf"\b(?:willReturn|willAnswer)\s*\([^;]*?\)\s*\.\s*given\s*\(\s*{field_re}\s*\)\s*\.\s*{method_re}\s*\(",
    )
    return any(re.search(pattern, block, flags=re.DOTALL) for pattern in patterns)


def _has_primary_void_stub(block: str, field: str, method_name: str) -> bool:
    field_re = re.escape(field)
    method_re = re.escape(method_name)
    patterns = (
        rf"\bdoNothing\s*\(\s*\)\s*\.\s*when\s*\(\s*{field_re}\s*\)\s*\.\s*{method_re}\s*\(",
        rf"\bwillDoNothing\s*\(\s*\)\s*\.\s*given\s*\(\s*{field_re}\s*\)\s*\.\s*{method_re}\s*\(",
    )
    return any(re.search(pattern, block, flags=re.DOTALL) for pattern in patterns)


def _missing_required_collaborator_stubs(
    method_context: MethodExecutionContext,
    block: str,
) -> tuple[str, ...]:
    """Find source-proven unconditional non-void calls omitted by the suite.

    These calls execute on every normal traversal of the selected public path.
    Accepting a suite that omits all stubbing for them produces null/default mock
    returns and is the exact failure seen for ``aesEncUtils.encode`` and
    ``iQuotationClient.getQuotationByRefNoTransType`` inside ``getQuotation``.
    """
    required: list[tuple[str, str, bool]] = []
    seen: set[tuple[str, str, bool]] = set()
    selected_primary = method_context.selected_primary_path is not None
    for invocation in method_context.dependency_invocations:
        contract = invocation.contract
        return_type = (contract.return_type or "").strip()
        is_void = return_type in {"", "void"}
        if (invocation.branch_id is not None and not selected_primary) or (is_void and not selected_primary):
            continue
        key = (invocation.dependency_field, invocation.method_name, is_void)
        if key in seen:
            continue
        seen.add(key)
        required.append(key)
    return tuple(
        f"{field}.{method_name}"
        for field, method_name, is_void in required
        if not (
            _has_primary_void_stub(block, field, method_name)
            if selected_primary and is_void
            else _has_primary_non_void_stub(block, field, method_name)
            if selected_primary
            else _has_collaborator_stub(block, field, method_name)
        )
    )


def validate_method_block(
    ctx: GenerationContext,
    production_method: MethodSig,
    method_context: MethodExecutionContext,
    block: str,
    existing_names: set[str],
) -> MethodBlockValidation:
    if not block.strip() or "@Test" not in block:
        return MethodBlockValidation(False, "no @Test methods returned")
    if re.search(r"(?m)^\s*(package|import)\b|\bclass\s+\w+", block):
        return MethodBlockValidation(False, "method response contains package/import/class wrapper")

    names = test_method_names(block)
    if not names:
        return MethodBlockValidation(False, "unable to identify generated test method names")
    duplicates = set(names) & existing_names
    if duplicates:
        return MethodBlockValidation(False, f"duplicate test method names: {', '.join(sorted(duplicates))}")
    if len(names) != len(set(names)):
        return MethodBlockValidation(False, "duplicate test names within method response")

    service_target = (
        ctx.execution_context is not None
        and ctx.execution_context.target_kind.value == "service-impl"
    )
    service_primary = service_target and method_context.selected_primary_path is not None
    if service_primary and len(names) != 1:
        return MethodBlockValidation(
            False,
            f"initial ServiceImpl generation must contain exactly one primary @Test; found {len(names)}",
        )
    if service_primary and re.search(
        r"@(?:ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b",
        block,
    ):
        return MethodBlockValidation(
            False,
            "initial ServiceImpl generation may contain only one ordinary @Test",
        )

    cut_var = cut_variable_name(ctx.symbol.name)
    called_cut_methods = set(re.findall(rf"\b{re.escape(cut_var)}\s*\.\s*([A-Za-z_$][\w$]*)\s*\(", block))
    # A test may legitimately name OTHER public methods of the CUT -- especially a
    # "funnel" class with one public entry point that delegates to sibling public
    # methods. Only reject a call to a method that is actually PRIVATE (calling a
    # private method directly is a real violation). Public sibling calls are
    # advisory; javac and the runtime arbitrate.
    public_cut_methods = {
        m.name for m in (ctx.symbol.methods or []) if getattr(m, "is_public", False)
    }
    other_calls = called_cut_methods - {production_method.name}
    # A call to another method on the CUT is NOT terminal. For a funnel class
    # (one public entry delegating to helpers) the model naturally references the
    # helpers. If a call is genuinely to a private method, javac rejects that one
    # line and repair fixes it -- far better than dropping the whole suite and
    # generating nothing. So: advisory, never terminal. javac arbitrates.
    private_other_calls = sorted(m for m in other_calls if m not in public_cut_methods)
    advisory_cut_calls = sorted(other_calls & public_cut_methods)
    if (
        service_target
        and production_method.name not in called_cut_methods
    ):
        return MethodBlockValidation(False, "ServiceImpl tests do not call the selected CUT method")
    if re.search(rf"\b(?:when|verify)\s*\(\s*{re.escape(cut_var)}\b", block) or re.search(
        rf"\.when\s*\(\s*{re.escape(cut_var)}\b", block
    ):
        return MethodBlockValidation(False, "response stubs or verifies the class under test")

    if service_primary:
        cut_invocation_count = len(re.findall(
            rf"\b{re.escape(cut_var)}\s*\.\s*{re.escape(production_method.name)}\s*\(",
            block,
        ))
        if cut_invocation_count != 1:
            return MethodBlockValidation(
                False,
                f"primary ServiceImpl test must invoke the selected entry method exactly once; found {cut_invocation_count}",
            )
        cut_capture = re.search(
            rf"\b(?:var|[A-Za-z_$][\w$<>,.?\[\] ]*)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            rf"{re.escape(cut_var)}\s*\.\s*{re.escape(production_method.name)}\s*\(",
            block,
        )
        if cut_capture is None:
            return MethodBlockValidation(
                False,
                "primary ServiceImpl test must capture the selected CUT method return value",
            )
        response_variable = cut_capture.group(1)
        assertion_calls = re.findall(
            r"(?<![A-Za-z0-9_$])(?:Assertions\s*\.\s*)?(assert[A-Za-z0-9_$]*)\s*\((.*?)\)\s*;",
            block,
            flags=re.DOTALL,
        )
        if len(assertion_calls) != 1:
            return MethodBlockValidation(
                False,
                f"primary ServiceImpl test must contain exactly one assertion; found {len(assertion_calls)}",
            )
        assertion_name, assertion_argument = assertion_calls[0]
        if assertion_name != "assertNotNull" or assertion_argument.strip() != response_variable:
            return MethodBlockValidation(
                False,
                "primary ServiceImpl test assertion must be only assertNotNull(capturedResponse)",
            )
        if re.search(r"(?<![A-Za-z0-9_$])fail\s*\(", block):
            return MethodBlockValidation(
                False,
                "primary ServiceImpl test may not contain fail(); assertNotNull(response) is the only assertion",
            )
        if method_context.selected_primary_path is not None and response_variable != "response":
            return MethodBlockValidation(
                False,
                "primary ServiceImpl test must name the captured return value response and use assertNotNull(response)",
            )

    missing_stubs = _missing_required_collaborator_stubs(method_context, block)
    if missing_stubs:
        return MethodBlockValidation(
            False,
            "response omits required collaborator stubs across the selected public/helper path: "
            + ", ".join(missing_stubs),
        )

    # Map<?, ?> is readable but cannot be mutated because neither key nor value
    # has a writable captured type. Catch this before javac creates a .failing
    # file and route the block through the bounded structural repair.
    mutated_wildcard_maps = list(_mutated_wildcard_map_variables(block))
    if mutated_wildcard_maps:
        return MethodBlockValidation(
            False,
            "response mutates wildcard Map<?, ?> variable(s): "
            + ", ".join(mutated_wildcard_maps)
            + "; use Map<String, Object> (with an explicit checked/unchecked cast from the verified fixture) before mutation",
        )

    # ------------------------------------------------------------------
    # ADVISORY ONLY.  Previously this rejected the block outright, which meant a
    # single analyzer extraction gap (chained call, lambda, stream, ternary)
    # silently destroyed a whole method's tests with no repair path.  The
    # analyzer under-approximates Java; it must not outrank javac.
    # ------------------------------------------------------------------
    warnings: list[str] = []
    allowed_calls: dict[str, set[str]] = {}
    for invocation in method_context.dependency_invocations:
        allowed_calls.setdefault(invocation.dependency_field, set()).add(invocation.method_name)
    for dependency in ctx.execution_context.dependencies if ctx.execution_context else ():
        field = dependency.field_name or dependency.parameter_name
        if not field:
            continue
        observed = set(re.findall(rf"\b{re.escape(field)}\s*\.\s*([A-Za-z_$][\w$]*)\s*\(", block))
        invalid = observed - allowed_calls.get(field, set())
        if invalid:
            warnings.append(
                f"unobserved collaborator calls on {field}: {', '.join(sorted(invalid))} "
                f"(advisory; deferred to javac)"
            )

    # Duplicate private helpers across concurrently generated blocks would be a
    # real compile error, so this one IS terminal.
    helpers = set(re.findall(r"(?m)^\s*private\s+[\w<>\[\],.\s]+\s+(\w+)\s*\(", block))
    if helpers:
        return MethodBlockValidation(
            False,
            f"response defines private helper methods: {', '.join(sorted(helpers))}; "
            f"call existing fixture builders instead",
        )

    if advisory_cut_calls:
        warnings.append(
            "response also calls sibling public CUT methods: "
            + ", ".join(advisory_cut_calls) + " (advisory; funnel class)"
        )
    if private_other_calls:
        warnings.append(
            "response references non-public CUT methods: "
            + ", ".join(private_other_calls)
            + " (advisory; if truly private, javac will flag the direct call and "
            "repair will route it through the public method)"
        )

    return MethodBlockValidation(True, test_names=names, warnings=tuple(warnings))


def insert_method_block(suite: str, method_id: str, block: str) -> str:
    marker = f"{_BEGIN}{method_id}\n"
    end_marker = f"{_END}{method_id}\n"
    indented = "\n".join("    " + line if line.strip() else "" for line in block.splitlines())
    insertion = f"    {marker}{indented}\n    {end_marker}"
    close = suite.rfind("}")
    if close < 0:
        raise ValueError("test skeleton has no closing class brace")
    prefix = suite[:close].rstrip()
    return prefix + "\n\n" + insertion.rstrip() + "\n}\n"


def method_block_for_id(suite: str, method_id: str) -> str | None:
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(_BEGIN + method_id)}\s*$\n(.*?)^\s*{re.escape(_END + method_id)}\s*$"
    )
    match = pattern.search(suite)
    return match.group(1) if match else None


def replace_method_block(suite: str, method_id: str, block: str) -> str:
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(_BEGIN + method_id)}\s*$\n.*?^\s*{re.escape(_END + method_id)}\s*$"
    )
    indented = "\n".join("    " + line if line.strip() else "" for line in block.splitlines())
    replacement = f"    {_BEGIN}{method_id}\n{indented}\n    {_END}{method_id}"
    updated, count = pattern.subn(replacement, suite, count=1)
    if count != 1:
        raise ValueError(f"method block not found: {method_id}")
    return updated


def owner_method_id_for_line(suite: str, line_number: int | None) -> str | None:
    if line_number is None:
        return None
    current: str | None = None
    for index, line in enumerate(suite.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(_BEGIN):
            current = stripped[len(_BEGIN):].strip()
        elif stripped.startswith(_END):
            current = None
        if index == line_number:
            return current
    return None


# ---------------------------------------------------------------------------
# Complete-class validation / repair source mapping
# ---------------------------------------------------------------------------


def _line_for_offset(source: str, offset: int) -> int:
    return source.count("\n", 0, max(0, offset)) + 1


def _skip_java_trivia(source: str, offset: int, end: int) -> int:
    """Skip whitespace and comments between generated class members."""
    cursor = offset
    while cursor < end:
        if source[cursor].isspace():
            cursor += 1
            continue
        if source.startswith("//", cursor):
            newline = source.find("\n", cursor + 2, end)
            cursor = end if newline < 0 else newline + 1
            continue
        if source.startswith("/*", cursor):
            close = source.find("*/", cursor + 2, end)
            cursor = end if close < 0 else close + 2
            continue
        break
    return cursor


def _scan_member_end(source: str, start: int, class_close: int) -> tuple[int, bool] | None:
    """Return (exclusive end, has_body) for one top-level generated member."""
    cursor = start
    parens = 0
    brackets = 0
    quote: str | None = None
    escaped = False
    while cursor < class_close:
        char = source[cursor]
        nxt = source[cursor + 1] if cursor + 1 < class_close else ""
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            cursor += 1
            continue
        if char in {'"', "'"}:
            quote = char
            cursor += 1
            continue
        if char == "/" and nxt == "/":
            newline = source.find("\n", cursor + 2, class_close)
            cursor = class_close if newline < 0 else newline + 1
            continue
        if char == "/" and nxt == "*":
            close = source.find("*/", cursor + 2, class_close)
            cursor = class_close if close < 0 else close + 2
            continue
        if char == "(":
            parens += 1
        elif char == ")" and parens:
            parens -= 1
        elif char == "[":
            brackets += 1
        elif char == "]" and brackets:
            brackets -= 1
        elif char == "{" and parens == 0 and brackets == 0:
            close = _matching_brace(source, cursor)
            if close is None or close > class_close:
                return None
            return close + 1, True
        elif char == ";" and parens == 0 and brackets == 0:
            return cursor + 1, False
        cursor += 1
    return None


def _member_name(header: str, *, has_body: bool) -> str:
    if has_body:
        calls = list(re.finditer(r"([A-Za-z_$][\w$]*)\s*\(", header))
        if calls:
            return calls[-1].group(1)
        nested = re.search(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)", header)
        if nested:
            return nested.group(1)
        return "class_member"
    declaration = re.sub(r"=[\s\S]*$", "", header).strip()
    names = re.findall(r"\b([A-Za-z_$][\w$]*)\b", declaration)
    return names[-1] if names else "field"


def _method_scope_kind(source: str, name: str, fixture_names: set[str]) -> GeneratedScopeKind:
    if re.search(r"@(?:org\.junit\.jupiter\.api\.)?Test\b", source):
        return GeneratedScopeKind.TEST_METHOD
    if re.search(r"@(?:BeforeEach|BeforeAll|AfterEach|AfterAll)\b", source):
        return GeneratedScopeKind.SETUP_METHOD
    if name in fixture_names:
        return GeneratedScopeKind.FIXTURE_HELPER
    if (
        re.search(r"\b(?:when|given|doReturn|doThrow|doAnswer|doNothing)\s*\(", source)
        or re.match(r"(?i)^(?:stub|mock|given|configureStub|prepareStub)", name)
    ):
        return GeneratedScopeKind.STUB_HELPER
    if re.match(r"(?i)^(?:create|build|valid|default|fixture|sample|new|make|get)", name):
        return GeneratedScopeKind.FIXTURE_HELPER
    return GeneratedScopeKind.OTHER_CLASS_DECLARATION


def generated_scopes(
    source: str,
    *,
    fixture_names: set[str] | tuple[str, ...] = (),
) -> tuple[GeneratedScope, ...]:
    """Map imports and every top-level generated declaration to a stable repair unit."""
    scopes: list[GeneratedScope] = []
    used_ids: dict[str, int] = {}

    def append_scope(
        kind: GeneratedScopeKind,
        name: str,
        start: int,
        end: int,
        owner: str | None = None,
    ) -> None:
        base = f"{kind.value}:{name}"
        occurrence = used_ids.get(base, 0) + 1
        used_ids[base] = occurrence
        scope_id = base if occurrence == 1 else f"{base}#{occurrence}"
        scopes.append(GeneratedScope(
            scope_id=scope_id,
            kind=kind,
            name=name,
            start_line=_line_for_offset(source, start),
            end_line=_line_for_offset(source, max(start, end - 1)),
            start_offset=start,
            end_offset=end,
            source=source[start:end].strip(),
            owner_method_id=owner,
        ))

    imports = list(re.finditer(r"(?m)^[ \t]*import\s+[^;]+;[ \t]*$", source))
    if imports:
        append_scope(
            GeneratedScopeKind.IMPORT_BLOCK,
            "imports",
            imports[0].start(),
            imports[-1].end(),
        )

    package = re.search(r"(?m)^[ \t]*package\s+[^;]+;[ \t]*$", source)
    if package:
        append_scope(
            GeneratedScopeKind.OTHER_CLASS_DECLARATION,
            "package",
            package.start(),
            package.end(),
        )

    class_match = re.search(r"\bclass\s+([A-Za-z_$][\w$]*)[^\{]*\{", source)
    if class_match is None:
        return tuple(sorted(scopes, key=lambda item: item.start_offset))
    class_open = source.find("{", class_match.start())
    class_close = _matching_brace(source, class_open)
    if class_close is None:
        class_close = len(source)
    class_line_start = source.rfind("\n", 0, class_match.start()) + 1
    append_scope(
        GeneratedScopeKind.OTHER_CLASS_DECLARATION,
        class_match.group(1),
        class_line_start,
        class_open + 1,
    )

    cursor = class_open + 1
    fixture_set = set(fixture_names)
    while cursor < class_close:
        start = _skip_java_trivia(source, cursor, class_close)
        if start >= class_close:
            break
        scanned = _scan_member_end(source, start, class_close)
        if scanned is None:
            break
        end, has_body = scanned
        member_source = source[start:end]
        header = member_source[:member_source.find("{")] if has_body else member_source
        name = _member_name(header, has_body=has_body)
        if has_body:
            kind = _method_scope_kind(member_source, name, fixture_set)
        else:
            kind = (
                GeneratedScopeKind.MOCK_DECLARATION
                if re.search(r"@(?:Mock|MockBean|MockitoBean)\b", member_source)
                else GeneratedScopeKind.CLASS_FIELD
            )
        owner = owner_method_id_for_line(source, _line_for_offset(source, start))
        append_scope(kind, name, start, end, owner)
        cursor = end

    return tuple(sorted(scopes, key=lambda item: item.start_offset))


def scope_for_line(
    source: str,
    line_number: int | None,
    *,
    fixture_names: set[str] | tuple[str, ...] = (),
) -> GeneratedScope | None:
    if line_number is None:
        return None
    scopes = generated_scopes(source, fixture_names=fixture_names)
    containing = [
        scope for scope in scopes
        if scope.start_line <= line_number <= scope.end_line
    ]
    if containing:
        return min(containing, key=lambda item: item.end_offset - item.start_offset)
    lines = source.splitlines(keepends=True)
    if line_number < 1 or line_number > len(lines):
        return None
    start = sum(len(line) for line in lines[:line_number - 1])
    end = start + len(lines[line_number - 1].rstrip("\r\n"))
    return GeneratedScope(
        scope_id=f"{GeneratedScopeKind.OTHER_CLASS_DECLARATION.value}:line-{line_number}",
        kind=GeneratedScopeKind.OTHER_CLASS_DECLARATION,
        name=f"line-{line_number}",
        start_line=line_number,
        end_line=line_number,
        start_offset=start,
        end_offset=end,
        source=source[start:end].strip(),
        owner_method_id=owner_method_id_for_line(source, line_number),
    )


def offending_statement(source: str, line_number: int | None) -> str:
    if line_number is None:
        return "<source location unavailable>"
    lines = source.splitlines()
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip()
    return "<source line outside generated file>"


def extract_corrected_scope(raw: str, scope: GeneratedScope) -> str | None:
    """Accept only one complete replacement for the requested source scope."""
    text = _strip_fences(raw or "").strip()
    if not text:
        return None
    if scope.kind is GeneratedScopeKind.IMPORT_BLOCK:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or any(not re.match(r"^import\s+[^;]+;$", line) for line in lines):
            return None
        return "\n".join(lines)
    if scope.name == "package":
        return text if re.fullmatch(r"package\s+[^;]+;", text) else None
    if re.search(r"(?m)^\s*package\s+", text) and scope.name != "package":
        return None
    if (
        re.search(r"\bclass\s+\w+", text)
        and scope.kind is not GeneratedScopeKind.OTHER_CLASS_DECLARATION
    ):
        return None
    if scope.kind in {
        GeneratedScopeKind.TEST_METHOD,
        GeneratedScopeKind.SETUP_METHOD,
        GeneratedScopeKind.FIXTURE_HELPER,
        GeneratedScopeKind.STUB_HELPER,
        GeneratedScopeKind.OTHER_CLASS_DECLARATION,
    } and "(" in scope.source:
        if not re.search(rf"\b{re.escape(scope.name)}\s*\(", text):
            return None
        open_brace = text.find("{")
        if open_brace < 0 or _matching_brace(text, open_brace) != len(text.rstrip()) - 1:
            return None
        if scope.kind is GeneratedScopeKind.TEST_METHOD and not re.search(r"@Test\b", text):
            return None
        return text
    if scope.kind in {GeneratedScopeKind.CLASS_FIELD, GeneratedScopeKind.MOCK_DECLARATION}:
        if (
            not re.search(rf"\b{re.escape(scope.name)}\b", text)
            or not text.rstrip().endswith(";")
            or text.count(";") != 1
            or "{" in text
            or "}" in text
        ):
            return None
        return text
    if (
        scope.kind is GeneratedScopeKind.OTHER_CLASS_DECLARATION
        and re.search(r"\b(?:class|interface|enum|record)\s+", scope.source)
    ):
        if (
            not re.search(
                rf"\b(?:class|interface|enum|record)\s+{re.escape(scope.name)}\b",
                text,
            )
            or not text.rstrip().endswith("{")
            or "}" in text
        ):
            return None
        return text
    if scope.kind is GeneratedScopeKind.OTHER_CLASS_DECLARATION and "\n" in text:
        return None
    return text


def replace_generated_scopes(
    source: str,
    replacements: dict[str, tuple[GeneratedScope, str]],
) -> str:
    """Apply all accepted repairs together, from the bottom of the class upward."""
    updated = source
    ordered = sorted(replacements.values(), key=lambda item: item[0].start_offset, reverse=True)
    for scope, replacement in ordered:
        updated = updated[:scope.start_offset] + replacement.strip() + updated[scope.end_offset:]
    return updated


def validate_scoped_class_repair(
    original: str,
    candidate: str,
    *,
    modified_scope_ids: set[str],
    strict_serviceimpl: bool,
    forbid_mockito_weakening: bool = False,
) -> str | None:
    """Return a rejection reason when a scoped repair touches protected tests."""
    original_scopes = generated_scopes(original)
    candidate_scopes = generated_scopes(candidate)
    original_tests = {
        scope.name: scope for scope in original_scopes
        if scope.kind is GeneratedScopeKind.TEST_METHOD
    }
    candidate_tests = {
        scope.name: scope for scope in candidate_scopes
        if scope.kind is GeneratedScopeKind.TEST_METHOD
    }
    if set(original_tests) != set(candidate_tests):
        return "repair removed, renamed, or added a generated test method"
    if re.search(r"@(?:Disabled|Ignore)\b", candidate):
        return "repair disabled or skipped a generated test"
    for name, before in original_tests.items():
        after = candidate_tests[name]
        if before.scope_id not in modified_scope_ids and before.source.strip() != after.source.strip():
            return f"repair modified unrelated passing test method {name}"
        if strict_serviceimpl and re.search(
            r"\bassertNotNull\s*\(\s*response\s*\)\s*;",
            before.source,
        ):
            assertions = re.findall(
                r"(?<![A-Za-z0-9_$])(?:Assertions\s*\.\s*)?(assert[A-Za-z0-9_$]*)\s*\((.*?)\)\s*;",
                after.source,
                flags=re.DOTALL,
            )
            if len(assertions) != 1 or assertions[0][0] != "assertNotNull" or assertions[0][1].strip() != "response":
                return f"repair weakened or replaced assertNotNull(response) in {name}"
            invocation_pattern = re.compile(
                r"\b(?:var|[A-Za-z_$][\w$<>,.?\[\] ]*)\s+response\s*=\s*"
                r"(?P<receiver>[A-Za-z_$][\w$]*)\s*\.\s*"
                r"(?P<method>[A-Za-z_$][\w$]*)\s*\("
            )
            before_invocation = invocation_pattern.search(before.source)
            after_invocation = invocation_pattern.search(after.source)
            if before_invocation is not None and (
                after_invocation is None
                or after_invocation.group("receiver") != before_invocation.group("receiver")
                or after_invocation.group("method") != before_invocation.group("method")
                or len(re.findall(
                    rf"\b{re.escape(before_invocation.group('receiver'))}\s*\.\s*"
                    rf"{re.escape(before_invocation.group('method'))}\s*\(",
                    after.source,
                )) != 1
            ):
                return f"repair removed or bypassed the selected ServiceImpl entry invocation in {name}"
    if forbid_mockito_weakening and (
        re.search(r"\blenient\s*\(", candidate)
        or re.search(r"Strictness\s*\.\s*LENIENT", candidate)
    ):
        return "repair attempted to disable Mockito strictness"
    return None
