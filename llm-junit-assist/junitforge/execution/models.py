"""Read-only execution facts for Controller and ServiceImpl test generation.

These models are deliberately isolated from DTO/entity generation.  They model
one production method at a time so prompts can be small, branch-complete, and
schema-grounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ExecutionTargetKind(str, Enum):
    CONTROLLER = "controller"
    SERVICE_IMPL = "service-impl"
    NONE = "none"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    INHERITED = "inherited"
    EXTERNAL = "external"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    PARTIAL = "partial"


@dataclass(slots=True, frozen=True)
class DependencyRef:
    field_name: str | None
    parameter_name: str | None
    declared_type: str
    simple_type: str
    fqcn: str | None
    origin: str
    resolution_status: ResolutionStatus
    mockable: bool = True
    diagnostics: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class InvocationArgument:
    expression: str
    inferred_type: str | None = None
    source_parameter: str | None = None
    literal_value: str | None = None
    transformed: bool = False
    expression_kind: str | None = None


@dataclass(slots=True, frozen=True)
class DependencyMethodContract:
    dependency_field: str
    dependency_type: str
    method_name: str
    return_type: str | None
    parameter_types: tuple[str, ...] = ()
    parameter_names: tuple[str, ...] = ()
    throws: tuple[str, ...] = ()
    declaring_type: str | None = None
    inherited_spring_data: bool = False
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED


@dataclass(slots=True, frozen=True)
class DependencyInvocation:
    invocation_id: str
    caller_method_id: str
    dependency_field: str
    dependency_type: str
    method_name: str
    arguments: tuple[InvocationArgument, ...]
    contract: DependencyMethodContract
    assigned_to: str | None = None
    assignment_chain: tuple[str, ...] = ()
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class BranchFact:
    branch_id: str
    owner_method_id: str
    kind: str
    condition: str | None
    line: int | None = None
    end_line: int | None = None
    parent_branch_id: str | None = None
    parent_arm: str | None = None
    true_range: tuple[int, int] | None = None
    false_range: tuple[int, int] | None = None
    body_range: tuple[int, int] | None = None
    true_path_calls: tuple[str, ...] = ()
    false_path_calls: tuple[str, ...] = ()
    body_path_calls: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class PrivateHelperCall:
    caller_method_id: str
    helper_method_id: str
    arguments: tuple[str, ...] = ()
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class MemberRead:
    owner_method_id: str
    variable_name: str
    property_path: str
    access_kind: str
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class ReturnObjectRead:
    owner_method_id: str
    invocation_id: str
    variable_name: str
    declared_type: str | None
    access_kind: str
    member_name: str
    property_path: str
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class NullGuard:
    guard_id: str
    owner_method_id: str
    expression: str
    operator: str
    compared_with: str = "null"
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class ExitFact:
    owner_method_id: str
    kind: str
    expression: str | None
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class PayloadProperty:
    name: str
    type_name: str
    readable: bool
    writable: bool
    getter: str | None = None
    setter: str | None = None
    builder_method: str | None = None
    constructor_index: int | None = None
    nested_schema_ref: str | None = None
    source: str = ""


@dataclass(slots=True, frozen=True)
class PayloadSchema:
    type_name: str
    fqcn: str | None
    kind: str
    constructors: tuple[str, ...] = ()
    properties: tuple[PayloadProperty, ...] = ()
    enum_constants: tuple[str, ...] = ()
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    depth: int = 0
    recursive_reference: bool = False




@dataclass(slots=True, frozen=True)
class ConfigurationField:
    field_name: str
    type_name: str
    property_key: str | None
    default_value: str | None
    test_value: str | None
    annotation_expr: str
    static: bool = False


@dataclass(slots=True, frozen=True)
class ConfigurationRequirement:
    field_name: str
    expression: str
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class LocalVariableFact:
    name: str
    declared_type: str | None
    initializer: str | None
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class AssignmentFact:
    target: str
    value: str
    operator: str
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class DereferenceRequirement:
    root_variable: str
    full_path: str
    non_null_prefixes: tuple[str, ...]
    terminal_path: str
    expression: str
    line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None
    consumer_kind: str | None = None
    consumer_name: str | None = None
    consumer_scope: str | None = None
    argument_index: int | None = None


@dataclass(slots=True, frozen=True)
class LineFact:
    kind: str
    source: str
    line: int | None = None
    end_line: int | None = None
    branch_id: str | None = None
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class RequestParameter:
    java_name: str
    wire_name: str
    source: str
    type_name: str
    required: bool | None = None
    default_value: str | None = None


@dataclass(slots=True, frozen=True)
class ControllerEndpoint:
    method_id: str
    http_methods: tuple[str, ...] = ()
    class_paths: tuple[str, ...] = ()
    method_paths: tuple[str, ...] = ()
    resolved_paths: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    request_parameters: tuple[RequestParameter, ...] = ()
    response_type: str | None = None


@dataclass(slots=True, frozen=True)
class MethodExecutionContext:
    method_id: str
    signature: str
    source_range: tuple[int, int] | None
    method_source: str
    endpoint: ControllerEndpoint | None
    dependency_invocations: tuple[DependencyInvocation, ...] = ()
    branches: tuple[BranchFact, ...] = ()
    direct_private_helpers: tuple[PrivateHelperCall, ...] = ()
    reachable_private_methods: tuple[str, ...] = ()
    member_reads: tuple[MemberRead, ...] = ()
    return_object_reads: tuple[ReturnObjectRead, ...] = ()
    null_guards: tuple[NullGuard, ...] = ()
    exits: tuple[ExitFact, ...] = ()
    required_object_paths: tuple[str, ...] = ()
    dereference_requirements: tuple[DereferenceRequirement, ...] = ()
    local_variables: tuple[LocalVariableFact, ...] = ()
    assignments: tuple[AssignmentFact, ...] = ()
    configuration_requirements: tuple[ConfigurationRequirement, ...] = ()
    line_facts: tuple[LineFact, ...] = ()
    input_payload_types: tuple[str, ...] = ()
    output_payload_types: tuple[str, ...] = ()
    payload_schemas: tuple[PayloadSchema, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ExecutionContext:
    target_kind: ExecutionTargetKind
    target_fqcn: str
    dependencies: tuple[DependencyRef, ...] = ()
    configuration_fields: tuple[ConfigurationField, ...] = ()
    methods: tuple[MethodExecutionContext, ...] = ()
    payload_schemas: tuple[PayloadSchema, ...] = ()
    diagnostics: tuple[str, ...] = ()
    extraction_status: ResolutionStatus = ResolutionStatus.PARTIAL
    metadata: dict[str, str] = field(default_factory=dict)
