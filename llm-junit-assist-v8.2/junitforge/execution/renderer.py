"""Render method-isolated execution contexts for diagnostics and LLM prompts."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from junitforge.execution.models import ExecutionContext, MethodExecutionContext, ResolutionStatus


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def execution_context_to_dict(context: ExecutionContext) -> dict[str, Any]:
    return _jsonable(context)


def write_execution_context_json(context: ExecutionContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(execution_context_to_dict(context), indent=2, sort_keys=False),
        encoding="utf-8",
    )


def find_method_context(
    context: ExecutionContext | None,
    method_id: str,
) -> MethodExecutionContext | None:
    if context is None:
        return None
    return next((method for method in context.methods if method.method_id == method_id), None)


def _contract_signature(invocation) -> str:
    contract = invocation.contract
    params = ", ".join(contract.parameter_types) if contract.parameter_types else ", ".join(
        argument.inferred_type or "?" for argument in invocation.arguments
    )
    return_type = contract.return_type or "<unresolved return type>"
    inherited = " [approved Spring Data inherited method]" if contract.inherited_spring_data else ""
    return f"{return_type} {contract.method_name}({params}){inherited}"


def render_dependency_contracts(
    context: ExecutionContext,
    method_id: str | None = None,
) -> str:
    methods = context.methods if method_id is None else tuple(
        method for method in context.methods if method.method_id == method_id
    )
    lines: list[str] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for method in methods:
        for invocation in method.dependency_invocations:
            key = (
                invocation.dependency_field,
                invocation.method_name,
                invocation.contract.parameter_types,
            )
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"Dependency field: {invocation.dependency_field} : {invocation.dependency_type}")
            lines.append(f"- Observed contract: {_contract_signature(invocation)}")
            lines.append(f"- Resolution: {invocation.contract.resolution_status.value}")
    if not lines:
        for dependency in context.dependencies:
            lines.append(
                f"Dependency field: {dependency.field_name or dependency.parameter_name} : "
                f"{dependency.declared_type}"
            )
            lines.append(
                f"- Resolution: {dependency.resolution_status.value}; no invocation contract observed"
            )
    return "\n".join(lines) if lines else "(none)"


def _endpoint_lines(endpoint) -> list[str]:
    if endpoint is None:
        return []
    methods = ", ".join(endpoint.http_methods) or "<mapping verb unresolved>"
    paths = ", ".join(endpoint.resolved_paths) or "<mapping path unresolved>"
    lines = [f"- Endpoint: {methods} {paths}"]
    if endpoint.response_type:
        lines.append(f"- Declared response type: {endpoint.response_type}")
    if endpoint.request_parameters:
        lines.append("- Request bindings:")
        for parameter in endpoint.request_parameters:
            required = "" if parameter.required is None else f", required={str(parameter.required).lower()}"
            default = "" if parameter.default_value is None else f", default={parameter.default_value!r}"
            lines.append(
                f"  - {parameter.java_name}: {parameter.source}, wire={parameter.wire_name!r}, "
                f"type={parameter.type_name}{required}{default}"
            )
    return lines


def _render_payload_schemas(method: MethodExecutionContext) -> list[str]:
    lines: list[str] = ["VERIFIED RECURSIVE PAYLOAD SCHEMAS FOR THIS METHOD:"]
    if not method.payload_schemas:
        lines.append("- none resolved")
        return lines
    for schema in method.payload_schemas:
        recursive = ", recursion-stopped" if schema.recursive_reference else ""
        lines.append(
            f"- {schema.type_name} ({schema.resolution_status.value}, objectLevel={schema.depth}{recursive}, "
            f"construction={schema.construction_kind.value})"
            + (f" fqcn={schema.fqcn}" if schema.fqcn else "")
        )
        if schema.fixture_method_name:
            lines.append(f"  fixture candidate: {schema.fixture_method_name}()")
        if schema.superclass:
            lines.append(f"  superclass: {schema.superclass}")
        if schema.enum_constants:
            lines.append(f"  enum constants: {', '.join(schema.enum_constants)}")
        if schema.constructors:
            lines.append("  constructors:")
            lines.extend(f"    - {constructor}" for constructor in schema.constructors)
        if schema.properties:
            lines.append("  properties:")
            for prop in schema.properties:
                mechanisms: list[str] = []
                if prop.getter:
                    mechanisms.append(f"getter={prop.getter}")
                if prop.setter:
                    mechanisms.append(f"setter={prop.setter}")
                if prop.builder_method:
                    mechanisms.append(f"builder={prop.builder_method}")
                if prop.constructor_index is not None:
                    mechanisms.append(f"ctorIndex={prop.constructor_index}")
                if prop.nested_schema_ref:
                    mechanisms.append(f"nested={prop.nested_schema_ref}")
                if prop.element_type:
                    mechanisms.append(f"element={prop.element_type}")
                if prop.map_key_type or prop.map_value_type:
                    mechanisms.append(f"map={prop.map_key_type}->{prop.map_value_type}")
                if prop.wrapped_type:
                    mechanisms.append(f"wrapped={prop.wrapped_type}")
                if prop.inherited:
                    mechanisms.append(f"inheritedFrom={prop.declared_in}")
                if prop.minimum_size:
                    mechanisms.append(f"minimumSize={prop.minimum_size}")
                lines.append(
                    f"    - {prop.name}: {prop.type_name}; kind={prop.kind.value}; "
                    f"readable={str(prop.readable).lower()}, writable={str(prop.writable).lower()}, "
                    f"baselineRequired={str(prop.baseline_required).lower()}"
                    + (f"; {', '.join(mechanisms)}" if mechanisms else "")
                )
        elif schema.resolution_status != ResolutionStatus.RESOLVED:
            lines.append("  unresolved: do not invent fields, accessors, constructors, or builders")
    return lines


def _render_method(method: MethodExecutionContext) -> list[str]:
    lines = [
        f"PUBLIC ENTRY METHOD: {method.signature}",
        "METHOD SOURCE (only this production method and its reachable facts authorize tests):",
        "```java",
        method.method_source,
        "```",
    ]
    lines.extend(_endpoint_lines(method.endpoint))
    if method.service_contract_signature:
        lines.append(f"MATCHED SERVICE CONTRACT: {method.service_contract_signature}")
    if method.selected_primary_path is not None:
        selection = method.selected_primary_path
        lines.append(
            f"SELECTED PRIMARY SUCCESSFUL PATH: score={selection.score}; feasibility={selection.feasibility.value}"
        )
        for choice in selection.branch_choices:
            lines.append(
                f"- {choice.branch_id} -> {choice.arm}; score={choice.score}; reason={choice.reason}"
            )
        if selection.limitation:
            lines.append(f"- limitation: {selection.limitation}")
    if method.required_fixture_ids:
        lines.append("REQUIRED EXISTING FIXTURE IDS FOR THIS METHOD:")
        lines.extend(f"- {fixture_id}" for fixture_id in method.required_fixture_ids)
    else:
        lines.append("REQUIRED EXISTING FIXTURE IDS: none verified")
    if method.map_schema_ids:
        lines.append("REQUIRED DYNAMIC MAP SCHEMAS:")
        lines.extend(f"- {schema_id}" for schema_id in method.map_schema_ids)

    if method.reachable_private_methods:
        lines.append("REACHABLE PRIVATE HELPERS (cover only through the public method):")
        lines.extend(f"- {helper}" for helper in method.reachable_private_methods)
    else:
        lines.append("REACHABLE PRIVATE HELPERS: none detected")

    if method.branches:
        lines.append("BRANCHES AND NESTING:")
        for branch in method.branches:
            parent = (
                f", parent={branch.parent_branch_id}/{branch.parent_arm}"
                if branch.parent_branch_id else ""
            )
            lines.append(
                f"- {branch.branch_id}: kind={branch.kind}, condition={branch.condition!r}, "
                f"lines={branch.line}-{branch.end_line}{parent}"
            )
            if branch.true_path_calls:
                lines.append(f"  then calls: {', '.join(branch.true_path_calls)}")
            if branch.false_path_calls:
                lines.append(f"  else calls: {', '.join(branch.false_path_calls)}")
            if branch.body_path_calls:
                lines.append(f"  body/catch/case calls: {', '.join(branch.body_path_calls)}")
    else:
        lines.append("BRANCHES: none detected")

    if method.null_guards:
        lines.append("NULL GUARDS:")
        for guard in method.null_guards:
            location = f" branch={guard.branch_id}/{guard.branch_arm}" if guard.branch_id else ""
            lines.append(
                f"- {guard.guard_id}: {guard.expression} {guard.operator} {guard.compared_with}; "
                f"line={guard.line}{location}"
            )
    else:
        lines.append("NULL GUARDS: none detected")

    if method.required_object_paths:
        lines.append("REQUIRED NON-NULL OBJECT PATHS FOR EXECUTED GETTER/FIELD CHAINS:")
        lines.extend(f"- {path}" for path in method.required_object_paths)
    else:
        lines.append("REQUIRED NON-NULL OBJECT PATHS: none detected")

    if method.member_reads:
        lines.append("MEMBER READS BY BRANCH:")
        for read in method.member_reads:
            location = f" branch={read.branch_id}/{read.branch_arm}" if read.branch_id else ""
            lines.append(
                f"- {read.variable_name}.{read.property_path}; access={read.access_kind}; "
                f"line={read.line}{location}"
            )

    if method.dereference_requirements:
        lines.append("EXACT DEREFERENCE / NON-NULL REQUIREMENTS BY SOURCE LINE:")
        for requirement in method.dereference_requirements:
            location = f" branch={requirement.branch_id}/{requirement.branch_arm}" if requirement.branch_id else ""
            prefixes = ", ".join(requirement.non_null_prefixes) or "none"
            consumer = ""
            if requirement.consumer_kind:
                consumer = f"; consumer={requirement.consumer_kind}"
                if requirement.consumer_name:
                    consumer += f" {requirement.consumer_scope + '.' if requirement.consumer_scope else ''}{requirement.consumer_name}"
            lines.append(
                f"- line={requirement.line}{location}: expression={requirement.expression}; "
                f"mustBeNonNull=[{prefixes}]; terminal={requirement.terminal_path}{consumer}"
            )
        lines.append(
            "- Create and attach every mustBeNonNull prefix before invoking the CUT. "
            "The terminal property may be null only when the selected source branch/consumer permits it."
        )

    if method.configuration_requirements:
        lines.append("@VALUE FIELDS READ BY THIS METHOD / REACHABLE HELPERS:")
        for requirement in method.configuration_requirements:
            location = f" branch={requirement.branch_id}/{requirement.branch_arm}" if requirement.branch_id else ""
            lines.append(
                f"- {requirement.field_name}; expression={requirement.expression}; line={requirement.line}{location}"
            )
        lines.append(
            "- The skeleton injects deterministic defaults. Override with ReflectionTestUtils.setField "
            "inside an individual test only when a listed branch requires a different configuration value."
        )

    if method.line_facts:
        lines.append("STATEMENT-BY-STATEMENT SOURCE FACTS:")
        for fact in method.line_facts:
            location = f" branch={fact.branch_id}/{fact.branch_arm}" if fact.branch_id else ""
            compact = " ".join(fact.source.split())
            if len(compact) > 260:
                compact = compact[:257] + "..."
            lines.append(f"- lines={fact.line}-{fact.end_line}; kind={fact.kind}{location}; source={compact}")

    if method.dependency_invocations:
        lines.append("ACTUAL COLLABORATOR INVOCATIONS FOR THIS METHOD/PUBLIC HELPER PATH:")
        for invocation in method.dependency_invocations:
            arguments = ", ".join(argument.expression for argument in invocation.arguments)
            location = (
                f"; branch={invocation.branch_id}/{invocation.branch_arm}"
                if invocation.branch_id else ""
            )
            assigned = f"; assignedTo={invocation.assigned_to}" if invocation.assigned_to else ""
            lines.append(
                f"- {invocation.invocation_id}: {invocation.dependency_field}."
                f"{invocation.method_name}({arguments}) -> {_contract_signature(invocation)}"
                f"; line={invocation.line}{assigned}{location}"
            )
    else:
        lines.append("ACTUAL COLLABORATOR INVOCATIONS: none detected; do not create Mockito stubs")

    if method.return_object_reads:
        lines.append("FIELDS/GETTERS READ FROM COLLABORATOR RETURN VALUES:")
        for read in method.return_object_reads:
            location = f" branch={read.branch_id}/{read.branch_arm}" if read.branch_id else ""
            lines.append(
                f"- invocation={read.invocation_id}, variable={read.variable_name}, "
                f"propertyPath={read.property_path}, type={read.declared_type}, "
                f"access={read.access_kind}{location}"
            )
        lines.append(
            "- thenReturn payloads must initialize every verified nested object/property read by the selected branch."
        )
    else:
        lines.append("COLLABORATOR RETURN MEMBER READS: none detected")

    if method.exits:
        lines.append("RETURNS / THROWS BY BRANCH:")
        for exit_fact in method.exits:
            location = f" branch={exit_fact.branch_id}/{exit_fact.branch_arm}" if exit_fact.branch_id else ""
            lines.append(
                f"- {exit_fact.kind} {exit_fact.expression!r}; line={exit_fact.line}{location}"
            )

    if method.collection_requirements:
        lines.append("COLLECTION / WRAPPER CARDINALITY REQUIREMENTS:")
        for requirement in method.collection_requirements:
            lines.append(
                f"- {requirement.path}: minimumSize={requirement.minimum_size}, "
                f"indexes={list(requirement.accessed_indexes)}, wrapperPresent={str(requirement.wrapper_present).lower()}"
                + (f", matches={list(requirement.matching_constraints)}" if requirement.matching_constraints else "")
            )

    lines.extend(_render_payload_schemas(method))
    if method.diagnostics:
        lines.append("METHOD EXTRACTION DIAGNOSTICS:")
        lines.extend(f"- {diagnostic}" for diagnostic in method.diagnostics)
    return lines


def _render_method_compact(method: MethodExecutionContext) -> list[str]:
    """Render the method-generation contract without duplicating raw AST facts.

    The generation prompt already contains the exact public method source.  The
    full report renderer remains available for audit JSON/diagnostics, while this
    compact view carries only test-arrangement facts that materially constrain
    generated JUnit code.
    """
    lines = [f"PUBLIC ENTRY METHOD: {method.signature}"]
    if method.service_contract_signature:
        lines.append(f"MATCHED SERVICE CONTRACT: {method.service_contract_signature}")
    if method.selected_primary_path is not None:
        selection = method.selected_primary_path
        lines.append(
            f"SELECTED PRIMARY SUCCESSFUL PATH: score={selection.score}; feasibility={selection.feasibility.value}"
        )
        for choice in selection.branch_choices:
            lines.append(f"- {choice.branch_id} -> {choice.arm}; {choice.reason}")
        if selection.limitation:
            lines.append(f"- LIMITATION: {selection.limitation}")
    if method.required_fixture_ids:
        lines.append("DIRECT FIXTURE ROOTS THE TEST MAY CALL:")
        lines.extend(f"- {fixture_id}" for fixture_id in method.required_fixture_ids)
    else:
        lines.append("DIRECT FIXTURE ROOTS: none verified")

    if method.reachable_private_methods:
        lines.append("REACHABLE SAME-CLASS METHODS EXECUTED NORMALLY — NEVER STUB OR VERIFY THEM:")
        lines.extend(f"- {helper}" for helper in method.reachable_private_methods)

    if method.branches:
        lines.append("BRANCH INVENTORY:")
        for branch in method.branches:
            parent = f"; parent={branch.parent_branch_id}/{branch.parent_arm}" if branch.parent_branch_id else ""
            lines.append(f"- {branch.branch_id}: {branch.kind}; condition={branch.condition!r}{parent}")

    if method.dependency_invocations:
        lines.append("EXACT COLLABORATOR CALLS ACROSS THE PUBLIC METHOD AND REACHABLE SAME-CLASS METHODS:")
        for invocation in method.dependency_invocations:
            arguments = ", ".join(argument.expression for argument in invocation.arguments)
            assigned = f"; assignedTo={invocation.assigned_to}" if invocation.assigned_to else ""
            branch = f"; branch={invocation.branch_id}/{invocation.branch_arm}" if invocation.branch_id else ""
            lines.append(
                f"- {invocation.dependency_field}.{invocation.method_name}({arguments}) "
                f"-> {_contract_signature(invocation)}{assigned}{branch}"
            )
    else:
        lines.append("EXACT COLLABORATOR CALLS: none; create no Mockito stubs")

    if method.return_object_reads:
        lines.append("COLLABORATOR RETURN PATHS THAT MUST BE HYDRATED:")
        for read in method.return_object_reads:
            lines.append(
                f"- {read.invocation_id}: {read.variable_name}.{read.property_path} "
                f"type={read.declared_type or '<unknown>'}"
            )

    # Retain only dereferences rooted in public parameters or collaborator
    # return variables. Internal locals created by production code do not need
    # test-side construction instructions and were the largest prompt duplicate.
    external_roots = {
        invocation.assigned_to
        for invocation in method.dependency_invocations
        if invocation.assigned_to
    }
    header = method.signature.split("(", 1)[1].rsplit(")", 1)[0] if "(" in method.signature else ""
    for parameter in header.split(","):
        tokens = parameter.strip().split()
        if tokens:
            external_roots.add(tokens[-1])
    external_requirements = [
        requirement for requirement in method.dereference_requirements
        if requirement.root_variable in external_roots
    ]
    if external_requirements:
        lines.append("EXTERNAL INPUT/RETURN NON-NULL REQUIREMENTS:")
        for requirement in external_requirements:
            prefixes = ", ".join(requirement.non_null_prefixes)
            lines.append(f"- {requirement.expression}: [{prefixes}]")

    if method.collection_requirements:
        lines.append("COLLECTION/WRAPPER REQUIREMENTS:")
        for requirement in method.collection_requirements:
            lines.append(
                f"- {requirement.path}: minimumSize={requirement.minimum_size}; "
                f"indexes={list(requirement.accessed_indexes)}; present={str(requirement.wrapper_present).lower()}"
            )

    if method.configuration_requirements:
        lines.append("CONFIGURATION FIELDS READ:")
        lines.extend(f"- {requirement.field_name}" for requirement in method.configuration_requirements)

    if method.exits:
        lines.append("RETURNS/THROWS:")
        for fact in method.exits:
            lines.append(f"- {fact.kind}: {fact.expression!r}; branch={fact.branch_id}/{fact.branch_arm}")

    if method.diagnostics:
        lines.append("METHOD EXTRACTION DIAGNOSTICS:")
        lines.extend(f"- {diagnostic}" for diagnostic in method.diagnostics)
    return lines


def render_execution_context(context: ExecutionContext, method_id: str | None = None) -> str:
    lines = [
        "=== AUTHORITATIVE METHOD EXECUTION CONTEXT ===",
        f"Target kind: {context.target_kind.value}",
        f"Extraction status: {context.extraction_status.value}",
        "The LLM may generate tests only from the selected method facts below.",
        "Unresolved facts are not permission to invent APIs or payload fields.",
    ]
    if context.configuration_fields:
        lines.append("CLASS @VALUE CONFIGURATION FIELDS (injected by deterministic test skeleton):")
        for field in context.configuration_fields:
            default = f", default={field.default_value!r}" if field.default_value is not None else ""
            test_value = field.test_value if field.test_value is not None else "<unsupported type: no automatic literal>"
            lines.append(
                f"- {field.field_name}: type={field.type_name}, property={field.property_key!r}"
                f"{default}, testLiteral={test_value}, static={str(field.static).lower()}"
            )
    else:
        lines.append("CLASS @VALUE CONFIGURATION FIELDS: none")

    methods = context.methods if method_id is None else tuple(
        method for method in context.methods if method.method_id == method_id
    )
    selected_fixture_ids = {
        fixture_id for method in methods for fixture_id in method.required_fixture_ids
    } if method_id is not None else {fixture.fixture_id for fixture in context.fixtures}
    selected_map_ids = {
        schema_id for method in methods for schema_id in method.map_schema_ids
    } if method_id is not None else {schema.schema_id for schema in context.map_schemas}

    fixtures = tuple(
        fixture for fixture in context.fixtures
        if fixture.fixture_id in selected_fixture_ids and not fixture.internal
    )
    if fixtures:
        if context.target_kind.methodwise_generation:
            heading = "COMPILED REUSABLE FIXTURE ROOTS FOR THIS REQUEST:" if method_id else "COMPILED REUSABLE FIXTURE CATALOG:"
        else:
            heading = "DETERMINISTIC REUSABLE FIXTURE ROOTS FOR THIS REQUEST:" if method_id else "DETERMINISTIC REUSABLE FIXTURE CATALOG (inserted before compile):"
        lines.append(heading)
        projection_calls = ({
            projection.fixture_id: projection.call_expression
            for method in methods
            for projection in method.fixture_projections
            if projection.call_expression
        } if method_id is not None else {})
        for fixture in fixtures:
            state = "supported" if fixture.supported else "unsupported"
            call = projection_calls.get(fixture.fixture_id)
            if call is None:
                call = (
                    f"{fixture.method_name}(String fixtureScenario, String... requiredPaths)"
                    if fixture.projection_aware
                    else f"{fixture.method_name}()"
                )
            lines.append(f"- {fixture.fixture_id}: {fixture.return_type} {call} [{state}]")
            for diagnostic in fixture.diagnostics:
                lines.append(f"  diagnostic: {diagnostic}")
    else:
        lines.append("COMPILED REUSABLE FIXTURE ROOTS: none")

    maps = tuple(schema for schema in context.map_schemas if schema.schema_id in selected_map_ids)
    if maps:
        lines.append("DYNAMIC MAP ROOTS FOR THIS REQUEST:" if method_id else "DYNAMIC MAP SCHEMA CATALOG:")
        for schema in maps:
            keys = ", ".join(
                f'{entry.key_literal}:{entry.runtime_type or entry.kind.value}' for entry in schema.entries
            ) or "<no verified keys>"
            lines.append(f"- {schema.schema_id} ({schema.semantic_name}): {keys}")

    for index, method in enumerate(methods):
        if index:
            lines.extend(["", "--- NEXT METHOD ---"])
        lines.extend(_render_method_compact(method) if method_id is not None else _render_method(method))

    if context.diagnostics:
        lines.extend(["", "CLASS-LEVEL EXTRACTION DIAGNOSTICS:"])
        lines.extend(f"- {diagnostic}" for diagnostic in context.diagnostics)

    if context.target_kind.value == "service-impl":
        lines.extend([
            "",
            "PRIMARY SERVICEIMPL GENERATION RULES:",
            "- Generate exactly one initial test for this matched Service-interface entry method.",
            "- Execute only the selected primary successful path and its listed same-class helpers.",
            "- Do not add null/blank/empty/error/status/product/transaction or speculative permutations.",
            "- Use only the exact method-specific fixture projection calls; each call returns fresh mutable instances and excludes unrelated associations.",
            "- Stub every listed collaborator invocation exactly once on the selected path, including helper-origin calls, inside this test only; use non-void Mockito return syntax only for non-void methods and doNothing().when(...) for void methods.",
            "- Do not stub a same-class helper or a collaborator absent from the selected path.",
            "- Invoke the selected public entry once, capture it as response, and use only assertNotNull(response).",
            "- Do not assert response fields, codes, statuses, messages, values, identifiers, timestamps, equality, or collection contents.",
            "- Use only source-proven constants/enums/configuration/comparisons or bounded predicate-compatible witnesses; never invent TYPE1/CODE1/STATUS1/test-value placeholders.",
        ])
    else:
        lines.extend([
            "",
            "METHOD-SPECIFIC GENERATION RULES:",
            "- Generate meaningful tests for every listed branch, nested branch, null guard, return, and throw of each selected method.",
            "- Initialize the complete verified nested object graph required by the selected branch before invoking the CUT.",
            "- For every dereference chain, all listed intermediate prefixes must be non-null; do not confuse a null-safe terminal consumer with a null-safe parent object.",
            "- Example: StringUtils.defaultString(unit.getValue()) permits a null value, but unit itself must still be initialized.",
            "- Use source-proven @Value test literals only; method-wise skeleton targets inject them deterministically, while class-wide targets must use exact-field ReflectionTestUtils setup when required.",
            "- Stub every actual collaborator invocation listed for the selected public path, including invocations inside public/protected/package-visible same-class helpers.",
            "- Same-class helper methods execute normally and are never Mockito receivers.",
            "- Keep stubs local to the individual test method; do not use lenient().",
            "- Call the selected public CUT method normally; never stub or verify the CUT.",
            "- Do not call a different CUT method, including plausible methods inferred from names or comments.",
            "- Use only verified payload fields, getters, setters, builders, constructors, and enum constants.",
            "- Call only the listed deterministic fixture methods; the engine supplies their verified Java implementations before compilation, so do not recreate those schema graphs inline.",
            "- Mutate only the smallest branch-specific field/path from a fresh fixture invocation.",
            "- Map<?, ?> is read-only in generated tests. Any map receiving put/putAll/replace/compute/merge must be declared/cast as Map<String, Object> before mutation.",
            "- If a required payload type or collaborator contract is unresolved, skip that unsupported path instead of guessing.",
            "- Private helpers must be covered only through the selected public method.",
        ])
    if context.target_kind.value == "controller":
        lines.extend([
            "- Use standalone MockMvc only.",
            "- Build request bodies from verified recursive schemas and assert only verified JSON property names.",
            "- Cover endpoint branches with their real HTTP status/response behavior from source; never invent status codes.",
        ])
    return "\n".join(lines)


def write_schema_catalog_json(context: ExecutionContext, path: Path) -> None:
    """Write the reusable class/map schema catalog and per-method usage index."""
    payload = {
        "target": context.target_fqcn,
        "metadata": _jsonable(context.metadata),
        "schemas": _jsonable(context.payload_schemas),
        "dynamicMapSchemas": _jsonable(context.map_schemas),
        "methodUsage": [
            {
                "methodId": method.method_id,
                "requiredSchemaIds": list(method.required_schema_ids),
                "mapSchemaIds": list(method.map_schema_ids),
                "requiredObjectPaths": list(method.required_object_paths),
                "collectionRequirements": _jsonable(method.collection_requirements),
            }
            for method in context.methods
        ],
        "diagnostics": list(context.diagnostics),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")


def write_fixture_catalog_json(context: ExecutionContext, path: Path) -> None:
    """Write fixture signatures, dependencies, diagnostics, and method reuse."""
    payload = {
        "target": context.target_fqcn,
        "fixtures": _jsonable(context.fixtures),
        "methodUsage": [
            {
                "methodId": method.method_id,
                "requiredFixtureIds": list(method.required_fixture_ids),
                "fixtureProjections": _jsonable(method.fixture_projections),
            }
            for method in context.methods
        ],
        "unsupported": [
            _jsonable(fixture)
            for fixture in context.fixtures
            if not fixture.supported
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
