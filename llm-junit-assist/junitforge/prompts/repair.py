"""Compile/runtime repair prompts aligned with unit-only generation rules."""

from __future__ import annotations

import re

from junitforge.execution.renderer import render_dependency_contracts, render_execution_context
from junitforge.models import CompileError, GenerationContext
from junitforge.parser.collaborators import render_collaborator
from junitforge.prompts.system import build_system_prompt


def _system(ctx: GenerationContext) -> str:
    return build_system_prompt(ctx.stack, ctx.template).replace("{TEST_CLASS_NAME}", ctx.test_class_name).replace("{TEST_PACKAGE}", ctx.test_package)


def _collabs(ctx: GenerationContext) -> str:
    if ctx.execution_context is not None:
        return render_dependency_contracts(ctx.execution_context)
    return "\n\n".join(render_collaborator(c) for c in ctx.collaborators) or "(none)"


def _execution_context(ctx: GenerationContext) -> str:
    if ctx.execution_context is None:
        return ""
    return render_execution_context(ctx.execution_context)


def _is_strict_serviceimpl(ctx: GenerationContext) -> bool:
    execution = getattr(ctx, "execution_context", None)
    target_kind = getattr(getattr(execution, "target_kind", None), "value", None)
    if target_kind is not None:
        return target_kind == "service-impl"
    if not ctx.symbol or ctx.symbol.kind != "class" or "abstract" in (ctx.symbol.modifiers or set()):
        return False
    annos = {a.split(".")[-1] for a in (ctx.symbol.annotations or [])}
    impls = {name.split(".")[-1] for name in (ctx.symbol.implements or [])}
    return (
        "Service" in annos
        or ctx.symbol.name.endswith("ServiceImpl")
        or any(name.endswith("Service") for name in impls)
    )


def _is_lombok_generated(method) -> bool:
    return "LombokGenerated" in {a.split(".")[-1] for a in (method.annotations or [])}


def _strict_service_methods(ctx: GenerationContext) -> list:
    if not ctx.symbol:
        return []
    return [
        method for method in (ctx.symbol.methods or [])
        if method.is_public and not method.is_static and not method.is_constructor and not _is_lombok_generated(method)
    ]


def _service_repair_rules(ctx: GenerationContext) -> str:
    if not _is_strict_serviceimpl(ctx):
        return ""
    names = ", ".join(method.name for method in _strict_service_methods(ctx)) or "(none)"
    return (
        "SERVICEIMPL HALLUCINATION REPAIR RULES:\n"
        f"- Exact allowed CUT method names: {names}.\n"
        "- Delete every test method that calls a CUT method outside that list. Do not rename an invented method to another guessed name.\n"
        "- Rebuild the affected test only from an exact listed CUT signature and its visible source path.\n"
        "- Never use the CUT as a Mockito when/doReturn/doThrow/doNothing/verify receiver; only dependencies may be Mockito receivers.\n"
        "- For collaborators, use only exact observed/declared contracts in DIRECT COLLABORATORS or AUTHORITATIVE EXECUTION CONTEXT.\n"
        "- If no exact CUT method or collaborator contract supports the broken test, remove that test instead of inventing an API."
    )


def _allowed_methods(ctx: GenerationContext) -> str:
    try:
        if _is_strict_serviceimpl(ctx):
            methods = [m.render() for m in _strict_service_methods(ctx)]
        else:
            methods = [m.render() for m in ctx.symbol.public_methods()]
    except Exception:
        methods = []
    header = "- EXACT SERVICEIMPL WHITELIST; no other CUT method is permitted\n" if _is_strict_serviceimpl(ctx) else ""
    return header + ("\n".join(f"- {m}" for m in methods) if methods else "- (none detected; do not invent)")


def _private_methods(ctx: GenerationContext) -> str:
    try:
        privates = [m.render() for m in (ctx.symbol.methods or []) if "private" in m.modifiers]
    except Exception:
        privates = []
    return "\n".join(f"- {m}" for m in privates) if privates else "(none detected)"


def _source_imports(ctx: GenerationContext) -> str:
    imports = list(getattr(ctx, "source_imports", None) or [])
    return "\n".join(f"- {i}" for i in imports) if imports else "(none detected)"


