"""Bounded Service-interface contract resolution for ServiceImpl generation.

This module deliberately resolves only the interface and superclass declarations
reachable from the selected class.  It does not build a repository-wide type
graph.  Its output is used to select the concrete Service entry methods that
receive an initial primary test; unrelated public helpers never enter that set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from junitforge.execution.models import ResolutionStatus, ServiceMethodContract
from junitforge.models import ClassSymbol, MethodSig

LookupFn = Callable[..., ClassSymbol | None]

_PRIMITIVES = {"void", "boolean", "byte", "short", "int", "long", "float", "double", "char"}
_JAVA_LANG = {
    "Boolean", "Byte", "Short", "Integer", "Long", "Float", "Double", "Character",
    "String", "CharSequence", "Object", "Number", "Class", "Throwable", "Exception",
    "RuntimeException", "Void",
}
_JAVA_UTIL = {
    "Collection", "Iterable", "Iterator", "List", "Set", "Map", "SortedMap", "Queue",
    "Deque", "Optional", "Date", "UUID", "ArrayList", "LinkedList", "HashSet",
    "LinkedHashSet", "HashMap", "LinkedHashMap", "Comparator",
}


@dataclass(slots=True, frozen=True)
class ServiceEntryResolution:
    contracts: tuple[ServiceMethodContract, ...]
    matched_methods: tuple[MethodSig, ...]
    diagnostics: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class _AvailableMethod:
    method: MethodSig
    owner: ClassSymbol
    substitutions: tuple[tuple[str, str], ...]
    inherited: bool = False


def _lookup(lookup: LookupFn, type_name: str, owner: ClassSymbol | None = None) -> ClassSymbol | None:
    try:
        return lookup(type_name, owner)
    except TypeError:
        return lookup(type_name)


def _split_generic_args(rendered: str) -> tuple[str, ...]:
    start = rendered.find("<")
    end = rendered.rfind(">")
    if start < 0 or end <= start:
        return ()
    body = rendered[start + 1:end]
    out: list[str] = []
    current: list[str] = []
    depth = 0
    for char in body:
        if char == "<":
            depth += 1
        elif char == ">" and depth:
            depth -= 1
        if char == "," and depth == 0:
            value = "".join(current).strip()
            if value:
                out.append(value)
            current = []
        else:
            current.append(char)
    value = "".join(current).strip()
    if value:
        out.append(value)
    return tuple(out)


def _raw_type(rendered: str) -> str:
    text = (rendered or "").strip()
    while text.endswith("[]"):
        text = text[:-2].strip()
    if text.endswith("..."):
        text = text[:-3].strip()
    return text.split("<", 1)[0].strip()


def _type_parameter_name(rendered: str) -> str:
    match = re.match(r"\s*([A-Za-z_$][\w$]*)", rendered or "")
    return match.group(1) if match else ""


def _declared_type_erasures(symbol: ClassSymbol) -> dict[str, str]:
    """Return Java-erasure witnesses for class/interface type parameters."""
    bindings: dict[str, str] = {}
    for rendered in symbol.type_parameters or ():
        name = _type_parameter_name(rendered)
        if not name:
            continue
        bound = re.search(r"\bextends\s+(.+)", rendered or "")
        first_bound = bound.group(1).split("&", 1)[0].strip() if bound else "java.lang.Object"
        bindings[name] = first_bound or "java.lang.Object"
    return bindings


def _substitute(rendered: str, substitutions: dict[str, str]) -> str:
    value = rendered or ""
    # Parent-interface substitutions may themselves reference a child type
    # parameter.  A small fixed point is sufficient for legal source type chains.
    for _ in range(8):
        previous = value
        for name in sorted(substitutions, key=len, reverse=True):
            if not name:
                continue
            value = re.sub(rf"(?<![\w$]){re.escape(name)}(?![\w$])", substitutions[name], value)
        if value == previous:
            break
    return value


def _canonical_type(
    rendered: str,
    substitutions: dict[str, str],
    owner: ClassSymbol,
    lookup: LookupFn,
) -> tuple[str | None, str | None]:
    value = re.sub(r"@[A-Za-z_$][\w$.]*(?:\s*\([^)]*\))?\s*", "", rendered or "")
    value = re.sub(r"\bfinal\b", "", value).strip()
    value = _substitute(value, substitutions).replace("$", ".")
    if not value:
        return None, "empty parameter type"

    array_depth = 0
    if value.endswith("..."):
        value = value[:-3].strip()
        array_depth += 1
    while value.endswith("[]"):
        value = value[:-2].strip()
        array_depth += 1

    wildcard_prefix = ""
    wildcard = re.match(r"^\?\s*(?:(extends|super)\s+(.+))?$", value)
    if wildcard:
        if wildcard.group(1) is None:
            return "?" + "[]" * array_depth, None
        wildcard_prefix = f"?{wildcard.group(1)}"
        value = wildcard.group(2).strip()

    raw = _raw_type(value)
    args = _split_generic_args(value)
    if raw in substitutions:
        # A raw generic declaration without an actual type argument cannot be
        # matched safely to every public method.
        return None, f"unsubstituted type variable: {raw}"

    if raw in _PRIMITIVES:
        canonical_raw = raw
    elif raw in _JAVA_LANG:
        canonical_raw = f"java.lang.{raw}"
    elif raw in _JAVA_UTIL:
        canonical_raw = f"java.util.{raw}"
    else:
        resolved = _lookup(lookup, raw, owner)
        if resolved is not None:
            canonical_raw = resolved.fqcn
        elif "." in raw:
            canonical_raw = raw
        else:
            return None, f"parameter type unresolved in {owner.fqcn}: {raw}"

    canonical_args: list[str] = []
    for argument in args:
        canonical, problem = _canonical_type(argument, substitutions, owner, lookup)
        if canonical is None:
            return None, problem
        canonical_args.append(canonical)
    rendered_args = "<" + ",".join(canonical_args) + ">" if canonical_args else ""
    prefix = wildcard_prefix
    return f"{prefix}{canonical_raw}{rendered_args}{'[]' * array_depth}", None


def _canonical_signature(
    method: MethodSig,
    substitutions: dict[str, str],
    owner: ClassSymbol,
    lookup: LookupFn,
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    parameters: list[str] = []
    diagnostics: list[str] = []
    for type_name, _ in method.params:
        canonical, problem = _canonical_type(type_name, substitutions, owner, lookup)
        if canonical is None:
            diagnostics.append(problem or f"parameter type unresolved: {type_name}")
        else:
            parameters.append(canonical)
    if diagnostics or len(parameters) != len(method.params):
        return None, tuple(parameters), tuple(diagnostics)
    return f"{method.name}({','.join(parameters)})", tuple(parameters), ()


def _type_bindings(type_ref: str, target: ClassSymbol, inherited: dict[str, str]) -> tuple[dict[str, str], str | None]:
    rendered = _substitute(type_ref, inherited)
    actuals = _split_generic_args(rendered)
    formals = tuple(_type_parameter_name(value) for value in (target.type_parameters or ()))
    if not formals:
        return {}, None
    if len(actuals) != len(formals):
        return {}, (
            f"generic arguments unresolved for {rendered}: expected {len(formals)}, "
            f"found {len(actuals)}"
        )
    return dict(zip(formals, actuals)), None


def _available_concrete_methods(symbol: ClassSymbol, lookup: LookupFn) -> tuple[list[_AvailableMethod], list[str]]:
    available: list[_AvailableMethod] = []
    diagnostics: list[str] = []
    current = symbol
    substitutions: dict[str, str] = _declared_type_erasures(symbol)
    inherited = False
    seen_types: set[str] = set()
    while current and current.fqcn not in seen_types:
        seen_types.add(current.fqcn)
        for method in current.methods or ():
            if not method.is_public or method.is_static or method.is_abstract or method.is_constructor:
                continue
            available.append(_AvailableMethod(method, current, tuple(substitutions.items()), inherited))
        parent_ref = current.extends
        if not parent_ref or _raw_type(parent_ref).rsplit(".", 1)[-1] in {"Object", "Record", "Enum"}:
            break
        parent = _lookup(lookup, _raw_type(parent_ref), current)
        if parent is None:
            diagnostics.append(f"ServiceImpl superclass unresolved: {current.fqcn} extends {parent_ref}")
            break
        substitutions, problem = _type_bindings(parent_ref, parent, substitutions)
        if problem:
            diagnostics.append(problem)
            substitutions = {}
        current = parent
        inherited = True
    return available, diagnostics


def resolve_service_entries(symbol: ClassSymbol, lookup: LookupFn) -> ServiceEntryResolution:
    """Resolve Service contracts and their concrete source-declared methods."""
    diagnostics: list[str] = []
    contracts: list[ServiceMethodContract] = []
    if not symbol.implements:
        message = f"ServiceImpl declares no Service interface: {symbol.fqcn}"
        return ServiceEntryResolution((), (), (message,))

    available, available_diagnostics = _available_concrete_methods(symbol, lookup)
    diagnostics.extend(available_diagnostics)
    available_signatures: list[tuple[str, _AvailableMethod]] = []
    seen_available_signatures: set[str] = set()
    for item in available:
        signature, _, problems = _canonical_signature(
            item.method, dict(item.substitutions), item.owner, lookup
        )
        if signature is None:
            diagnostics.extend(
                f"concrete method signature unresolved [{item.method.render()}]: {problem}"
                for problem in problems
            )
            continue
        # Child methods are visited before superclass methods; retain the
        # closest concrete override while preserving same-arity overloads.
        if signature in seen_available_signatures:
            continue
        seen_available_signatures.add(signature)
        available_signatures.append((signature, item))

    class_erasures = _declared_type_erasures(symbol)
    queue: list[tuple[str, ClassSymbol, dict[str, str], str]] = [
        (interface_ref, symbol, class_erasures, interface_ref) for interface_ref in symbol.implements
    ]
    seen_interfaces: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    seen_contracts: set[str] = set()
    matched_method_ids: set[str] = set()

    while queue:
        interface_ref, declaring_owner, inherited_substitutions, root_ref = queue.pop(0)
        resolved_ref = _substitute(interface_ref, inherited_substitutions)
        raw = _raw_type(resolved_ref)
        interface = _lookup(lookup, raw, declaring_owner)
        if interface is None:
            message = f"Service interface unresolved: {resolved_ref} declared by {declaring_owner.fqcn}"
            diagnostics.append(message)
            contracts.append(ServiceMethodContract(
                interface_type=root_ref,
                declaring_interface=None,
                method_name=None,
                resolution_status=ResolutionStatus.UNRESOLVED,
                diagnostics=(message,),
            ))
            continue
        if interface.kind != "interface":
            message = f"implemented type is not an interface: {resolved_ref} -> {interface.fqcn}"
            diagnostics.append(message)
            contracts.append(ServiceMethodContract(
                interface_type=root_ref,
                declaring_interface=interface.fqcn,
                method_name=None,
                resolution_status=ResolutionStatus.UNRESOLVED,
                diagnostics=(message,),
            ))
            continue

        substitutions, binding_problem = _type_bindings(resolved_ref, interface, inherited_substitutions)
        state = (interface.fqcn, tuple(sorted(substitutions.items())))
        if state in seen_interfaces:
            continue
        seen_interfaces.add(state)
        if binding_problem:
            diagnostics.append(binding_problem)

        for method in interface.methods or ():
            if method.is_constructor or method.is_static or "private" in method.modifiers:
                continue
            signature, parameter_types, signature_problems = _canonical_signature(
                method, substitutions, interface, lookup
            )
            if signature is not None and signature in seen_contracts:
                continue
            if signature is not None:
                seen_contracts.add(signature)
            if binding_problem or signature is None:
                problems = tuple(dict.fromkeys((binding_problem, *signature_problems))) if binding_problem else signature_problems
                problem_text = tuple(value for value in problems if value)
                contract = ServiceMethodContract(
                    interface_type=root_ref,
                    declaring_interface=interface.fqcn,
                    method_name=method.name,
                    resolved_parameter_types=parameter_types,
                    normalized_signature=signature,
                    resolution_status=ResolutionStatus.UNRESOLVED,
                    diagnostics=problem_text,
                )
                contracts.append(contract)
                diagnostics.extend(
                    f"Service contract unresolved [{interface.fqcn}.{method.name}]: {problem}"
                    for problem in problem_text
                )
                continue

            if (method.return_type or "void").strip() == "void":
                message = (
                    f"Service contract cannot use the primary response assertion because it returns void: "
                    f"{interface.fqcn}.{signature}"
                )
                contracts.append(ServiceMethodContract(
                    interface_type=root_ref,
                    declaring_interface=interface.fqcn,
                    method_name=method.name,
                    resolved_parameter_types=parameter_types,
                    normalized_signature=signature,
                    resolution_status=ResolutionStatus.PARTIAL,
                    diagnostics=(message,),
                ))
                diagnostics.append(message)
                continue

            candidates = [item for candidate_signature, item in available_signatures if candidate_signature == signature]
            if len(candidates) != 1:
                status = ResolutionStatus.AMBIGUOUS if len(candidates) > 1 else ResolutionStatus.UNRESOLVED
                message = (
                    f"Service contract {'ambiguous' if candidates else 'has no matching public concrete method'}: "
                    f"{interface.fqcn}.{signature}"
                )
                contracts.append(ServiceMethodContract(
                    interface_type=root_ref,
                    declaring_interface=interface.fqcn,
                    method_name=method.name,
                    resolved_parameter_types=parameter_types,
                    normalized_signature=signature,
                    resolution_status=status,
                    diagnostics=(message,),
                ))
                diagnostics.append(message)
                continue

            selected = candidates[0]
            method_id = f"{selected.method.name}({','.join(t for t, _ in selected.method.params)})"
            if selected.inherited:
                message = (
                    f"Service contract resolves to inherited concrete method {selected.owner.fqcn}.{method_id}; "
                    "the bounded v7 ServiceImpl analyzer generates primary tests only for methods declared in the selected source"
                )
                contracts.append(ServiceMethodContract(
                    interface_type=root_ref,
                    declaring_interface=interface.fqcn,
                    method_name=method.name,
                    resolved_parameter_types=parameter_types,
                    normalized_signature=signature,
                    implementation_method_id=method_id,
                    implementation_owner=selected.owner.fqcn,
                    resolution_status=ResolutionStatus.PARTIAL,
                    diagnostics=(message,),
                ))
                diagnostics.append(message)
                continue

            contracts.append(ServiceMethodContract(
                interface_type=root_ref,
                declaring_interface=interface.fqcn,
                method_name=method.name,
                resolved_parameter_types=parameter_types,
                normalized_signature=signature,
                implementation_method_id=method_id,
                implementation_owner=selected.owner.fqcn,
                resolution_status=ResolutionStatus.RESOLVED,
            ))
            matched_method_ids.add(method_id)

        parents = interface.extends_types or ([interface.extends] if interface.extends else [])
        for parent_ref in parents:
            queue.append((parent_ref, interface, substitutions, root_ref))

    ordered_matches = tuple(
        method for method in (symbol.methods or ())
        if f"{method.name}({','.join(t for t, _ in method.params)})" in matched_method_ids
    )
    if not ordered_matches:
        diagnostics.append(f"no resolved source-declared Service entry methods for {symbol.fqcn}")
    return ServiceEntryResolution(
        contracts=tuple(contracts),
        matched_methods=ordered_matches,
        diagnostics=tuple(dict.fromkeys(diagnostics)),
    )


def contract_for_method(
    contracts: tuple[ServiceMethodContract, ...],
    method_id: str,
) -> ServiceMethodContract | None:
    return next(
        (
            contract for contract in contracts
            if contract.implementation_method_id == method_id
            and contract.resolution_status == ResolutionStatus.RESOLVED
        ),
        None,
    )
