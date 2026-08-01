"""Compile/runtime repair prompts aligned with unit-only generation rules."""

from __future__ import annotations

import re

from junitforge.execution.assembly import generated_scopes
from junitforge.execution.renderer import render_dependency_contracts, render_execution_context
from junitforge.models import (
    CompileError,
    GeneratedScope,
    GeneratedScopeKind,
    GenerationContext,
    TestMethodResult,
    TestMethodStatus,
)
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
        "- Preserve every generated primary test method. Never delete, comment out, disable, or rename a test to force publication.\n"
        "- Repair only the affected scope from an exact listed CUT signature and its visible source path.\n"
        "- Never use the CUT as a Mockito when/doReturn/doThrow/doNothing/verify receiver; only dependencies may be Mockito receivers.\n"
        "- For collaborators, use only exact observed/declared contracts in DIRECT COLLABORATORS or AUTHORITATIVE EXECUTION CONTEXT.\n"
        "- If supplied verified evidence is insufficient, leave the scope unresolved instead of inventing an API or removing a test."
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


def _fixture_names(ctx: GenerationContext) -> set[str]:
    execution = ctx.execution_context
    if execution is None:
        return set()
    return {fixture.method_name for fixture in execution.fixtures if fixture.method_name}


def _identifiers(text: str) -> set[str]:
    return set(re.findall(r"\b[A-Za-z_$][\w$]*\b", text or ""))


def _scope_map(ctx: GenerationContext, current_test: str) -> tuple:
    return generated_scopes(current_test, fixture_names=_fixture_names(ctx))


def _test_scope_by_name(scopes: tuple, name: str) -> GeneratedScope | None:
    base = name.split("(", 1)[0]
    return next(
        (
            scope for scope in scopes
            if scope.kind is GeneratedScopeKind.TEST_METHOD and scope.name == base
        ),
        None,
    )