def _equals_hashcode_repair_rule(ctx: GenerationContext) -> str:
    source = ctx.cut_source or ""
    call_super_true = re.search(
        r"@(?:lombok\.)?EqualsAndHashCode\s*\([^)]*\bcallSuper\s*=\s*true\b[^)]*\)",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    ) is not None
    if not call_super_true:
        return (
            "- Keep two-instance equals/hashCode tests only when manual equals/hashCode or "
            "Lombok @Data/@Value/@EqualsAndHashCode proves the contract. For manual equals, "
            "test only fields used by the method body."
        )
    return (
        "- SPECIAL @EqualsAndHashCode(callSuper = true) REPAIR: keep exactly two objects of the "
        "same concrete class; set identical values for every writable listed field; compare each "
        "initialized field through verified getters; then use assertEquals(first, second). "
        "HashCode assertions must be self-comparisons only: "
        "assertEquals(first.hashCode(), first.hashCode()) and "
        "assertEquals(second.hashCode(), second.hashCode()). Remove any comparison between "
        "first.hashCode() and second.hashCode(). Do not create or compare a parent object."
    )


def build_compile_repair_messages(
    ctx: GenerationContext,
    current_test: str,
    errors: list[CompileError],
    kb_hints: list[str] | None = None,
) -> list[dict]:
    err_block = "\n".join(f"- {e.render()}" for e in errors) or "- (compiler errors unavailable)"
    kb_block = "\n".join(f"- {h}" for h in (kb_hints or [])) or "(none)"
    execution_block = _execution_context(ctx)
    execution_section = f"{execution_block}\n\n" if execution_block else ""
    equality_repair_rule = _equals_hashcode_repair_rule(ctx)
    service_repair_rule = _service_repair_rules(ctx)

    user = f"""The generated JUnit test file failed compilation. Return the COMPLETE corrected Java test file, not a snippet.

=== COMPILE ERRORS ===
{err_block}

=== KNOWN FIX HINTS ===
{kb_block}

=== ALLOWED CLASS-UNDER-TEST METHODS ===
{_allowed_methods(ctx)}

=== PRIVATE METHODS DETECTED — DO NOT CALL DIRECTLY ===
{_private_methods(ctx)}

=== DIRECT COLLABORATORS ===
{_collabs(ctx)}

{execution_section}=== SOURCE IMPORTS FROM CLASS UNDER TEST ===
{_source_imports(ctx)}

=== CURRENT BROKEN TEST FILE ===
```java
{current_test}
```

REPAIR RULES:
- Return exactly one complete Java file with package, imports, class, and tests.
- Keep the required package {ctx.test_package} and class name {ctx.test_class_name}.
- Fix the compile errors directly; do not rewrite working logic unnecessarily.
- Do not use Spring context annotations, @Autowired, @MockBean, or @MockitoBean.
- For controller tests, do not generate custom nested servlet mock classes. Never implement/extend HttpServletResponse, HttpServletRequest, ServletResponse, ServletRequest, FilterChain, ServletOutputStream, or PrintWriter. Use Mockito mock(...) or org.springframework.mock.web.MockHttpServletResponse/MockHttpServletRequest instead.
- Use jakarta.servlet.* only; never use javax.servlet.*.
- Do not invent methods or constructors.
{service_repair_rule}
- Add missing imports when needed, especially JUnit assertions, Mockito static imports, and production enum/constant imports shown in SOURCE IMPORTS FROM CLASS UNDER TEST.
- Mockito matcher rule: never mix raw values with any()/anyString()/eq()/isNull()/notNull()/argThat() in the same mocked method invocation. If one argument is a matcher, wrap exact raw values with eq(value). Prefer typed matchers such as anyString(), anyInt(), anyLong(), anyBoolean(), or any(Type.class).
- For custom exceptions extending Throwable/Exception/RuntimeException, inherited getMessage(), getCause(), getLocalizedMessage(), getSuppressed(), and getStackTrace() are valid methods even if not listed directly on the subclass.
- For @RestControllerAdvice/@ControllerAdvice tests, do not invent dependency setters such as setEnv()/setEnvironment(). If the handler has a private Environment field and no real setter, inject the mock with ReflectionTestUtils.setField(handler, "env", environment). Stubbing Environment.getProperty(...) is valid.
- For entity/DTO null-field tests, set each tested non-primitive field to null through its setter before assertNull(getter()). Do not assert default-null values on a new object.
- Keep canEqual tests only when canEqual is listed/available and package-accessible; otherwise remove them.
{equality_repair_rule}
- Keep constructor tests only for constructors explicitly listed/available, including Lombok @AllArgsConstructor and manually declared argument constructors.
- Keep builder tests only when builder() is available/listed.
- Keep toString content assertions only when source has explicit toString or Lombok @ToString/@Data/@Value and the asserted field is stable/non-excluded; otherwise use assertNotNull(instance.toString()).
- If a call uses a private method, remove that test method or cover the behavior through a public method. Do not use ReflectionTestUtils.invokeMethod for private helpers.
- If an accessor is derived from DB/annotation names, replace it with Java-field-derived accessor names.
- Output raw Java only. No markdown."""

    return [{"role": "system", "content": _system(ctx)}, {"role": "user", "content": user}]


