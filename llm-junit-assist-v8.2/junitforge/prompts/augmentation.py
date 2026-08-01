"""Coverage augmentation prompt builder.

Used only when run_coverage=True and the engine tries to add more tests for
uncovered JaCoCo lines/branches.

This prompt must be stricter than initial generation because augmentation often
causes duplicate tests, invented methods, and non-compiling code if not bounded.
"""

from __future__ import annotations

from typing import Dict, List

from junitforge.execution.renderer import render_dependency_contracts, render_execution_context
from junitforge.models import GenerationContext
from junitforge.parser.collaborators import render_collaborator
from junitforge.prompts.system import build_system_prompt

MessagePayload = List[Dict[str, str]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_system_prompt(ctx: GenerationContext) -> str:
    prompt = build_system_prompt(ctx.stack, ctx.template) or ""

    test_class = ctx.test_class_name or "GeneratedTest"
    test_pkg = ctx.test_package or "com.fallback.test"

    return (
        prompt.replace("{TEST_CLASS_NAME}", test_class)
        .replace("{TEST_PACKAGE}", test_pkg)
    )


def _annotation_names(ctx: GenerationContext) -> set[str]:
    if not ctx.symbol:
        return set()
    return {a.split(".")[-1] for a in (ctx.symbol.annotations or [])}


def _template_kind(ctx: GenerationContext) -> str:
    if not ctx.template or not ctx.template.kind:
        return ""
    return str(getattr(ctx.template.kind, "value", ctx.template.kind))


def _is_entity_like(ctx: GenerationContext) -> bool:
    if not ctx.symbol:
        return False

    annos = _annotation_names(ctx)
    name = ctx.symbol.name or ""

    return (
        "Entity" in annos
        or "Embeddable" in annos
        or "MappedSuperclass" in annos
        or name.endswith("Entity")
    )


def _is_dto_like(ctx: GenerationContext) -> bool:
    if not ctx.symbol:
        return False

    annos = _annotation_names(ctx)
    name = ctx.symbol.name or ""

    spring_annos = {
        "Service",
        "Component",
        "Repository",
        "Controller",
        "RestController",
        "Configuration",
        "SpringBootApplication",
    }

    if annos & spring_annos:
        return False

    if _is_entity_like(ctx):
        return False

    return (
        name.endswith("Dto")
        or name.endswith("DTO")
        or name.endswith("Request")
        or name.endswith("Response")
        or name.endswith("Model")
        or "Data" in annos
        or "Getter" in annos
        or "Setter" in annos
        or "Builder" in annos
    )


def _is_controller_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    kind = _template_kind(ctx)
    return "Controller" in annos or "RestController" in annos or kind in {"webmvc", "webflux"}


def _is_repository_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    name = ctx.symbol.name if ctx.symbol else ""
    kind = _template_kind(ctx)
    return "Repository" in annos or name.endswith("Repository") or kind == "datajpa"


def _is_config_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    return bool(
        annos
        & {
            "Configuration",
            "SpringBootApplication",
            "AutoConfiguration",
            "EnableAutoConfiguration",
        }
    )


def _java_bean_suffix(field_name: str) -> str:
    """Correct JavaBean suffix from the actual Java field identifier only."""
    if not field_name:
        return ""

    if len(field_name) >= 2 and field_name[0].isupper() and field_name[1].isupper():
        return field_name

    return field_name[0].upper() + field_name[1:]




def _primitive_boolean(field_type: str | None) -> bool:
    return (field_type or "").strip() == "boolean"


def _bean_base(field_name: str, field_type: str | None) -> str:
    if _primitive_boolean(field_type) and field_name.startswith("is") and len(field_name) > 2 and field_name[2].isupper():
        return field_name[2:]
    return _java_bean_suffix(field_name)


def _getter_name(field_name: str, field_type: str | None) -> str:
    if _primitive_boolean(field_type):
        return f"is{_bean_base(field_name, field_type)}"
    return f"get{_java_bean_suffix(field_name)}"


def _setter_name(field_name: str, field_type: str | None) -> str:
    return f"set{_bean_base(field_name, field_type)}"


def _method_name_from_signature(sig: str) -> str:
    import re

    m = re.search(r"\b([A-Za-z_$][\w$]*)\s*\(", sig or "")
    return m.group(1) if m else sig


def _dedupe(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    for line in lines:
        clean = " ".join((line or "").strip().split())
        if not clean:
            continue

        if clean in seen:
            continue

        seen.add(clean)
        out.append(clean)

    return out


def _explicit_public_methods(ctx: GenerationContext) -> list[str]:
    if not ctx.symbol:
        return []

    try:
        methods = ctx.symbol.public_methods()
    except Exception:
        methods = []

    return [m.render() for m in methods]


def _allowed_methods_block(ctx: GenerationContext) -> str:
    """Render only methods the augmentation prompt is allowed to call.

    For Lombok entities/DTOs, synthesize accessor names from Java field names,
    not DB column names.
    """
    if not ctx.symbol:
        return "- (no class symbol available)"

    out = _explicit_public_methods(ctx)
    existing_names = {_method_name_from_signature(s) for s in out}

    annos = _annotation_names(ctx)
    source = ctx.cut_source or ""

    has_data = "Data" in annos or "@Data" in source
    has_getter = has_data or "Getter" in annos or "@Getter" in source
    has_setter = has_data or "Setter" in annos or "@Setter" in source
    has_builder = "Builder" in annos or "@Builder" in source
    # @Data does NOT imply constructor generation.
    has_no_args = "NoArgsConstructor" in annos or "@NoArgsConstructor" in source
    has_all_args = "AllArgsConstructor" in annos or "@AllArgsConstructor" in source

    for field in ctx.symbol.fields or []:
        if "static" in field.modifiers:
            continue

        field_annos = {a.split(".")[-1] for a in (field.annotations or [])}
        suffix = _java_bean_suffix(field.name)
        field_type = field.type or "Object"

        field_has_getter = has_getter or "Getter" in field_annos
        field_has_setter = has_setter or "Setter" in field_annos

        if field_has_getter:
            getter = _getter_name(field.name, field_type)
            if getter not in existing_names:
                out.append(f"public {field_type} {getter}() [Lombok generated from Java field '{field.name}']")
                existing_names.add(getter)

        if field_has_setter:
            setter = _setter_name(field.name, field_type)
            if setter not in existing_names:
                out.append(f"public void {setter}({field_type} {field.name}) [Lombok generated from Java field '{field.name}']")
                existing_names.add(setter)

    if has_builder and "builder" not in existing_names:
        out.append(f"public static {ctx.symbol.name}.{ctx.symbol.name}Builder builder() [Lombok generated]")
        existing_names.add("builder")

    if has_no_args:
        out.append(f"public {ctx.symbol.name}() [Lombok generated]")

    if has_all_args:
        params = []
        for f in ctx.symbol.fields or []:
            if "static" not in f.modifiers:
                params.append(f"{f.type} {f.name}")
        if params:
            out.append(f"public {ctx.symbol.name}({', '.join(params)}) [Lombok generated]")

    out = _dedupe(out)

    if not out:
        return "- No public methods detected. Do not invent method calls."

    return "\n".join(f"- {m}" for m in out)


def _fields_block(ctx: GenerationContext) -> str:
    if not ctx.symbol or not ctx.symbol.fields:
        return "(no fields detected)"

    lines: list[str] = []

    for f in ctx.symbol.fields:
        mods = " ".join(sorted(f.modifiers)) if f.modifiers else ""
        annos = " ".join("@" + a for a in f.annotations) if f.annotations else ""
        prefix = " ".join(x for x in (annos, mods) if x)

        if prefix:
            lines.append(f"- {prefix} {f.type} {f.name}")
        else:
            lines.append(f"- {f.type} {f.name}")

    return "\n".join(lines)


def _source_imports_block(ctx: GenerationContext) -> str:
    imports = list(getattr(ctx, "source_imports", None) or [])
    return "\n".join(f"- {imp}" for imp in imports) if imports else "(none detected)"


def _collaborators_block(ctx: GenerationContext) -> str:
    if not ctx.collaborators:
        return "(none)"

    return "\n\n".join(render_collaborator(c) for c in ctx.collaborators).strip() or "(none)"


def _branch_hints_block(branch_hints: list[str]) -> str:
    if not branch_hints:
        return "(none)"

    return "\n".join(f"- {h}" for h in branch_hints if h and h.strip()).strip() or "(none)"


def _strategy_block(ctx: GenerationContext) -> str:
    kind = _template_kind(ctx)

    if _is_entity_like(ctx):
        return (
            "ENTITY AUGMENTATION STRATEGY:\n"
            "- Add only missing simple entity tests.\n"
            "- Do not use Mockito.\n"
            "- Do not use Spring context.\n"
            "- Do not create exception-path tests for getters/setters.\n"
            "- Do not test database/JPA provider behavior.\n"
            "- Do not derive methods from @Column/@JoinColumn names.\n"
            "- Only use Java field names and allowed methods.\n"
            "- For null-field coverage, explicitly call the verified JavaBean setter or fluent mutator with null for each non-primitive field before assertNull(getter()). Never assert default-null values on a new instance.\n"
            "- Do not add direct canEqual tests, two-instance equality/hashCode comparisons, or strict generated toString content assertions.\n"
        )

    if _is_dto_like(ctx):
        return (
            "DTO / MODEL AUGMENTATION STRATEGY:\n"
            "- Add only missing constructor/accessor/builder tests.\n"
            "- Do not use Mockito.\n"
            "- Do not use Spring context.\n"
            "- Do not create artificial exception tests.\n"
            "- Only use Java field names and allowed methods.\n"
            "- For null-field coverage, explicitly call the verified JavaBean setter or fluent mutator with null for each non-primitive field before assertNull(getter()). Never assert default-null values on a new instance.\n"
            "- Do not add direct canEqual tests, two-instance equality/hashCode comparisons, or strict generated toString content assertions.\n"
        )

    if _is_controller_like(ctx):
        return (
            "CONTROLLER AUGMENTATION STRATEGY:\n"
            "- Add only tests for uncovered controller branches shown in the excerpt.\n"
            "- Mock service collaborators only if listed in DIRECT COLLABORATORS.\n"
            "- Do not invent repository/database/network behavior.\n"
            "- Do not duplicate existing MockMvc tests.\n"
            "- Do not generate custom nested servlet mock classes such as MockHttpServletResponse or MockHttpServletRequest.\n"
            "- For servlet request/response arguments, use Mockito mocks or org.springframework.mock.web.MockHttpServletResponse/MockHttpServletRequest.\n"
            "- Use jakarta.servlet.* only; never use javax.servlet.*.\n"
        )

    if _is_repository_like(ctx):
        return (
            "REPOSITORY AUGMENTATION STRATEGY:\n"
            "- Do not invent Spring Data methods.\n"
            "- Only call methods listed in ALLOWED METHODS.\n"
            "- Do not add integration/database setup unless already present in the existing test class.\n"
        )

    if _is_config_like(ctx):
        return (
            "CONFIGURATION AUGMENTATION STRATEGY:\n"
            "- Avoid adding full Spring context tests.\n"
            "- Add only compile-safe tests for explicit bean factory methods if uncovered and callable.\n"
        )

    if kind in {"pure_unit", "unit", "reactive_unit"}:
        return (
            "SERVICE / UNIT AUGMENTATION STRATEGY:\n"
            "- Add focused tests for uncovered branches only.\n"
            "- Mock only DIRECT COLLABORATORS.\n"
            "- Do not mock DTOs, entities, strings, dates, collections, primitives, Optional, BigDecimal, or ResponseEntity.\n"
            "- Use collaborator exception tests only when the uncovered source has catch/exception logic or the method propagates exceptions.\n"
        )

    return (
        "GENERAL AUGMENTATION STRATEGY:\n"
        "- Add only compile-safe tests for uncovered source regions.\n"
        "- Do not invent methods or framework behavior.\n"
        "- Prefer simple JUnit 5 tests.\n"
    )


def _global_rules_block(ctx: GenerationContext) -> str:
    return (
        "GLOBAL AUGMENTATION RULES:\n"
        "- Return only NEW raw Java @Test methods and private helper methods if absolutely required.\n"
        "- Do not return package declarations.\n"
        "- Do not return imports.\n"
        "- Do not return class wrapper.\n"
        "- Do not return markdown fences or explanations.\n"
        "- Do not duplicate any existing test method name or existing test body.\n"
        "- Use JUnit 5 only.\n"
        "- Use simple Java 17/21-compatible syntax.\n"
        "- Do not use var, records, text blocks, switch expressions, or preview features.\n"
        "- Only call class-under-test methods listed in ALLOWED METHODS.\n"
        "- Only stub/mock collaborator methods listed in DIRECT COLLABORATORS.\n"
        "- Do not invent methods, constructors, fields, enum values, constants, or nested classes.\n"
        "- Never derive getter/setter names from @Column, @JoinColumn, @Table, database names, SQL names, or annotation values.\n"
        "- Getter/setter names must come only from Java field names listed for this exact class.\n"
        "- Do not mention or test any field name that is not listed in JAVA FIELDS DETECTED for this exact class.\n"
        "- Use ReflectionTestUtils only for @Value fields or unavoidable private field setup already required by existing tests.\n"
        "- For non-static @Value fields, ReflectionTestUtils.setField must target the class-under-test object instance, not ClassName.class.\n"
        "- @Value fields are configuration inputs, not JavaBean properties; do not call generated getX()/setX() for them unless the exact method is listed in ALLOWED METHODS.\n"
        "- For @RestControllerAdvice/@ControllerAdvice classes, do not invent dependency setters such as setEnv()/setEnvironment(); inject private Environment fields with ReflectionTestUtils.setField(instance, \"env\", environment) unless a real setter/constructor is listed. Environment.getProperty(...) is valid on Environment mocks.\n"
        "- Assert behavior through real public methods that read @Value fields; do not assert the private field itself.\n"
        "- Do not add @SpringBootTest, @DataJpaTest, @WebMvcTest, @WebFluxTest, @ContextConfiguration, @SpringJUnitConfig, @EnableFeignClients, @Autowired, @MockBean, or @MockitoBean.\n"
        "- Do not load application.yaml or start Spring context.\n"
        "- Assume imports, mocks, fields, and setup already exist in the current test class.\n"
        "- Null-field tests must set each tested non-primitive field to null via its verified JavaBean setter or fluent mutator before assertNull(getter()). Skip primitive fields.\n"
        "- Call canEqual only when it is listed/available and package-accessible from the generated test. Do not compare two different entity/DTO instances with assertEquals/assertNotEquals unless the source explicitly defines that equality contract. Do not assert generated toString contents by default.\n"
    )


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


def build_augmentation_messages(
    ctx: GenerationContext,
    existing_test: str,
    uncovered_excerpt: str,
    branch_hints: list[str],
) -> MessagePayload:
    """Build structured messages for test augmentation.

    The LLM should add only missing focused test methods, not regenerate the
    entire test file.
    """

    system_prompt = _safe_system_prompt(ctx)

    test_class = ctx.test_class_name or "GeneratedTest"
    test_package = ctx.test_package or "com.fallback.test"
    source_code = ctx.cut_source or ""
    existing_test_code = existing_test or ""
    uncovered_code = uncovered_excerpt or "(no uncovered excerpt provided)"

    user_prompt = (
        "Add coverage to an existing JUnit 5 test class.\n\n"

        f"Target test class : {test_class}\n"
        f"Target package    : {test_package}\n"
        f"Class under test  : {ctx.symbol.fqcn if ctx.symbol else 'Unknown'}\n\n"

        "==================================================\n"
        "GLOBAL RULES\n"
        "==================================================\n"
        f"{_global_rules_block(ctx)}\n\n"

        "==================================================\n"
        "CLASS-SPECIFIC STRATEGY\n"
        "==================================================\n"
        f"{_strategy_block(ctx)}\n\n"

        "==================================================\n"
        "CURRENT EXISTING TEST CLASS\n"
        "==================================================\n"
        "Read this carefully. Do not duplicate existing tests. Reuse existing fields, mocks, setup, and helper methods.\n"
        f"```java\n{existing_test_code}\n```\n\n"

        "==================================================\n"
        "CLASS UNDER TEST SOURCE\n"
        "==================================================\n"
        f"```java\n{source_code}\n```\n\n"

        "==================================================\n"
        "JAVA FIELDS DETECTED\n"
        "==================================================\n"
        "Use these Java field names for accessor logic. Ignore database/annotation names.\n"
        f"{_fields_block(ctx)}\n\n"

        "==================================================\n"
        "SOURCE IMPORTS FROM CLASS UNDER TEST\n"
        "==================================================\n"
        "Use only when a new method references the same production type/static member. Do not copy unrelated imports.\n"
        f"{_source_imports_block(ctx)}\n\n"

        "==================================================\n"
        "ALLOWED METHODS TO CALL ON CLASS UNDER TEST\n"
        "==================================================\n"
        f"{_allowed_methods_block(ctx)}\n\n"

        "==================================================\n"
        "DIRECT COLLABORATORS\n"
        "==================================================\n"
        "Only these collaborators may be mocked or stubbed.\n"
        f"{_collaborators_block(ctx)}\n\n"

        "==================================================\n"
        "UNCOVERED SOURCE REGIONS\n"
        "==================================================\n"
        f"{uncovered_code}\n\n"

        "==================================================\n"
        "BRANCH HINTS\n"
        "==================================================\n"
        f"{_branch_hints_block(branch_hints)}\n\n"

        "==================================================\n"
        "AUGMENTATION OBJECTIVE\n"
        "==================================================\n"
        "- Generate the smallest set of NEW tests needed for the uncovered excerpt.\n"
        "- Prioritize real uncovered branches over artificial parameter combinations.\n"
        "- For each uncovered if/else branch, add one focused test only if the branch is visible in the source excerpt.\n"
        "- For each uncovered catch block, add an exception-path test only if the catch block is visible in the source excerpt.\n"
        "- For dependencies, test null/empty/exception behavior only when the source has explicit logic for it.\n"
        "- For String/Collection/numeric values, test boundary cases only when they control a visible branch.\n"
        "- Do not generate duplicate happy-path tests.\n"
        "- Do not regenerate the whole class.\n\n"

        "Return ONLY raw Java @Test methods."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]



def build_method_augmentation_messages(
    ctx: GenerationContext,
    method,
    current_method_block: str,
    uncovered_excerpt: str,
    branch_hints: list[str],
) -> MessagePayload:
    """Add missing coverage for one production method without seeing other tests."""
    method_id = f"{method.name}({','.join(type_name for type_name, _ in method.params)})"
    execution = (
        render_execution_context(ctx.execution_context, method_id=method_id)
        if ctx.execution_context is not None else "(execution context unavailable)"
    )
    collaborators = (
        render_dependency_contracts(ctx.execution_context, method_id=method_id)
        if ctx.execution_context is not None else "(none authorized)"
    )
    hints = "\n".join(f"- {hint}" for hint in branch_hints) or "- none"
    user = f"""Add only the missing JUnit 5 @Test methods for ONE production method.

=== ONLY AUTHORIZED PRODUCTION METHOD ===
{method.render()}

{execution}

=== METHOD-SPECIFIC COLLABORATORS ===
{collaborators}

=== EXISTING @TEST METHODS FOR THIS PRODUCTION METHOD ===
```java
{current_method_block}
```

=== UNCOVERED SOURCE / JACOCO WINDOW ===
{uncovered_excerpt}

=== COVERAGE HINTS ===
{hints}

RULES:
- Output only NEW raw Java @Test methods. No package, imports, class wrapper, fields, setup, helpers, or prose.
- Add the smallest tests required for the uncovered source-proven branch/path.
- Call only the authorized production method.
- Use only verified recursive payload schemas and exact collaborator invocations from the authoritative context.
- Build every intermediate non-null dereference prefix required by the uncovered branch; allow only the terminal value to be null when the source branch/consumer supports it.
- Direct @Value fields are injected by the deterministic skeleton; override exact listed fields only for configuration-dependent branches.
- Stub only calls executed by that branch; keep stubs local and do not use lenient().
- Do not duplicate existing test names or existing covered scenarios.
- Do not invent APIs, fields, constructors, enum constants, branches, HTTP statuses, or JSON paths.
"""
    return [
        {"role": "system", "content": _safe_system_prompt(ctx)},
        {"role": "user", "content": user},
    ]

# Spring Data inherited repository method rule
SPRING_DATA_REPOSITORY_METHODS = "findById, save, saveAll, findAll, existsById, count, deleteById, delete, deleteAll, flush, saveAndFlush, getReferenceById are allowed on repository collaborators; derived query methods must be declared."
