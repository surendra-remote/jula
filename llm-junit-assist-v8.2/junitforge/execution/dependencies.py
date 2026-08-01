"""Exact dependency references and invoked method contracts.

Unlike the compatibility collaborator resolver, this module preserves the
injection-point name.  It is used only for Controller and ServiceImpl context.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from junitforge.execution.models import (
    DependencyMethodContract,
    DependencyRef,
    InvocationArgument,
    ResolutionStatus,
)
from junitforge.models import ClassSymbol, JavaSourceFile, MethodSig

LookupFn = Callable[[str], ClassSymbol | None]

_VALUE_TYPES = {
    "void", "boolean", "byte", "short", "int", "long", "float", "double", "char",
    "Boolean", "Byte", "Short", "Integer", "Long", "Float", "Double", "Character",
    "String", "Object", "BigDecimal", "BigInteger", "UUID", "Date", "LocalDate",
    "LocalDateTime", "ZonedDateTime", "OffsetDateTime", "Instant", "Duration",
    "List", "Set", "Map", "Collection", "Iterable", "Iterator", "Stream",
    "Optional", "Page", "Slice", "ResponseEntity", "HttpHeaders", "HttpStatus",
    "CompletableFuture", "Logger", "Class",
}
_PAYLOAD_SUFFIXES = (
    "Dto", "DTO", "Request", "Response", "Model", "Entity", "Payload",
    "Command", "Event", "Vo", "VO",
)
_SPRING_DATA_SUPERS = {"JpaRepository", "CrudRepository", "PagingAndSortingRepository", "Repository"}
_APPROVED_INHERITED_METHODS = {"findById", "save", "findAll", "count", "deleteById"}


def simple_type_name(rendered: str | None) -> str:
    text = (rendered or "").strip().replace("...", "").replace("[]", "")
    if "<" in text:
        text = text.split("<", 1)[0]
    return text.rsplit(".", 1)[-1].strip()


def normalized_type(rendered: str | None) -> str:
    return re.sub(r"\s+", "", (rendered or "").replace("java.lang.", ""))


def _annotation_names(values: Iterable[str]) -> set[str]:
    return {str(v).split(".")[-1].lstrip("@").lower() for v in values}


def _is_payload_or_value(simple: str, sym: ClassSymbol | None) -> bool:
    if not simple or simple in _VALUE_TYPES or simple.endswith(_PAYLOAD_SUFFIXES):
        return True
    if sym is None:
        return False
    annos = _annotation_names(sym.annotations or [])
    if annos & {"entity", "embeddable", "mappedsuperclass"}:
        return True
    component_annos = {"service", "component", "repository", "controller", "restcontroller"}
    lombok_pojo_annos = {"data", "getter", "setter", "value", "builder", "superbuilder"}
    if annos & lombok_pojo_annos and not annos & component_annos:
        return True
    return sym.name.endswith(_PAYLOAD_SUFFIXES)


def _resolve_type(simple: str, lookup: LookupFn) -> tuple[ClassSymbol | None, ResolutionStatus]:
    sym = lookup(simple)
    if sym is None:
        return None, ResolutionStatus.EXTERNAL
    return sym, ResolutionStatus.RESOLVED


def _eligible_constructors(symbol: ClassSymbol) -> tuple[list[MethodSig], list[str]]:
    constructors = [c for c in (symbol.constructors or []) if "private" not in c.modifiers]
    if not constructors:
        return [], []
    annotated = [
        c for c in constructors
        if _annotation_names(c.annotations or []) & {"autowired", "inject", "resource"}
    ]
    if len(annotated) == 1:
        return annotated, []
    if len(annotated) > 1:
        return [], ["multiple constructors carry injection annotations"]
    if len(constructors) == 1:
        return constructors, []
    return [], ["multiple non-private constructors and no deterministic injection constructor"]


def resolve_dependency_refs(
    source_file: JavaSourceFile,
    symbol: ClassSymbol,
    lookup: LookupFn,
) -> tuple[list[DependencyRef], list[str]]:
    """Resolve exact dependency injection points without collapsing by type."""
    refs: list[DependencyRef] = []
    diagnostics: list[str] = []
    field_by_name = {f.name: f for f in (symbol.fields or [])}
    fields_by_type: dict[str, list[str]] = {}

    for field in symbol.fields or []:
        if "static" in field.modifiers:
            continue
        annos = _annotation_names(field.annotations or [])
        if "value" in annos:
            continue
        simple = simple_type_name(field.type)
        resolved, status = _resolve_type(simple, lookup)
        if _is_payload_or_value(simple, resolved):
            continue
        fields_by_type.setdefault(simple, []).append(field.name)
        refs.append(
            DependencyRef(
                field_name=field.name,
                parameter_name=None,
                declared_type=field.type,
                simple_type=simple,
                fqcn=resolved.fqcn if resolved else None,
                origin="field",
                resolution_status=status,
            )
        )

    constructors, ctor_diagnostics = _eligible_constructors(symbol)
    diagnostics.extend(ctor_diagnostics)
    existing_fields = {(r.field_name, r.simple_type) for r in refs}

    for ctor in constructors:
        for ptype, pname in ctor.params:
            simple = simple_type_name(ptype)
            resolved, status = _resolve_type(simple, lookup)
            if _is_payload_or_value(simple, resolved):
                continue

            mapped_field: str | None = None
            if pname in field_by_name and simple_type_name(field_by_name[pname].type) == simple:
                mapped_field = pname
            else:
                same_type_fields = fields_by_type.get(simple, [])
                if len(same_type_fields) == 1:
                    mapped_field = same_type_fields[0]

            if mapped_field is not None and (mapped_field, simple) in existing_fields:
                # Preserve constructor parameter identity on the existing field ref.
                refs = [
                    DependencyRef(
                        field_name=r.field_name,
                        parameter_name=pname if r.field_name == mapped_field and r.simple_type == simple else r.parameter_name,
                        declared_type=r.declared_type,
                        simple_type=r.simple_type,
                        fqcn=r.fqcn,
                        origin="field+ctor-param" if r.field_name == mapped_field and r.simple_type == simple else r.origin,
                        resolution_status=r.resolution_status,
                        mockable=r.mockable,
                        diagnostics=r.diagnostics,
                    )
                    for r in refs
                ]
                continue

            refs.append(
                DependencyRef(
                    field_name=mapped_field or pname,
                    parameter_name=pname,
                    declared_type=ptype,
                    simple_type=simple,
                    fqcn=resolved.fqcn if resolved else None,
                    origin="ctor-param",
                    resolution_status=status,
                    diagnostics=("constructor parameter could not be mapped to a declared field",) if mapped_field is None else (),
                )
            )

    # Deduplicate only the same injection point, never all instances of a type.
    unique: dict[tuple[str | None, str | None, str], DependencyRef] = {}
    for ref in refs:
        unique[(ref.field_name, ref.parameter_name, ref.declared_type)] = ref
    return list(unique.values()), diagnostics


def _is_mockable_instance_method(method: MethodSig, owner_kind: str) -> bool:
    if method.is_constructor or method.is_static or "private" in method.modifiers:
        return False
    if owner_kind == "interface":
        return True
    return method.is_public


def _is_spring_data_repository(sym: ClassSymbol | None) -> bool:
    if sym is None:
        return False
    supers = {simple_type_name(sym.extends)} if sym.extends else set()
    supers.update(simple_type_name(x) for x in (sym.implements or []))
    annos = _annotation_names(sym.annotations or [])
    return bool(supers & _SPRING_DATA_SUPERS) or "repository" in annos


def _argument_types_match(method: MethodSig, args: tuple[InvocationArgument, ...]) -> bool:
    if len(method.params) != len(args):
        return False
    for (ptype, _), arg in zip(method.params, args):
        if not arg.inferred_type:
            continue
        left = simple_type_name(ptype)
        right = simple_type_name(arg.inferred_type)
        if left == right:
            continue
        primitive_pairs = {
            ("int", "Integer"), ("Integer", "int"),
            ("long", "Long"), ("Long", "long"),
            ("boolean", "Boolean"), ("Boolean", "boolean"),
            ("double", "Double"), ("Double", "double"),
        }
        if (left, right) not in primitive_pairs:
            return False
    return True


def resolve_method_contract(
    dependency: DependencyRef,
    method_name: str,
    args: tuple[InvocationArgument, ...],
    lookup: LookupFn,
    *,
    return_type_hint: str | None = None,
    declaring_type_hint: str | None = None,
    parameter_type_hints: tuple[str, ...] = (),
) -> DependencyMethodContract:
    """Resolve one actually observed dependency invocation."""
    sym = lookup(dependency.simple_type)
    candidates: list[MethodSig] = []
    if sym is not None:
        candidates = [
            m for m in (sym.methods or [])
            if m.name == method_name
            and m.arity == len(args)
            and _is_mockable_instance_method(m, sym.kind)
        ]

    selected: MethodSig | None = None
    if len(candidates) == 1:
        selected = candidates[0]
    elif len(candidates) > 1:
        matching = [m for m in candidates if _argument_types_match(m, args)]
        if len(matching) == 1:
            selected = matching[0]

    if selected is not None:
        return DependencyMethodContract(
            dependency_field=dependency.field_name or dependency.parameter_name or dependency.simple_type,
            dependency_type=dependency.declared_type,
            method_name=method_name,
            return_type=selected.return_type,
            parameter_types=tuple(t for t, _ in selected.params),
            parameter_names=tuple(n for _, n in selected.params),
            throws=tuple(selected.throws or []),
            declaring_type=sym.fqcn if sym else declaring_type_hint,
            inherited_spring_data=False,
            resolution_status=ResolutionStatus.RESOLVED,
        )

    if sym is not None and _is_spring_data_repository(sym) and method_name in _APPROVED_INHERITED_METHODS:
        return DependencyMethodContract(
            dependency_field=dependency.field_name or dependency.parameter_name or dependency.simple_type,
            dependency_type=dependency.declared_type,
            method_name=method_name,
            return_type=return_type_hint,
            parameter_types=parameter_type_hints or tuple(arg.inferred_type or "?" for arg in args),
            declaring_type=sym.fqcn,
            inherited_spring_data=True,
            resolution_status=ResolutionStatus.INHERITED,
        )

    status = ResolutionStatus.AMBIGUOUS if len(candidates) > 1 else dependency.resolution_status
    if return_type_hint or parameter_type_hints or declaring_type_hint:
        status = ResolutionStatus.PARTIAL if status != ResolutionStatus.AMBIGUOUS else status
    return DependencyMethodContract(
        dependency_field=dependency.field_name or dependency.parameter_name or dependency.simple_type,
        dependency_type=dependency.declared_type,
        method_name=method_name,
        return_type=return_type_hint,
        parameter_types=parameter_type_hints or tuple(arg.inferred_type or "?" for arg in args),
        declaring_type=declaring_type_hint or dependency.fqcn,
        inherited_spring_data=False,
        resolution_status=status,
    )