def build_test_failure_repair_messages(ctx: GenerationContext, current_test: str, failure_excerpt: str) -> list[dict]:
    execution_block = _execution_context(ctx)
    execution_section = f"{execution_block}\n\n" if execution_block else ""
    equality_repair_rule = _equals_hashcode_repair_rule(ctx)
    service_repair_rule = _service_repair_rules(ctx)
    user = f"""A generated JUnit test failed at runtime. Return the COMPLETE corrected Java test file.

=== RUNTIME FAILURE / STACK TRACE ===
{failure_excerpt}

=== CLASS UNDER TEST SOURCE ===
```java
{ctx.cut_source}
```

=== DIRECT COLLABORATORS ===
{_collabs(ctx)}

{execution_section}=== SOURCE IMPORTS FROM CLASS UNDER TEST ===
{_source_imports(ctx)}

=== CURRENT TEST FILE ===
```java
{current_test}
```

REPAIR RULES:
- Return exactly one complete Java file with package, imports, class, and tests.
- Keep package {ctx.test_package} and class name {ctx.test_class_name}.
- Align assertions and Mockito stubs with actual source behavior.
{service_repair_rule}
- Fix Mockito InvalidUseOfMatchersException by making each mocked method call use either all exact raw values or all matchers. If any argument uses any()/anyString()/eq()/isNull()/notNull()/argThat(), wrap raw exact arguments with eq(value).
- For custom exceptions extending Throwable/Exception/RuntimeException, inherited getMessage(), getCause(), getLocalizedMessage(), getSuppressed(), and getStackTrace() are valid methods even if not listed directly on the subclass.
- For @RestControllerAdvice/@ControllerAdvice tests, do not invent dependency setters such as setEnv()/setEnvironment(). If the handler has a private Environment field and no real setter, inject the mock with ReflectionTestUtils.setField(handler, "env", environment). Stubbing Environment.getProperty(...) is valid.
- For entity/DTO null-field tests, set each tested non-primitive field to null through its setter before assertNull(getter()).
- Keep canEqual tests only when canEqual is listed/available and package-accessible; otherwise remove them.
{equality_repair_rule}
- Keep constructor tests only for constructors explicitly listed/available.
- Keep builder tests only when builder() is available/listed.
- Avoid strict toString content assertions unless explicit toString or Lombok @ToString/@Data/@Value proves a stable contract.
- Do not weaken to trivial assertions such as assertTrue(true).
- Remove unused stubs that cause UnnecessaryStubbingException.
- Do not use Spring context annotations, @Autowired, @MockBean, or @MockitoBean.
- For controller tests, do not generate custom nested servlet mock classes. Never implement/extend HttpServletResponse, HttpServletRequest, ServletResponse, ServletRequest, FilterChain, ServletOutputStream, or PrintWriter. Use Mockito mock(...) or org.springframework.mock.web.MockHttpServletResponse/MockHttpServletRequest instead.
- Use jakarta.servlet.* only; never use javax.servlet.*.
- Output raw Java only. No markdown."""

    return [{"role": "system", "content": _system(ctx)}, {"role": "user", "content": user}]


# Spring Data inherited repository method rule
SPRING_DATA_REPOSITORY_METHODS = "findById, save, findAll, count, and deleteById are allowed on verified Spring Data repository collaborators; every other derived or inherited method must be declared or authoritatively resolved."


