"""Deterministic Controller/ServiceImpl test-class assembly.

The LLM generates only @Test methods for one production method.  Python owns the
class wrapper, imports, mocks, standalone MockMvc setup, method markers, and
incremental validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from junitforge.execution.models import MethodExecutionContext
from junitforge.models import GenerationContext, MethodSig

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
        for schema in execution.payload_schemas:
            if schema.fqcn and _class_package(schema.fqcn) != ctx.test_package:
                imports.add(f"import {schema.fqcn};")

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

    cut_var = cut_variable_name(ctx.symbol.name)
    called_cut_methods = set(re.findall(rf"\b{re.escape(cut_var)}\s*\.\s*([A-Za-z_$][\w$]*)\s*\(", block))
    forbidden_cut_calls = called_cut_methods - {production_method.name}
    if forbidden_cut_calls:
        return MethodBlockValidation(
            False,
            "response calls a different CUT method: " + ", ".join(sorted(forbidden_cut_calls)),
        )
    if (
        ctx.execution_context is not None
        and ctx.execution_context.target_kind.value == "service-impl"
        and production_method.name not in called_cut_methods
    ):
        return MethodBlockValidation(False, "ServiceImpl tests do not call the selected CUT method")
    if re.search(rf"\b(?:when|verify)\s*\(\s*{re.escape(cut_var)}\b", block) or re.search(
        rf"\.when\s*\(\s*{re.escape(cut_var)}\b", block
    ):
        return MethodBlockValidation(False, "response stubs or verifies the class under test")

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