def _called_helper_scopes(seed_text: str, scopes: tuple) -> list[GeneratedScope]:
    helper_kinds = {
        GeneratedScopeKind.FIXTURE_HELPER,
        GeneratedScopeKind.STUB_HELPER,
        GeneratedScopeKind.OTHER_CLASS_DECLARATION,
    }
    by_name = {
        scope.name: scope for scope in scopes
        if scope.kind in helper_kinds and "(" in scope.source
    }
    selected: list[GeneratedScope] = []
    seen: set[str] = set()
    pending = list(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", seed_text or ""))
    while pending:
        name = pending.pop(0)
        helper = by_name.get(name)
        if helper is None or helper.scope_id in seen:
            continue
        seen.add(helper.scope_id)
        selected.append(helper)
        pending.extend(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", helper.source))
    return selected


def _relevant_setup_source(
    ctx: GenerationContext,
    scope: GeneratedScope,
    setup: GeneratedScope,
    owner_ids: set[str],
) -> str | None:
    execution = ctx.execution_context
    if execution is None:
        return None
    required_fields = {
        requirement.field_name
        for method in execution.methods
        if method.method_id in owner_ids
        for requirement in method.configuration_requirements
    }
    if not required_fields:
        return None
    lines = setup.source.splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if (
            stripped.startswith("@")
            or re.search(rf"\b{re.escape(setup.name)}\s*\(", line)
            or stripped in {"{", "}"}
            or any(re.search(rf"\b{re.escape(field)}\b|\"{re.escape(field)}\"", line) for field in required_fields)
        ):
            kept.append(line)
    rendered = "\n".join(kept).strip()
    return rendered or None


def _direct_generated_context(
    ctx: GenerationContext,
    current_test: str,
    affected_scope: GeneratedScope,
    *,
    affected_test_names: tuple[str, ...] = (),
) -> tuple[str, set[str], str]:
    """Return minimal generated context, owning production ids, and relevance text."""
    scopes = _scope_map(ctx, current_test)
    failed_tests = [
        test_scope for name in affected_test_names
        if (test_scope := _test_scope_by_name(scopes, name)) is not None
    ]
    owner_ids = {
        owner for owner in (
            affected_scope.owner_method_id,
            *(scope.owner_method_id for scope in failed_tests),
        )
        if owner
    }
    seed_scopes = [affected_scope]
    if affected_scope.kind is not GeneratedScopeKind.TEST_METHOD:
        seed_scopes.extend(failed_tests)
    seed_text = "\n".join(scope.source for scope in seed_scopes)
    helpers = [
        helper for helper in _called_helper_scopes(seed_text, scopes)
        if helper.scope_id != affected_scope.scope_id
    ]
    relevant_text = "\n".join([seed_text, *(helper.source for helper in helpers)])
    identifiers = _identifiers(relevant_text)

    setup_sections: list[str] = []
    for setup in scopes:
        if setup.kind is not GeneratedScopeKind.SETUP_METHOD:
            continue
        rendered = _relevant_setup_source(ctx, affected_scope, setup, owner_ids)
        if rendered:
            setup_sections.append(rendered)
            identifiers.update(_identifiers(rendered))
            relevant_text += "\n" + rendered

    field_sections = [
        scope.source for scope in scopes
        if scope.kind in {GeneratedScopeKind.CLASS_FIELD, GeneratedScopeKind.MOCK_DECLARATION}
        and scope.scope_id != affected_scope.scope_id
        and scope.name in identifiers
    ]
    relevant_text += "\n" + "\n".join(field_sections)
    identifiers.update(_identifiers(relevant_text))

    import_scope = next(
        (scope for scope in scopes if scope.kind is GeneratedScopeKind.IMPORT_BLOCK),
        None,
    )
    relevant_imports: list[str] = []
    if import_scope is not None and affected_scope.kind is not GeneratedScopeKind.IMPORT_BLOCK:
        for line in import_scope.source.splitlines():
            target = line.replace("import", "", 1).replace("static", "", 1).strip().rstrip(";")
            simple = target.rsplit(".", 1)[-1]
            if simple == "*" or simple in identifiers:
                relevant_imports.append(line.strip())

    sections: list[str] = []
    contextual_failed_tests = [
        scope for scope in failed_tests
        if scope.scope_id != affected_scope.scope_id
    ]
    if contextual_failed_tests:
        sections.append(
            "DIRECTLY AFFECTED FAILING TEST METHOD(S):\n"
            + "\n\n".join(
                f"```java\n{scope.source}\n```"
                for scope in contextual_failed_tests
            )
        )
    if setup_sections:
        sections.append(
            "DIRECTLY USED SETUP STATEMENTS:\n"
            + "\n\n".join(f"```java\n{section}\n```" for section in setup_sections)
        )
    fixture_helpers = [helper for helper in helpers if helper.kind is GeneratedScopeKind.FIXTURE_HELPER]
    stub_helpers = [helper for helper in helpers if helper.kind is GeneratedScopeKind.STUB_HELPER]
    if fixture_helpers:
        sections.append(
            "DIRECTLY USED FIXTURE HELPERS:\n"
            + "\n\n".join(f"```java\n{helper.source}\n```" for helper in fixture_helpers)
        )
    if stub_helpers:
        sections.append(
            "DIRECTLY USED STUB HELPERS:\n"
            + "\n\n".join(f"```java\n{helper.source}\n```" for helper in stub_helpers)
        )
    other_helpers = [
        helper for helper in helpers
        if helper.kind not in {GeneratedScopeKind.FIXTURE_HELPER, GeneratedScopeKind.STUB_HELPER}
    ]
    if other_helpers:
        sections.append(
            "DIRECTLY USED GENERATED HELPERS:\n"
            + "\n\n".join(f"```java\n{helper.source}\n```" for helper in other_helpers)
        )
    if field_sections:
        sections.append("DIRECTLY RELEVANT MOCKS/FIELDS:\n" + "\n".join(field_sections))
    if relevant_imports:
        sections.append("RELEVANT IMPORTS:\n" + "\n".join(relevant_imports))
    return "\n\n".join(sections) or "(no additional generated context required)", owner_ids, relevant_text


def _method_id(method) -> str:
    return f"{method.name}({','.join(type_name for type_name, _ in method.params)})"


def _source_for_method(ctx: GenerationContext, method_id: str) -> str | None:
    method = next((item for item in (ctx.symbol.methods or []) if _method_id(item) == method_id), None)
    if method is None or method.line is None:
        return None
    lines = (ctx.cut_source or "").splitlines()
    end = method.end_line or method.line
    if not (1 <= method.line <= len(lines)):
        return None
    return "\n".join(lines[method.line - 1:min(end, len(lines))]).strip()


def _verified_production_evidence(
    ctx: GenerationContext,
    *,
    owner_ids: set[str],
    relevance_text: str,
    diagnostic_text: str,
) -> str:
    execution = ctx.execution_context
    if execution is None:
        return "(no verified execution context available)"
    identifiers = _identifiers(relevance_text + "\n" + diagnostic_text)
    lines: list[str] = []
    emitted_contracts: set[tuple[str, str, tuple[str, ...], str | None]] = set()
    emitted_config: set[str] = set()
    if ctx.symbol.name in identifiers:
        lines.append(f"VERIFIED CUT TYPE: {ctx.symbol.fqcn}")
    for dependency in execution.dependencies:
        field_name = dependency.field_name or dependency.parameter_name or ""
        if (
            dependency.resolution_status.value in {"resolved", "inherited", "external"}
            and (
                field_name in identifiers
                or dependency.simple_type in identifiers
                or dependency.declared_type in identifiers
            )
        ):
            lines.append(
                f"VERIFIED DEPENDENCY TYPE: field={field_name}; "
                f"declared={dependency.declared_type}; canonical={dependency.fqcn or dependency.declared_type}; "
                f"resolution={dependency.resolution_status.value}"
            )
    methods = [method for method in execution.methods if method.method_id in owner_ids]
    for method in methods:
        lines.append(f"PRODUCTION ENTRY: {method.signature}")
        lines.append("```java\n" + method.method_source.strip() + "\n```")
        selection = method.selected_primary_path
        if selection is not None:
            choices = ", ".join(
                f"{choice.branch_id}={choice.arm}" for choice in selection.branch_choices
            ) or "no conditional branch required"
            reachable = ", ".join(selection.reachable_method_ids) or "none"
            lines.append(
                f"SELECTED SUCCESS PATH: feasibility={selection.feasibility.value}; "
                f"branches={choices}; reachable same-class methods={reachable}"
            )
            if selection.limitation:
                lines.append(f"PATH LIMITATION: {selection.limitation}")
            for helper_id in selection.reachable_method_ids:
                helper_source = _source_for_method(ctx, helper_id)
                if helper_source:
                    lines.append(f"REACHABLE PRODUCTION HELPER {helper_id}:\n```java\n{helper_source}\n```")
        for invocation in method.dependency_invocations:
            contract = invocation.contract
            if contract.resolution_status.value not in {"resolved", "inherited", "external"}:
                continue
            contract_key = (
                invocation.dependency_field,
                contract.method_name,
                tuple(contract.parameter_types),
                contract.return_type,
            )
            if contract_key in emitted_contracts:
                continue
            emitted_contracts.add(contract_key)
            params = ", ".join(contract.parameter_types)
            return_type = contract.return_type or "<unresolved>"
            args = ", ".join(argument.expression for argument in invocation.arguments)
            lines.append(
                "VERIFIED COLLABORATOR SIGNATURE: "
                f"{return_type} {invocation.dependency_field}.{contract.method_name}({params}); "
                f"observed invocation arguments=({args}); sourceLine={invocation.line}; "
                f"resolution={contract.resolution_status.value}"
            )
        required_config = {requirement.field_name for requirement in method.configuration_requirements}
        for field in execution.configuration_fields:
            if field.field_name in required_config:
                emitted_config.add(field.field_name)
                lines.append(
                    f"VERIFIED CONFIGURATION: {field.type_name} {field.field_name}; "
                    f"property={field.property_key!r}; testLiteral={field.test_value}"
                )

    # Shared stub/setup helpers have no owning method marker. Resolve only the
    # collaborator/configuration symbols named by that helper instead of
    # dumping every contract from the class-wide catalogue.
    for method in execution.methods:
        for invocation in method.dependency_invocations:
            contract = invocation.contract
            contract_key = (
                invocation.dependency_field,
                contract.method_name,
                tuple(contract.parameter_types),
                contract.return_type,
            )
            if (
                contract_key in emitted_contracts
                or contract.resolution_status.value not in {"resolved", "inherited", "external"}
                or invocation.dependency_field not in identifiers
                or contract.method_name not in identifiers
            ):
                continue
            emitted_contracts.add(contract_key)
            lines.append(
                "VERIFIED COLLABORATOR SIGNATURE: "
                f"{contract.return_type or '<unresolved>'} "
                f"{invocation.dependency_field}.{contract.method_name}"
                f"({', '.join(contract.parameter_types)}); "
                f"resolution={contract.resolution_status.value}"
            )
    for field in execution.configuration_fields:
        if field.field_name in identifiers and field.field_name not in emitted_config:
            emitted_config.add(field.field_name)
            lines.append(
                f"VERIFIED CONFIGURATION: {field.type_name} {field.field_name}; "
                f"property={field.property_key!r}; testLiteral={field.test_value}"
            )

    relevant_fixture_schema_ids = {
        fixture.schema_id
        for fixture in execution.fixtures
        if fixture.method_name in identifiers
    }
    candidate_schemas = [
        schema for schema in execution.payload_schemas
        if (
            schema.type_name in identifiers
            or (schema.fqcn and schema.fqcn.rsplit(".", 1)[-1] in identifiers)
            or schema.type_name in relevant_fixture_schema_ids
            or (schema.fqcn and schema.fqcn in relevant_fixture_schema_ids)
        )
    ]
    for schema in candidate_schemas:
        canonical = schema.fqcn or schema.type_name
        if schema.resolution_status.value not in {"resolved", "inherited", "external"}:
            continue
        lines.append(
            f"VERIFIED JAVA TYPE: {canonical}; construction={schema.construction_kind.value}; "
            f"constructors={list(schema.constructors)}"
        )
        if schema.enum_constants:
            lines.append(f"VERIFIED ENUM CONSTANTS {canonical}: {', '.join(schema.enum_constants)}")
        for prop in schema.properties:
            accessors = {prop.name, prop.getter or "", prop.setter or "", prop.builder_method or ""}
            if accessors.intersection(identifiers):
                lines.append(
                    f"VERIFIED PROPERTY {canonical}.{prop.name}: type={prop.resolved_type or prop.type_name}; "
                    f"getter={prop.getter}; setter={prop.setter}; enumConstants={list(prop.enum_constants)}"
                )

    if not lines:
        exact_methods = [
            method.render() for method in (ctx.symbol.methods or [])
            if method.name in identifiers
        ]
        lines.extend(f"VERIFIED CUT METHOD: {method}" for method in exact_methods)
    return "\n".join(lines) if lines else "(no additional production evidence is required for this scope)"


def _compile_diagnostic_block(
    errors: list[CompileError],
    statements: list[str],
) -> str:
    rows: list[str] = []
    for index, error in enumerate(errors):
        statement = statements[index] if index < len(statements) else "<unavailable>"
        rows.extend([
            f"Diagnostic {index + 1}:",
            f"- generated file: {error.file}",
            f"- line: {error.line}",
            f"- column: {error.col if error.col is not None else '<unavailable>'}",
            f"- exact compiler message: {error.message}",
            f"- offending generated statement: {statement}",
        ])
        rows.extend(f"- compiler detail: {detail}" for detail in error.detail)
    return "\n".join(rows) or "(compiler diagnostics unavailable)"


def build_scoped_compile_repair_messages(
    ctx: GenerationContext,
    current_test: str,
    affected_scope: GeneratedScope,
    errors: list[CompileError],
    offending_statements: list[str],
    *,
    affected_test_names: tuple[str, ...] = (),
    phase: str = "initial compilation",
) -> list[dict]:
    """Build one evidence-based request for all diagnostics in one generated scope."""
    generated_context, owner_ids, relevance_text = _direct_generated_context(
        ctx,
        current_test,
        affected_scope,
        affected_test_names=affected_test_names,
    )
    diagnostic_block = _compile_diagnostic_block(errors, offending_statements)
    production_evidence = _verified_production_evidence(
        ctx,
        owner_ids=owner_ids,
        relevance_text=relevance_text,
        diagnostic_text=diagnostic_block,
    )
    user = f"""Repair one affected scope in a complete generated Java test class during {phase}.

=== EXACT COMPILER EVIDENCE ===
{diagnostic_block}

=== AFFECTED GENERATED SCOPE ===
scope id: {affected_scope.scope_id}
scope kind: {affected_scope.kind.value}
scope name: {affected_scope.name}
```java
{affected_scope.source}
```

=== DIRECTLY RELEVANT GENERATED CONTEXT ===
{generated_context}

=== NECESSARY VERIFIED PRODUCTION EVIDENCE ===
{production_evidence}

REPAIR CONTRACT:
- Do not modify production code.
- Do not invent Java classes, methods, constructors, fields, accessors, enum constants, or generic types.
- Do not modify unrelated tests or passing test methods.
- Do not rewrite the complete generated test class.
- Modify only the affected {affected_scope.kind.value} named {affected_scope.name}.
- Preserve the intended successful ServiceImpl scenario and selected production path.
- Use only the supplied verified Java evidence.
- Preserve every generated primary test; do not delete, rename, comment out, disable, or skip any test.
- Preserve assertNotNull(response); never remove, replace, weaken, or bypass it.
- Return the complete corrected affected scope only, including its annotations and declaration. No class wrapper, package, unrelated imports, markdown, or prose.
"""
    return [{"role": "system", "content": _system(ctx)}, {"role": "user", "content": user}]


def _execution_failure_block(failures: list[TestMethodResult]) -> str:
    sections: list[str] = []
    for failure in failures:
        lines = [
            f"TEST METHOD: {failure.method_name}",
            f"OUTCOME: {failure.status.value}",
            f"EXCEPTION TYPE: {failure.exception_type or '<none>'}",
            f"EXACT DIAGNOSTIC: {failure.message or '<none>'}",
            f"SOURCE LINE: {failure.source_line if failure.source_line is not None else '<unavailable>'}",
        ]
        if failure.status is TestMethodStatus.ASSERTION_FAILURE:
            lines.extend([
                f"EXPECTED VALUE: {failure.expected or '<not emitted>'}",
                f"ACTUAL VALUE: {failure.actual or '<not emitted>'}",
                "RETURNED RESPONSE STATE: " + (
                    f"actual={failure.actual}" if failure.actual is not None
                    else "Surefire emitted no additional response fields"
                ),
                "OBSERVED COLLABORATOR INTERACTIONS: " + (
                    failure.actual_invocation or "Surefire emitted no interaction trace for this assertion"
                ),
            ])
        if failure.status is TestMethodStatus.RUNTIME_ERROR:
            lines.extend([
                f"ROOT CAUSE: {failure.root_cause or '<unavailable>'}",
                "CAUSE CHAIN: " + (" -> ".join(failure.cause_chain) or "<none emitted>"),
                f"GENERATED TEST FRAME: {failure.generated_test_frame or '<unavailable>'}",
                f"SERVICEIMPL/CUT FRAME: {failure.cut_frame or '<unavailable>'}",
                f"NULL OBJECT PATH: {failure.null_path or '<not determinable>'}",
                "FILTERED STACK TRACE:\n" + ("\n".join(failure.filtered_stack_trace) or "<none>"),
            ])
        if failure.status is TestMethodStatus.MOCKITO_FAILURE:
            mockito_diagnostic = "\n".join(
                line for line in failure.raw_diagnostic.splitlines()
                if not line.strip().startswith(("at ", "\tat "))
            ).strip()
            lines.extend([
                f"EXACT MOCKITO DIAGNOSTIC: {mockito_diagnostic[:4000] or failure.message or '<none>'}",
                f"MOCKITO SUBTYPE: {failure.mockito_subtype or '<unclassified>'}",
                f"STUB DECLARATION: {failure.stub_declaration or '<not emitted>'}",
                f"STUB SOURCE LOCATION: {failure.stub_source_location or '<not emitted>'}",
                f"ACTUAL INVOCATION: {failure.actual_invocation or '<not emitted>'}",
                f"ACTUAL INVOCATION SOURCE: {failure.actual_invocation_source_location or '<not emitted>'}",
                f"EXPECTED/STUBBED ARGUMENTS: {failure.expected_arguments}",
                f"ACTUAL INVOCATION ARGUMENTS: {failure.actual_arguments}",
                f"UNUSED STUB LOCATIONS: {failure.unused_stub_locations}",
            ])
        sections.append("\n".join(lines))
    return "\n\n---\n\n".join(sections)


def build_scoped_execution_repair_messages(
    ctx: GenerationContext,
    current_test: str,
    affected_scope: GeneratedScope,
    failures: list[TestMethodResult],
) -> list[dict]:
    """Build one focused assertion/runtime/Mockito repair request per affected scope."""
    affected_names = tuple(failure.method_name for failure in failures)
    generated_context, owner_ids, relevance_text = _direct_generated_context(
        ctx,
        current_test,
        affected_scope,
        affected_test_names=affected_names,
    )
    failure_block = _execution_failure_block(failures)
    production_evidence = _verified_production_evidence(
        ctx,
        owner_ids=owner_ids,
        relevance_text=relevance_text,
        diagnostic_text=failure_block,
    )
    user = f"""Repair one affected scope after a complete-class JUnit/Surefire execution.

=== EXACT CURRENT EXECUTION EVIDENCE ===
{failure_block}

=== AFFECTED GENERATED REPAIR SCOPE ===
scope id: {affected_scope.scope_id}
scope kind: {affected_scope.kind.value}
scope name: {affected_scope.name}
```java
{affected_scope.source}
```

=== DIRECTLY RELEVANT GENERATED CONTEXT ===
{generated_context}

=== RELEVANT VERIFIED PRODUCTION AND COLLABORATOR EVIDENCE ===
{production_evidence}

REPAIR CONTRACT:
- Do not modify production code, unrelated tests, passing test methods, unrelated fixtures, or unrelated stubs.
- Do not rewrite the complete generated test class. Return only the complete corrected affected scope with annotations/declaration.
- Preserve the intended successful ServiceImpl scenario and the supplied selected path.
- Preserve assertNotNull(response). If it failed, repair the fixture/configuration/stub/path cause; do not remove, replace, weaken, or bypass the assertion.
- Do not add speculative assertions for response fields.
- Preserve every generated primary test; never delete, rename, comment out, disable, or skip a test.
- Do not invent Java symbols or behavior; use only supplied verified evidence.
- Do not disable Mockito strict stubbing globally and do not add lenient().
- Do not replace every argument with any(). A typed matcher is allowed only when the CUT constructs that collaborator argument internally and exact values do not select the production branch.
- Do not remove a required stub without determining why it was not reached.
- Use doNothing().when(...) only for verified void methods; never for non-void methods. Use when(...).thenReturn(...) only for verified non-void methods; never for void methods.
- Do not stub getters on real DTOs/entities and do not return values incompatible with the verified generic return type.
- No class wrapper, package, unrelated imports, markdown, or prose.
"""
    return [{"role": "system", "content": _system(ctx)}, {"role": "user", "content": user}]


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
- Fix wildcard-map capture errors by using Map<String, Object> for mutable fixture maps and explicitly casting nested Object values before put/putAll/replace/compute/merge. Never mutate Map<?, ?>.
{service_repair_rule}
- Add missing imports when needed, especially JUnit assertions, Mockito static imports, and production enum/constant imports shown in SOURCE IMPORTS FROM CLASS UNDER TEST.
- Mockito matcher rule: never mix raw values with any()/anyString()/eq()/isNull()/notNull()/argThat() in the same mocked method invocation. If one argument is a matcher, wrap exact raw values with eq(value). Prefer typed matchers such as anyString(), anyInt(), anyLong(), anyBoolean(), or any(Type.class).
- For custom exceptions extending Throwable/Exception/RuntimeException, inherited getMessage(), getCause(), getLocalizedMessage(), getSuppressed(), and getStackTrace() are valid methods even if not listed directly on the subclass.
- For @RestControllerAdvice/@ControllerAdvice tests, do not invent dependency setters such as setEnv()/setEnvironment(). If the handler has a private Environment field and no real setter, inject the mock with ReflectionTestUtils.setField(handler, "env", environment). Stubbing Environment.getProperty(...) is valid.
- For entity/DTO null-field tests, set each tested non-primitive field to null through its verified JavaBean setter or fluent mutator before assertNull(getter()). Do not assert default-null values on a new object.
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
- For entity/DTO null-field tests, set each tested non-primitive field to null through its verified JavaBean setter or fluent mutator before assertNull(getter()).
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


def build_method_generation_validation_repair_messages(
    ctx: GenerationContext,
    method,
    current_method_block: str,
    rejection_reason: str,
) -> list[dict]:
    """Repair one structurally rejected initial method-generation response.

    This is intentionally bounded to one retry.  It handles errors that occur
    before javac can arbitrate, especially Mockito stubbing/verifying of the CUT.
    """
    method_id = f"{method.name}({','.join(type_name for type_name, _ in method.params)})"
    execution_block = (
        render_execution_context(ctx.execution_context, method_id=method_id)
        if ctx.execution_context is not None else "(execution context unavailable)"
    )
    collaborator_block = (
        render_dependency_contracts(ctx.execution_context, method_id=method_id)
        if ctx.execution_context is not None else "(none authorized)"
    )
    user = f"""The initial JUnit method response was structurally rejected. Return the corrected replacement @Test methods only.

=== ONLY AUTHORIZED PRODUCTION METHOD ===
{method.render()}

=== REJECTION REASON ===
{rejection_reason}

{execution_block}

=== EXACT COLLABORATORS AVAILABLE THROUGH THE PUBLIC METHOD AND ITS REAL SAME-CLASS CALL PATH ===
{collaborator_block}

=== REJECTED @TEST METHODS ===
```java
{current_method_block}
```

CORRECTION RULES:
- Output raw Java @Test methods only; no package/import/class/setup/helper/prose.
- Call the selected CUT method normally.
- Never use the CUT as a receiver of when(...), doReturn/doThrow/doNothing(...).when(...), mock(...), spy(...), or verify(...).
- Same-class methods listed as reachable execute normally. Do not stub them. Stub only their exact listed collaborators.
- Replace any CUT spy/stub with the corresponding exact collaborator stubs from the authoritative context.
- If the public entry calls a concrete public/package-visible/protected method on the same class, let it run normally and add the exact collaborator stubs listed for that helper path.
- When the rejection reports missing required collaborator stubs, add every named source-proven non-void stub somewhere in the corrected suite. In particular, preserve the value flow between chained helper collaborators (for example encode quotationNo first, then pass the encoded value to the quotation client).
- Never mutate Map<?, ?>. For any map receiving put/putAll/replace/compute/merge, declare it as Map<String, Object>; cast nested Object values explicitly before mutation.
- Reuse only the listed compiled fixture roots; do not rebuild their DTO/entity/map graphs inline and do not invent helpers.
- Preserve meaningful branch coverage from the rejected block where source facts support it.
- Use only exact fields, map keys, constructors, setters, enums, constants, and collaborator contracts supplied above.
- Keep Mockito stubs local to the test that executes them; no lenient().
- Do not call a different CUT method or a private helper directly.
"""
    return [{"role": "system", "content": _system(ctx)}, {"role": "user", "content": user}]


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
- Same-class public/package-visible/protected helpers execute normally; preserve and stub their exact listed collaborator calls.
- Never call put/putAll/replace/compute/merge on Map<?, ?>. Change the local declaration/cast to Map<String, Object> before mutation.
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