def build_method_compile_repair_messages(
    ctx: GenerationContext,
    method,
    current_method_block: str,
    errors: list[CompileError],
) -> list[dict]:
    """Repair only the generated tests that belong to one production method."""
    method_id = f"{method.name}({','.join(type_name for type_name, _ in method.params)})"
    execution_block = (
        render_execution_context(ctx.execution_context, method_id=method_id)
        if ctx.execution_context is not None else "(execution context unavailable)"
    )
    collaborator_block = (
        render_dependency_contracts(ctx.execution_context, method_id=method_id)
        if ctx.execution_context is not None else "(none authorized)"
    )
    err_block = "\n".join(f"- {error.render()}" for error in errors) or "- (errors unavailable)"
    user = f"""Repair ONLY the JUnit @Test methods generated for one production method.

=== ONLY AUTHORIZED PRODUCTION METHOD ===
{method.render()}

=== COMPILE ERRORS BELONGING TO THIS METHOD BLOCK ===
{err_block}

{execution_block}

=== METHOD-SPECIFIC COLLABORATOR CONTRACTS ===
{collaborator_block}

=== CURRENT BROKEN @TEST METHODS ===
```java
{current_method_block}
```

REPAIR RULES:
- Output raw Java @Test methods only. No package, imports, class wrapper, fields, setup method, helpers, or prose.
- Preserve coverage of all source-proven branches for this production method unless a branch is unsupported because its schema/contract is unresolved.
- Fix only the listed compile errors. Do not call another class-under-test method.
- Use only the verified recursive payload schemas and exact collaborator receiver+method contracts in the authoritative context.
- Rebuild every listed intermediate dereference prefix before calling the CUT; a null-safe terminal utility does not make its parent object null-safe.
- Direct @Value fields are injected by the deterministic skeleton. Override only exact listed fields when the selected branch requires a different configuration value.
- Do not invent fields, getters, setters, constructors, builders, enum constants, exceptions, branches, status codes, or JSON paths.
- Never stub or verify the class under test.
- Keep branch-specific Mockito stubs local to the test that executes them; no lenient().
- Do not call private methods directly.
"""
    return [{"role": "system", "content": _system(ctx)}, {"role": "user", "content": user}]


def build_method_runtime_repair_messages(
    ctx: GenerationContext,
    method,
    current_method_block: str,
    failure_excerpt: str,
) -> list[dict]:
    """Repair runtime failures without exposing or rewriting other method tests."""
    method_id = f"{method.name}({','.join(type_name for type_name, _ in method.params)})"
    execution_block = (
        render_execution_context(ctx.execution_context, method_id=method_id)
        if ctx.execution_context is not None else "(execution context unavailable)"
    )
    collaborator_block = (
        render_dependency_contracts(ctx.execution_context, method_id=method_id)
        if ctx.execution_context is not None else "(none authorized)"
    )
    user = f"""Repair ONLY the JUnit @Test methods for one production method after a runtime failure.

=== ONLY AUTHORIZED PRODUCTION METHOD ===
{method.render()}

=== FAILURE / STACK TRACE ===
{failure_excerpt}

{execution_block}

=== METHOD-SPECIFIC COLLABORATORS ===
{collaborator_block}

=== CURRENT @TEST METHODS FOR THIS PRODUCTION METHOD ===
```java
{current_method_block}
```

REPAIR RULES:
- Output the complete replacement set of raw Java @Test methods for this production method only.
- No package, imports, class wrapper, fields, setup, helper methods, or prose.
- Preserve source-proven branch coverage while fixing the runtime failure.
- For NullPointerException, build every verified intermediate dereference prefix required by the selected branch; do not guess missing fields. For StringUtils.defaultString(unit.getValue())-style code, unit must exist even when value is intentionally null.
- Direct @Value fields are injected by the deterministic skeleton. Override only exact listed fields when the failing branch requires a different value.
- Remove branch-irrelevant stubs causing UnnecessaryStubbingException.
- Align Mockito arguments with exact observed invocation expressions and never mix raw values with matchers.
- Use only verified payload schemas and exact collaborator contracts.
- Do not call another CUT method, stub/verify the CUT, invoke private methods, use lenient(), or invent APIs/behavior.
"""
    return [{"role": "system", "content": _system(ctx)}, {"role": "user", "content": user}]
