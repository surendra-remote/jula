"""Deterministic class-level schema discovery for structured test inputs.

The engine, not the LLM, resolves repository payload types and recursively
expands their declared fields. The same catalog supports Controller, ServiceImpl,
validator, utility, mapper, and generic structured-unit targets. The default is
five domain-object levels; collection/wrapper nodes do not consume a level.
Method-specific projection occurs after the complete class catalog is built.
"""
from __future__ import annotations

import json
import os
import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from time import perf_counter

from junitforge.execution.dependencies import simple_type_name
from junitforge.execution.models import (
    CollectionRequirement,
    ConstructionKind,
    MapEntrySchema,
    MapSchema,
    PayloadProperty,
    PayloadSchema,
    PropertyKind,
    ResolutionStatus,
)
from junitforge.models import ClassSymbol, FieldSig
from junitforge.timing import event as timing_event

LookupFn = Callable[..., ClassSymbol | None]

# Domain-object levels, root == level 1. Wrappers/collections do not consume a level.
MAX_PAYLOAD_DEPTH = int(os.getenv("JUNITFORGE_SCHEMA_MAX_OBJECT_LEVELS", os.getenv("JUNITFORGE_PAYLOAD_MAX_DEPTH", "5")))
MAX_PAYLOAD_TYPES_PER_METHOD = int(os.getenv("JUNITFORGE_PAYLOAD_MAX_TYPES", "80"))

_PRIMITIVES = {"boolean", "byte", "short", "int", "long", "float", "double", "char"}
_SCALARS = {
    "Boolean", "Byte", "Short", "Integer", "Long", "Float", "Double", "Character",
    "String", "Object", "BigDecimal", "BigInteger", "UUID", "URI", "URL",
}
_TEMPORALS = {
    "Date", "LocalDate", "LocalDateTime", "ZonedDateTime", "OffsetDateTime",
    "Instant", "Duration", "Period", "LocalTime", "OffsetTime",
}
_COLLECTIONS = {"List", "ArrayList", "LinkedList", "Set", "HashSet", "LinkedHashSet", "Collection", "Iterable"}
_MAPS = {"Map", "HashMap", "LinkedHashMap", "SortedMap", "TreeMap", "ConcurrentMap"}
_OPTIONALS = {"Optional", "OptionalInt", "OptionalLong", "OptionalDouble"}
_PAGES = {"Page", "Slice"}
_RESPONSE_WRAPPERS = {"ResponseEntity"}
_OTHER_WRAPPERS = {"CompletableFuture", "Mono", "Flux", "AtomicReference"}
_INFRASTRUCTURE = {
    "void", "Class", "Throwable", "Exception", "RuntimeException", "HttpHeaders", "HttpStatus",
    "ServletRequest", "ServletResponse", "HttpServletRequest", "HttpServletResponse", "BindingResult",
    "Model", "Principal", "Logger", "ObjectMapper",
}
_SKIP_TYPES = _PRIMITIVES | _SCALARS | _TEMPORALS | _COLLECTIONS | _MAPS | _OPTIONALS | _PAGES | _RESPONSE_WRAPPERS | _OTHER_WRAPPERS | _INFRASTRUCTURE
_TYPE_TOKEN_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")


@dataclass(frozen=True, slots=True)
class TypeShape:
    rendered: str
    raw_type: str
    simple_type: str
    arguments: tuple[str, ...] = ()
    kind: PropertyKind = PropertyKind.UNKNOWN
    element_type: str | None = None
    map_key_type: str | None = None
    map_value_type: str | None = None
    wrapped_type: str | None = None
    array_component_type: str | None = None


def _simple(value: str | None) -> str:
    text = (value or "").strip().replace("...", "")
    return text.rsplit(".", 1)[-1]


def _split_generic_args(rendered: str) -> tuple[str, ...]:
    start = rendered.find("<")
    end = rendered.rfind(">")
    if start < 0 or end <= start:
        return ()
    body = rendered[start + 1:end]
    args: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        if char == "," and depth == 0:
            value = "".join(current).strip()
            if value:
                args.append(value)
            current = []
        else:
            current.append(char)
    value = "".join(current).strip()
    if value:
        args.append(value)
    return tuple(args)


def parse_type_shape(rendered: str | None) -> TypeShape:
    text = (rendered or "Object").strip()
    is_array = text.endswith("[]") or text.endswith("...")
    array_component = text[:-2].strip() if text.endswith("[]") else (text[:-3].strip() if text.endswith("...") else None)
    base_text = array_component or text
    raw = base_text.split("<", 1)[0].strip()
    simple = _simple(raw)
    args = _split_generic_args(base_text)
    if is_array:
        return TypeShape(text, raw, simple, args, PropertyKind.ARRAY, array_component_type=array_component)
    if simple in _PRIMITIVES:
        return TypeShape(text, raw, simple, args, PropertyKind.PRIMITIVE)
    if simple in _SCALARS:
        return TypeShape(text, raw, simple, args, PropertyKind.SCALAR)
    if simple in _TEMPORALS:
        return TypeShape(text, raw, simple, args, PropertyKind.TEMPORAL)
    if simple in _MAPS:
        return TypeShape(
            text, raw, simple, args, PropertyKind.MAP,
            map_key_type=args[0] if len(args) > 0 else "Object",
            map_value_type=args[1] if len(args) > 1 else "Object",
        )
    if simple in {"List", "ArrayList", "LinkedList"}:
        return TypeShape(text, raw, simple, args, PropertyKind.LIST, element_type=args[0] if args else "Object")
    if simple in {"Set", "HashSet", "LinkedHashSet"}:
        return TypeShape(text, raw, simple, args, PropertyKind.SET, element_type=args[0] if args else "Object")
    if simple in {"Collection", "Iterable"}:
        return TypeShape(text, raw, simple, args, PropertyKind.COLLECTION, element_type=args[0] if args else "Object")
    if simple in _OPTIONALS:
        return TypeShape(text, raw, simple, args, PropertyKind.OPTIONAL, wrapped_type=args[0] if args else None)
    if simple in _PAGES:
        return TypeShape(text, raw, simple, args, PropertyKind.PAGE, element_type=args[0] if args else "Object")
    if simple in _RESPONSE_WRAPPERS:
        return TypeShape(text, raw, simple, args, PropertyKind.RESPONSE_ENTITY, wrapped_type=args[0] if args else None)
    if simple in _OTHER_WRAPPERS:
        return TypeShape(text, raw, simple, args, PropertyKind.WRAPPER, wrapped_type=args[0] if args else None)
    return TypeShape(text, raw, simple, args, PropertyKind.OBJECT)


def extract_application_type_names(rendered: str | None) -> tuple[str, ...]:
    """Return application type candidates from nested generic/array declarations."""
    if not rendered:
        return ()
    out: list[str] = []
    for token in _TYPE_TOKEN_RE.findall(rendered):
        simple = token.rsplit(".", 1)[-1]
        if simple in _SKIP_TYPES or simple in {"extends", "super"}:
            continue
        if simple and token not in out and simple not in {_simple(v) for v in out}:
            out.append(token)
    return tuple(out)


def _lookup(lookup: LookupFn, type_name: str, owner: ClassSymbol | None = None) -> ClassSymbol | None:
    try:
        return lookup(type_name, owner)
    except TypeError:
        return lookup(type_name)


def _annotation_names(sym: ClassSymbol) -> set[str]:
    return {a.split(".")[-1].lower() for a in (sym.annotations or [])}


def _property_method_maps(symbols: Sequence[ClassSymbol]) -> tuple[dict[str, str], dict[str, str]]:
    getters: dict[str, str] = {}
    setters: dict[str, str] = {}
    for sym in symbols:
        for method in sym.methods or []:
            if method.is_static or "private" in method.modifiers:
                continue
            if method.arity == 0 and method.return_type and method.return_type != "void":
                if method.name.startswith("get") and len(method.name) > 3:
                    getters[method.name[3:4].lower() + method.name[4:]] = method.name
                elif method.name.startswith("is") and len(method.name) > 2:
                    getters[method.name[2:3].lower() + method.name[3:]] = method.name
                elif sym.kind == "record":
                    getters[method.name] = method.name
            elif method.arity == 1 and method.name.startswith("set") and len(method.name) > 3:
                setters[method.name[3:4].lower() + method.name[4:]] = method.name
    return getters, setters


def _inheritance_chain(sym: ClassSymbol, lookup: LookupFn) -> tuple[list[ClassSymbol], list[str]]:
    chain: list[ClassSymbol] = []
    diagnostics: list[str] = []
    seen: set[str] = set()
    current = sym
    while current and current.fqcn not in seen:
        seen.add(current.fqcn)
        chain.append(current)
        parent_name = current.extends
        if not parent_name or _simple(parent_name) in {"Object", "Record", "Enum"}:
            break
        parent = _lookup(lookup, parent_name, current)
        if parent is None:
            diagnostics.append(f"superclass unresolved: {current.fqcn} extends {parent_name}")
            break
        current = parent
    chain.reverse()  # base fields first; child overrides by name
    return chain, diagnostics


def _selected_constructor(sym: ClassSymbol, fields: Sequence[FieldSig]) -> tuple[ConstructionKind, tuple[tuple[str, str], ...]]:
    if sym.kind == "enum":
        return ConstructionKind.ENUM, ()
    if sym.kind == "record":
        return ConstructionKind.RECORD, tuple((f.type, f.name) for f in fields if "static" not in f.modifiers)
    if sym.kind in {"interface", "annotation"} or "abstract" in sym.modifiers:
        return ConstructionKind.UNSUPPORTED, ()
    # A non-static inner class requires an enclosing instance.  The emitter does
    # not invent or guess that outer instance; report reduced support instead.
    if sym.enclosing_fqcn and "static" not in sym.modifiers:
        return ConstructionKind.UNSUPPORTED, ()
    annos = _annotation_names(sym)
    has_builder = "builder" in annos or "superbuilder" in annos or any(m.name == "builder" and m.is_static for m in sym.methods or [])
    constructors = [c for c in sym.constructors or [] if "private" not in c.modifiers]
    if any(c.arity == 0 for c in constructors) or not constructors:
        return ConstructionKind.NO_ARGS_SETTERS, ()
    if has_builder:
        return ConstructionKind.BUILDER, ()
    selected = min(constructors, key=lambda c: (c.arity, c.render()))
    return ConstructionKind.CONSTRUCTOR, tuple(selected.params)


def _field_union(chain: Sequence[ClassSymbol]) -> list[tuple[FieldSig, ClassSymbol, bool]]:
    by_name: dict[str, tuple[FieldSig, ClassSymbol, bool]] = {}
    root = chain[-1] if chain else None
    for owner in chain:
        for field in owner.fields or []:
            if "static" in field.modifiers:
                continue
            by_name[field.name] = (field, owner, owner is not root)
    return list(by_name.values())


def _resolve_nested_refs(shape: TypeShape, lookup: LookupFn, owner: ClassSymbol) -> tuple[tuple[str, ...], tuple[str, ...]]:
    refs: list[str] = []
    enum_constants: tuple[str, ...] = ()
    candidates: list[str] = []
    if shape.kind == PropertyKind.OBJECT:
        candidates.append(shape.raw_type)
    elif shape.kind in {PropertyKind.LIST, PropertyKind.SET, PropertyKind.COLLECTION, PropertyKind.PAGE} and shape.element_type:
        candidates.extend(extract_application_type_names(shape.element_type))
    elif shape.kind in {PropertyKind.OPTIONAL, PropertyKind.RESPONSE_ENTITY, PropertyKind.WRAPPER} and shape.wrapped_type:
        candidates.extend(extract_application_type_names(shape.wrapped_type))
    elif shape.kind == PropertyKind.MAP and shape.map_value_type:
        candidates.extend(extract_application_type_names(shape.map_value_type))
    elif shape.kind == PropertyKind.ARRAY and shape.array_component_type:
        candidates.extend(extract_application_type_names(shape.array_component_type))
    for candidate in candidates:
        nested = _lookup(lookup, candidate, owner)
        resolved = nested.fqcn if nested else candidate
        if resolved not in refs:
            refs.append(resolved)
        if nested and nested.kind == "enum":
            enum_constants = tuple(nested.enum_constants or ())
    return tuple(refs), enum_constants


def schema_for_symbol(
    sym: ClassSymbol,
    *,
    lookup: LookupFn,
    depth: int = 1,
    recursive_reference: bool = False,
    required_properties: set[str] | None = None,
    prune_unrequested: bool = False,
) -> PayloadSchema:
    """Build a schema for one resolved application type.

    The default remains the existing complete-schema behavior. ServiceImpl
    primary-path analysis opts into ``prune_unrequested`` so only fields proven
    to be used by a selected path enter its class-wide union.
    """
    chain, diagnostics = _inheritance_chain(sym, lookup)
    fields = _field_union(chain)
    getters, setters = _property_method_maps(chain)
    construction_kind, ctor_params = _selected_constructor(sym, [f for f, _, _ in fields])
    annos = _annotation_names(sym)
    has_builder = construction_kind == ConstructionKind.BUILDER or "builder" in annos or "superbuilder" in annos
    ctor_index = {name: index for index, (_, name) in enumerate(ctor_params)}

    properties: list[PayloadProperty] = []
    for field, owner, inherited in fields:
        if prune_unrequested and field.name not in (required_properties or set()):
            continue
        shape = parse_type_shape(field.type)
        nested_refs, enum_constants = _resolve_nested_refs(shape, lookup, owner)
        direct_nested = nested_refs[0] if len(nested_refs) == 1 else None
        getter = getters.get(field.name)
        setter = setters.get(field.name)
        builder_method = field.name if has_builder else None
        cidx = ctor_index.get(field.name)
        required = bool(required_properties and field.name in required_properties)
        minimum_size = 1 if required and shape.kind in {PropertyKind.LIST, PropertyKind.SET, PropertyKind.COLLECTION, PropertyKind.PAGE, PropertyKind.ARRAY} else 0
        resolved_type = None
        if shape.kind == PropertyKind.OBJECT:
            resolved = _lookup(lookup, shape.raw_type, owner)
            resolved_type = resolved.fqcn if resolved else shape.raw_type
            if resolved and resolved.kind == "enum":
                shape = replace(shape, kind=PropertyKind.ENUM)
                enum_constants = tuple(resolved.enum_constants or ())
        properties.append(PayloadProperty(
            name=field.name,
            type_name=field.type,
            readable=getter is not None or "public" in field.modifiers,
            writable=bool(setter or builder_method or cidx is not None or "public" in field.modifiers or sym.kind == "record"),
            getter=getter,
            setter=setter,
            builder_method=builder_method,
            constructor_index=cidx,
            nested_schema_ref=direct_nested,
            source="declared-field-graph",
            resolved_type=resolved_type,
            kind=shape.kind,
            element_type=shape.element_type,
            map_key_type=shape.map_key_type,
            map_value_type=shape.map_value_type,
            wrapped_type=shape.wrapped_type,
            array_component_type=shape.array_component_type,
            nested_schema_refs=nested_refs,
            declared_in=owner.fqcn,
            inherited=inherited,
            field_public="public" in field.modifiers,
            enum_constants=enum_constants,
            default_initializer=field.initializer,
            baseline_required=required,
            minimum_size=minimum_size,
        ))

    return PayloadSchema(
        type_name=sym.name,
        fqcn=sym.fqcn,
        kind=sym.kind,
        constructors=tuple(c.render() for c in sym.constructors or [] if "private" not in c.modifiers),
        properties=tuple(properties),
        enum_constants=tuple(sym.enum_constants or ()),
        resolution_status=ResolutionStatus.RESOLVED,
        depth=depth,
        recursive_reference=recursive_reference,
        package_name=sym.package_name,
        source_path=sym.source_path,
        type_parameters=tuple(sym.type_parameters or ()),
        superclass=sym.extends,
        interfaces=tuple(sym.implements or ()),
        construction_kind=construction_kind,
        constructor_parameters=ctor_params,
        fixture_method_name=f"valid{re.sub(r'[^A-Za-z0-9_$]', '', sym.name)}",
        diagnostics=tuple(diagnostics),
    )


def _normalize_path(path: str | None) -> tuple[str, ...]:
    if not path:
        return ()
    text = re.sub(r"\.get\(\s*\d+\s*\)", "[*]", path)
    text = re.sub(r"\.get\(\s*\"([^\"]+)\"\s*\)", r'["\1"]', text)
    parts: list[str] = []
    for raw in text.split("."):
        value = raw.strip()
        if not value:
            continue
        if value.endswith("()"):
            value = value[:-2]
        if value in {"stream", "iterator", "findFirst", "orElse", "orElseGet", "next", "get"}:
            continue
        if value.startswith("get") and len(value) > 3 and not value.startswith("get("):
            value = value[3:4].lower() + value[4:]
        elif value.startswith("is") and len(value) > 2:
            value = value[2:3].lower() + value[3:]
        value = value.replace("[*]", "")
        parts.append(value)
    return tuple(parts)


def _merge_requested_paths(path_requests: Iterable[tuple[str, str]]) -> dict[str, set[tuple[str, ...]]]:
    merged: dict[str, set[tuple[str, ...]]] = {}
    for rendered_type, raw_path in path_requests:
        candidates = extract_application_type_names(rendered_type)
        if not candidates:
            continue
        root = candidates[0]
        merged.setdefault(root, set()).add(_normalize_path(raw_path))
    return merged


def _schema_key(schema: PayloadSchema) -> str:
    return schema.fqcn or schema.type_name


def merge_payload_schemas(schemas: Iterable[PayloadSchema]) -> list[PayloadSchema]:
    """Field-wise union; never first/last/property-count replacement."""
    merged: dict[str, PayloadSchema] = {}
    for schema in schemas:
        key = _schema_key(schema)
        previous = merged.get(key)
        if previous is None:
            merged[key] = schema
            continue
        props: dict[tuple[str, str], PayloadProperty] = {
            (p.name, p.resolved_type or p.type_name): p for p in previous.properties
        }
        for prop in schema.properties:
            pkey = (prop.name, prop.resolved_type or prop.type_name)
            old = props.get(pkey)
            if old is None:
                props[pkey] = prop
            else:
                props[pkey] = replace(
                    old,
                    baseline_required=old.baseline_required or prop.baseline_required,
                    minimum_size=max(old.minimum_size, prop.minimum_size),
                    nested_schema_refs=tuple(dict.fromkeys((*old.nested_schema_refs, *prop.nested_schema_refs))),
                    enum_constants=tuple(dict.fromkeys((*old.enum_constants, *prop.enum_constants))),
                )
        merged[key] = replace(
            previous,
            properties=tuple(props.values()),
            depth=min(previous.depth, schema.depth),
            diagnostics=tuple(dict.fromkeys((*previous.diagnostics, *schema.diagnostics))),
            cycle_references=tuple(dict.fromkeys((*previous.cycle_references, *schema.cycle_references))),
        )
    return list(merged.values())


def build_payload_schemas(
    seed_types: Iterable[str],
    lookup: LookupFn,
    *,
    path_requests: Iterable[tuple[str, str]] = (),
    max_depth: int = MAX_PAYLOAD_DEPTH,
    max_types: int = MAX_PAYLOAD_TYPES_PER_METHOD,
    schema_cache: dict[object, PayloadSchema] | None = None,
    trace_label: str = "",
    prune_unrequested: bool = False,
) -> tuple[list[PayloadSchema], list[str]]:
    """Build a full, cycle-safe recursive catalog for the supplied roots."""
    started = perf_counter()
    requested = _merge_requested_paths(path_requests)
    roots: list[str] = []
    for rendered in seed_types:
        for candidate in extract_application_type_names(rendered):
            if candidate not in roots:
                roots.append(candidate)
    for root in requested:
        if root not in roots:
            roots.append(root)

    def requested_for(rendered: str) -> set[tuple[str, ...]]:
        return set(
            requested.get(rendered)
            or requested.get(_simple(rendered))
            or set()
        )

    queue: deque[tuple[str, int, tuple[str, ...], ClassSymbol | None, set[tuple[str, ...]]]] = deque(
        (root, 1, (), None, requested_for(root)) for root in roots
    )
    schemas: list[PayloadSchema] = []
    diagnostics: list[str] = []
    visited_states: set[tuple[str, tuple[tuple[str, ...], ...], int]] = set()
    cache = schema_cache if schema_cache is not None else {}

    timing_event("payload.build.start", method=trace_label, roots=len(roots), max_depth=max_depth, max_types=max_types)
    while queue and len(visited_states) < max_types:
        rendered, level, ancestry, owner, queued_paths = queue.popleft()
        sym = _lookup(lookup, rendered, owner)
        if sym is None:
            simple = _simple(rendered)
            diagnostics.append(f"payload type unresolved or ambiguous: {rendered}")
            schemas.append(PayloadSchema(type_name=simple, fqcn=None, kind="unknown", resolution_status=ResolutionStatus.UNRESOLVED, depth=level))
            continue
        key = sym.fqcn or sym.name
        if key in ancestry:
            diagnostics.append(f"payload recursion stopped: {' -> '.join((*ancestry, key))}")
            continue
        root_paths = set(queued_paths)
        root_paths.update(requested.get(rendered) or requested.get(sym.name) or requested.get(sym.fqcn) or set())
        state = (key, tuple(sorted(root_paths)), level)
        if state in visited_states:
            continue
        visited_states.add(state)
        required_props = {p[0] for p in root_paths if p}
        cache_key = (key, tuple(sorted(required_props)), level, prune_unrequested)
        schema = cache.get(cache_key)
        if schema is None:
            schema = schema_for_symbol(
                sym,
                lookup=lookup,
                depth=level,
                required_properties=required_props,
                prune_unrequested=prune_unrequested,
            )
            cache[cache_key] = schema
        schemas.append(schema)

        child_ancestry = (*ancestry, key)
        if (
            not prune_unrequested
            and schema.superclass
            and _simple(schema.superclass) not in {"Object", "Record", "Enum"}
        ):
            queue.append((schema.superclass, level, child_ancestry, sym, set()))
        for prop in schema.properties:
            child_paths = {
                path[1:]
                for path in root_paths
                if path and path[0] == prop.name and len(path) > 1
            }
            for child_ref in prop.nested_schema_refs:
                child_sym = _lookup(lookup, child_ref, sym)
                child_key = (child_sym.fqcn if child_sym else child_ref)
                if child_key in child_ancestry:
                    diagnostics.append(f"payload cycle reference: {key}.{prop.name} -> {child_key}")
                    continue
                if level >= max_depth:
                    # Explicitly requested dereferences are allowed to exceed the default depth.
                    explicitly_required = prop.baseline_required or any(path and path[0] == prop.name and len(path) > 1 for path in root_paths)
                    if not explicitly_required:
                        diagnostics.append(f"payload depth limit reached: {child_key} at object level {level + 1}")
                        continue
                queue.append((child_ref, level + 1, child_ancestry, sym, child_paths))

    if queue:
        diagnostics.append(f"payload schema limit reached: max_types={max_types}")
    result = merge_payload_schemas(schemas)
    timing_event(
        "payload.build.end", elapsed=perf_counter() - started, method=trace_label,
        schemas=len(result), diagnostics=len(diagnostics), remaining_queue=len(queue),
    )
    return result, list(dict.fromkeys(diagnostics))


def schema_index(schemas: Iterable[PayloadSchema]) -> dict[str, PayloadSchema]:
    index: dict[str, PayloadSchema] = {}
    for schema in schemas:
        index[schema.type_name] = schema
        if schema.fqcn:
            index[schema.fqcn] = schema
    return index


def project_schema_ids(seed_types: Iterable[str], schemas: Iterable[PayloadSchema]) -> tuple[str, ...]:
    """Return the recursive schema closure required by one method catalog view."""
    index = schema_index(schemas)
    queue = deque(extract_application_type_names(t) for t in seed_types)
    flat: deque[str] = deque()
    for group in queue:
        flat.extend(group)
    out: list[str] = []
    while flat:
        name = flat.popleft()
        schema = index.get(name) or index.get(_simple(name))
        if not schema:
            continue
        sid = _schema_key(schema)
        if sid in out:
            continue
        out.append(sid)
        for prop in schema.properties:
            flat.extend(prop.nested_schema_refs)
    return tuple(out)


def project_root_schema_ids(seed_types: Iterable[str], schemas: Iterable[PayloadSchema]) -> tuple[str, ...]:
    """Return only directly required fixture roots, without expanding dependencies.

    The compiled skeleton already contains the dependency fixture graph.  Method
    prompts should name only fixtures the test itself must call: public method
    inputs and collaborator return payloads.  Expanding every field of a CUT
    return/local DTO caused one ServiceImpl request to include almost the entire
    48-fixture class catalog.
    """
    index = schema_index(schemas)
    out: list[str] = []
    for rendered in seed_types:
        for name in extract_application_type_names(rendered):
            schema = index.get(name) or index.get(_simple(name))
            if not schema:
                continue
            sid = _schema_key(schema)
            if sid not in out:
                out.append(sid)
    return tuple(out)


def getter_property_name(method_name: str) -> str | None:
    if method_name.startswith("get") and len(method_name) > 3:
        return method_name[3:4].lower() + method_name[4:]
    if method_name.startswith("is") and len(method_name) > 2:
        return method_name[2:3].lower() + method_name[3:]
    return None


def required_object_paths(
    reads: Iterable[tuple[str, str]],
    parameter_types: Mapping[str, str],
    schemas: Iterable[PayloadSchema],
) -> tuple[str, ...]:
    by_name = schema_index(schemas)
    out: list[str] = []
    for variable, raw_path in reads:
        root_type = simple_type_name(parameter_types.get(variable))
        if not root_type:
            continue
        current_type = root_type
        rendered_parts = [variable]
        valid = True
        for prop_name in _normalize_path(raw_path):
            schema = by_name.get(current_type)
            prop = next((p for p in (schema.properties if schema else ()) if p.name == prop_name), None)
            if prop is None:
                valid = False
                break
            rendered_parts.append(prop.name)
            if prop.nested_schema_refs:
                current_type = prop.nested_schema_refs[0]
        if valid and len(rendered_parts) > 1:
            path = ".".join(rendered_parts)
            if path not in out:
                out.append(path)
    return tuple(out)


def extract_collection_requirements(method_source: str) -> tuple[CollectionRequirement, ...]:
    requirements: dict[str, CollectionRequirement] = {}
    source = method_source or ""

    def update(
        path: str,
        *,
        minimum_size: int = 0,
        index: int | None = None,
        wrapper_present: bool = False,
        constraint: str | None = None,
    ) -> None:
        normalized = ".".join(_normalize_path(path))
        if not normalized:
            return
        old = requirements.get(normalized, CollectionRequirement(path=normalized))
        indexes = old.accessed_indexes
        if index is not None:
            indexes = tuple(sorted(set((*indexes, index))))
            minimum_size = max(minimum_size, index + 1)
        constraints = old.matching_constraints
        if constraint:
            compact = " ".join(constraint.split())
            if compact not in constraints:
                constraints = (*constraints, compact)
        requirements[normalized] = replace(
            old,
            minimum_size=max(old.minimum_size, minimum_size),
            accessed_indexes=indexes,
            matching_constraints=constraints,
            wrapper_present=old.wrapper_present or wrapper_present,
        )

    path_pattern = r"[A-Za-z_$][\w$]*(?:\.(?:get|is)[A-Za-z_$][\w$]*\(\))*"
    for match in re.finditer(r"(?P<path>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*\(\))*)\.get\(\s*(?P<index>\d+)\s*\)", method_source or ""):
        update(match.group("path"), index=int(match.group("index")))
    for match in re.finditer(rf"(?P<path>{path_pattern})\.stream\(\)", source):
        tail = source[match.end(): min(len(source), match.end() + 800)]
        boundary = re.split(r"[;\n]", tail, maxsplit=1)[0]
        filter_part = re.search(r"\.filter\s*\((?P<constraint>.*)", boundary)
        update(
            match.group("path"),
            minimum_size=1,
            constraint=filter_part.group("constraint") if filter_part else None,
        )
    for match in re.finditer(rf"(?P<path>{path_pattern})\.(?:iterator|forEach)\s*\(", source):
        update(match.group("path"), minimum_size=1)
    for match in re.finditer(rf"for\s*\([^:;]+:\s*(?P<path>{path_pattern})\s*\)", source):
        update(match.group("path"), minimum_size=1)
    for match in re.finditer(rf"(?P<path>{path_pattern})\s*\[\s*(?P<index>\d+)\s*\]", source):
        update(match.group("path"), index=int(match.group("index")))
    for match in re.finditer(r"(?P<path>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*\(\))*)\.get\(\)", method_source or ""):
        update(match.group("path"), wrapper_present=True)
    return tuple(requirements.values())



def apply_usage_constraints(
    schemas: Iterable[PayloadSchema],
    method_sources: Iterable[str],
    collection_requirements: Iterable[CollectionRequirement] = (),
) -> tuple[PayloadSchema, ...]:
    """Overlay source-derived baseline values/cardinality on the full catalog.

    Schema structure remains repository-derived.  This pass only applies facts
    that are explicit in reachable source: literal equality checks, date parser
    formats, and indexed/stream collection access.  Ambiguous matches are
    intentionally applied conservatively by property name and remain visible in
    reports through the stored baseline/minimum-size metadata.
    """
    sources = tuple(source or "" for source in method_sources)
    minimum_by_property: dict[str, int] = {}
    for requirement in collection_requirements:
        parts = [part for part in requirement.path.split(".") if part and part not in {"value", "*"}]
        if parts:
            name = parts[-1].replace("[*]", "")
            minimum_by_property[name] = max(minimum_by_property.get(name, 0), requirement.minimum_size)

    literal_by_property: dict[str, str] = {}
    format_by_property: dict[str, str] = {}
    java_string = r'"(?P<literal>(?:\\.|[^"\\])*)"'
    getter = r'(?:[A-Za-z_$][\w$]*(?:\(\))?\.)*(?:get|is)(?P<field>[A-Z][\w$]*)\(\)'
    for source in sources:
        for match in re.finditer(java_string + r'\.equals\(\s*' + getter + r'\s*\)', source):
            field = match.group("field")
            name = field[:1].lower() + field[1:]
            literal_by_property.setdefault(name, match.group("literal"))
        for match in re.finditer(getter + r'\.equals\(\s*' + java_string + r'\s*\)', source):
            field = match.group("field")
            name = field[:1].lower() + field[1:]
            literal_by_property.setdefault(name, match.group("literal"))
        for match in re.finditer(r'LocalDate\.parse\([^;\n]*?(?:get|is)(?P<field>[A-Z][\w$]*)\(\)', source):
            field = match.group("field")
            format_by_property[field[:1].lower() + field[1:]] = "ISO_LOCAL_DATE"
        for match in re.finditer(
            r'new\s+SimpleDateFormat\(\s*"(?P<format>[^"]+)"\s*\)[^;\n]*?parse\([^;\n]*?(?:get|is)(?P<field>[A-Z][\w$]*)\(\)',
            source,
        ):
            field = match.group("field")
            format_by_property[field[:1].lower() + field[1:]] = match.group("format")

    def quoted(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    updated: list[PayloadSchema] = []
    for schema in schemas:
        properties: list[PayloadProperty] = []
        for prop in schema.properties:
            baseline = prop.baseline_value
            if baseline is None and prop.kind == PropertyKind.SCALAR and _simple(prop.type_name) == "String":
                if prop.name in literal_by_property:
                    baseline = quoted(literal_by_property[prop.name])
                else:
                    fmt = format_by_property.get(prop.name)
                    if fmt == "ISO_LOCAL_DATE" or (fmt and "yyyy-MM-dd" in fmt):
                        baseline = '"2020-01-01"'
                    elif fmt and "dd/MM/yyyy" in fmt:
                        baseline = '"01/01/1990"'
            properties.append(replace(
                prop,
                baseline_value=baseline,
                minimum_size=max(prop.minimum_size, minimum_by_property.get(prop.name, 0)),
            ))
        updated.append(replace(schema, properties=tuple(properties)))
    return tuple(updated)


def apply_selected_string_branch_constraints(
    schemas: Iterable[PayloadSchema],
    branches: Iterable[object],
    selected_arms: Mapping[str, str],
) -> tuple[tuple[PayloadSchema, ...], tuple[str, ...]]:
    """Overlay bounded String witnesses that satisfy selected equality arms.

    ``apply_usage_constraints`` can recover a literal from ``equals`` but does
    not know whether the chosen path needs that predicate to be true or false.
    This ServiceImpl-only overlay uses the already selected branch arm, supports
    nested getter chains and unary negation, and chooses a small non-equal
    witness when the successful path must reject the compared literal.
    """
    getter = r'(?:[A-Za-z_$][\w$]*(?:\(\))?\.)*(?:get|is)(?P<field>[A-Z][\w$]*)\(\)'
    literal = r'"(?P<literal>(?:\\.|[^"\\])*)"'
    left_literal = re.compile(literal + r'\s*\.\s*equals\(\s*' + getter + r'\s*\)')
    left_getter = re.compile(getter + r'\s*\.\s*equals\(\s*' + literal + r'\s*\)')
    required_equal: dict[str, str] = {}
    forbidden: dict[str, set[str]] = {}
    diagnostics: list[str] = []

    for branch in branches:
        branch_id = str(getattr(branch, "branch_id", "") or "")
        arm = selected_arms.get(branch_id)
        if arm not in {"true", "false"}:
            continue
        condition = str(getattr(branch, "condition", "") or "")
        match = left_literal.search(condition) or left_getter.search(condition)
        if match is None:
            continue
        field = match.group("field")
        property_name = field[:1].lower() + field[1:]
        raw_literal = match.group("literal")
        try:
            compared = json.loads(f'"{raw_literal}"')
        except (TypeError, ValueError):
            compared = raw_literal.replace("\\'", "'")
        prefix = condition[:match.start()]
        negated = bool(re.search(r"!\s*\(?\s*$", prefix))
        condition_true = arm == "true"
        equality_true = not condition_true if negated else condition_true
        if equality_true:
            old = required_equal.get(property_name)
            if old is not None and old != compared:
                diagnostics.append(
                    f"conflicting selected String equalities for {property_name}: {old!r} and {compared!r}"
                )
            else:
                required_equal[property_name] = compared
        else:
            forbidden.setdefault(property_name, set()).add(compared)

    selected_values: dict[str, str] = {}
    for property_name in set(required_equal) | set(forbidden):
        equal_value = required_equal.get(property_name)
        excluded = forbidden.get(property_name, set())
        if equal_value is not None and equal_value not in excluded:
            selected_values[property_name] = equal_value
            continue
        if equal_value is not None:
            diagnostics.append(
                f"selected String constraints conflict for {property_name}; exact feasibility not proven"
            )
        witness = next(value for value in ("A", "B", "C") if value not in excluded)
        selected_values[property_name] = witness
        diagnostics.append(
            f"bounded non-equal String witness {witness!r} selected for {property_name}"
        )

    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    updated: list[PayloadSchema] = []
    for schema in schemas:
        properties = tuple(
            replace(prop, baseline_value=quoted(selected_values[prop.name]))
            if prop.name in selected_values
            and prop.kind == PropertyKind.SCALAR
            and _simple(prop.type_name) == "String"
            else prop
            for prop in schema.properties
        )
        updated.append(replace(schema, properties=properties))
    return tuple(updated), tuple(dict.fromkeys(diagnostics))


def _infer_map_value_type(source: str, variable: str, key: str, local_types: Mapping[str, str]) -> tuple[str | None, PropertyKind]:
    escaped = re.escape(variable) + r"\.get\(\s*\"" + re.escape(key) + r"\"\s*\)"
    cast = re.search(r"\((?P<type>[^()]+)\)\s*" + escaped, source)
    if cast:
        rendered = cast.group("type").strip()
        return rendered, parse_type_shape(rendered).kind
    # Assignment target type is a strong fact.
    assign = re.search(r"(?P<type>[A-Za-z_$][\w$<>?,. \[\]]+)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:\([^;]+\)\s*)?" + escaped, source)
    if assign:
        rendered = assign.group("type").strip()
        return rendered, parse_type_shape(rendered).kind
    if re.search(escaped + r"\s*\.toString\(\)", source):
        return "String", PropertyKind.SCALAR
    if re.search(r"Boolean\.TRUE\.equals\(\s*" + escaped, source):
        return "Boolean", PropertyKind.SCALAR
    return None, PropertyKind.UNKNOWN


def extract_map_schemas(
    method_id: str,
    method_source: str,
    local_variables: Iterable[object] = (),
) -> tuple[MapSchema, ...]:
    """Extract literal-key dynamic map shapes from one verified method source.

    In addition to direct ``map.get("key")`` reads, this links collection
    aliases such as ``addressList = personMap.get("giAddresses")`` followed by
    ``addressList.forEach(a -> { Map add = (Map) a; ... })``.  The link lets the
    deterministic fixture emitter populate a list with a deep child-map fixture
    instead of an empty list.
    """
    source = method_source or ""
    locals_list = tuple(local_variables)

    # Duplicate Java local names can occur in distinct lambda/block scopes. Keep
    # the most structurally informative declaration instead of last-write-wins
    # (e.g. Map traveller versus later int traveller).
    local_types: dict[str, str] = {}
    initializers: dict[str, list[str]] = {}
    for value in locals_list:
        name = getattr(value, "name", "")
        if not name:
            continue
        rendered = getattr(value, "declared_type", None) or ""
        old = local_types.get(name, "")
        old_kind = parse_type_shape(old).kind if old else PropertyKind.UNKNOWN
        new_kind = parse_type_shape(rendered).kind if rendered else PropertyKind.UNKNOWN
        structural = {PropertyKind.MAP, PropertyKind.LIST, PropertyKind.SET, PropertyKind.COLLECTION}
        if not old or (new_kind in structural and old_kind not in structural):
            local_types[name] = rendered
        initializer = getattr(value, "initializer", None)
        if initializer:
            initializers.setdefault(name, []).append(str(initializer))

    map_vars = {
        name for name, rendered in local_types.items()
        if parse_type_shape(rendered).kind == PropertyKind.MAP
    }
    map_vars.update(m.group("var") for m in re.finditer(r'(?P<var>[A-Za-z_$][\w$]*)\.get\(\s*"[^"]+"\s*\)', source))

    # Track every local map/list extracted from a parent map literal key.
    parent_links: dict[str, tuple[str, str, PropertyKind]] = {}
    collection_parent_links: dict[str, tuple[str, str, PropertyKind]] = {}
    for child, values in initializers.items():
        for initializer in values:
            match = re.search(r'(?P<parent>[A-Za-z_$][\w$]*)\.get\(\s*"(?P<key>[^"]+)"\s*\)', initializer)
            if not match:
                continue
            kind = parse_type_shape(local_types.get(child)).kind
            link = (match.group("parent"), match.group("key"), kind)
            if kind == PropertyKind.MAP:
                parent_links[child] = link
                map_vars.add(child)
            elif kind in {PropertyKind.LIST, PropertyKind.SET, PropertyKind.COLLECTION}:
                collection_parent_links[child] = link
            break

    # Link collection elements to the map variable created from the lambda item.
    collection_element_maps: dict[str, str] = {}
    for collection_name in collection_parent_links:
        pattern = re.compile(
            rf'\b{re.escape(collection_name)}\s*\.\s*forEach\s*\(\s*'
            rf'(?P<lambda>[A-Za-z_$][\w$]*)\s*->\s*\{{(?P<body>.*?)\}}\s*\)',
            re.DOTALL,
        )
        for match in pattern.finditer(source):
            lambda_name = match.group("lambda")
            body = match.group("body")
            cast = re.search(
                rf'\bMap(?:\s*<[^;=]+>)?\s+(?P<child>[A-Za-z_$][\w$]*)\s*=\s*'
                rf'\(\s*Map(?:\s*<[^)]+>)?\s*\)\s*{re.escape(lambda_name)}\b',
                body,
            )
            if cast:
                child = cast.group("child")
                collection_element_maps[collection_name] = child
                map_vars.add(child)
                local_types.setdefault(child, "Map<?, ?>")
                break

    if not map_vars:
        return ()

    ids = {var: f"map:{var}" for var in sorted(map_vars)}
    entries_by_var: dict[str, dict[str, MapEntrySchema]] = {var: {} for var in map_vars}
    for var in map_vars:
        pattern = re.compile(re.escape(var) + r'\.get\(\s*"(?P<key>[^"]+)"\s*\)')
        for match in pattern.finditer(source):
            key = match.group("key")
            runtime_type, kind = _infer_map_value_type(source, var, key, local_types)
            entries_by_var[var][key] = MapEntrySchema(
                key_literal=key,
                runtime_type=runtime_type,
                kind=kind,
                methods=(method_id,),
            )

    # Connect nested map variables to their parent key.
    for child, (parent, key, child_kind) in parent_links.items():
        entries_by_var.setdefault(parent, {})
        ids.setdefault(parent, f"map:{parent}")
        old = entries_by_var[parent].get(key, MapEntrySchema(key_literal=key, methods=(method_id,)))
        entries_by_var[parent][key] = replace(
            old,
            kind=PropertyKind.MAP,
            nested_schema_id=ids[child],
            runtime_type=local_types.get(child) or "Map<?, ?>",
        )

    # Connect list/set map entries to a concrete child-map fixture.
    for collection_name, (parent, key, collection_kind) in collection_parent_links.items():
        entries_by_var.setdefault(parent, {})
        ids.setdefault(parent, f"map:{parent}")
        child = collection_element_maps.get(collection_name)
        old = entries_by_var[parent].get(key, MapEntrySchema(key_literal=key, methods=(method_id,)))
        entries_by_var[parent][key] = replace(
            old,
            kind=collection_kind,
            runtime_type=local_types.get(collection_name) or "List<?> ",
            collection_element_schema_id=ids.get(child) if child else old.collection_element_schema_id,
        )

    result: list[MapSchema] = []
    for var in sorted(entries_by_var):
        parent = parent_links.get(var)
        result.append(MapSchema(
            schema_id=ids[var],
            semantic_name=f"{var[:1].upper() + var[1:]}Map",
            variable_name=var,
            method_ids=(method_id,),
            entries=tuple(entries_by_var[var].values()),
            fixture_method_name=f"valid{var[:1].upper() + var[1:]}Map",
            parent_schema_id=ids.get(parent[0]) if parent else None,
            parent_key=parent[1] if parent else None,
            resolution_status=ResolutionStatus.RESOLVED if entries_by_var[var] else ResolutionStatus.PARTIAL,
        ))
    return tuple(result)

def apply_map_usage_constraints(
    schemas: Iterable[MapSchema],
    method_sources: Iterable[str],
) -> list[MapSchema]:
    """Apply source-proven scalar formatting constraints to dynamic map keys."""
    sources = tuple(source or "" for source in method_sources)
    formats: list[str] = []
    for source in sources:
        formats.extend(re.findall(r'new\s+SimpleDateFormat\s*\(\s*"([^"]+)"\s*\)', source))
    date_format = formats[0] if formats else None

    def sample(key: str) -> str | None:
        if not date_format:
            return None
        lowered = key.lower()
        if not any(token in lowered for token in ("dob", "date", "start", "end")):
            return None
        if date_format == "dd/MM/yyyy":
            return '"01/01/1990"' if "dob" in lowered or "birth" in lowered else '"01/01/2020"'
        if date_format in {"yyyy-MM-dd", "yyyy/MM/dd"}:
            separator = "-" if "-" in date_format else "/"
            return f'"1990{separator}01{separator}01"' if "dob" in lowered or "birth" in lowered else f'"2020{separator}01{separator}01"'
        return None

    out: list[MapSchema] = []
    for schema in schemas:
        entries = []
        for entry in schema.entries:
            baseline = entry.baseline_value or sample(entry.key_literal)
            entries.append(replace(entry, baseline_value=baseline))
        out.append(replace(schema, entries=tuple(entries)))
    return out


def merge_map_schemas(schemas: Iterable[MapSchema]) -> list[MapSchema]:
    merged: dict[str, MapSchema] = {}
    for schema in schemas:
        previous = merged.get(schema.schema_id)
        if previous is None:
            merged[schema.schema_id] = schema
            continue
        entries = {entry.key_literal: entry for entry in previous.entries}
        for entry in schema.entries:
            old = entries.get(entry.key_literal)
            if old is None:
                entries[entry.key_literal] = entry
            else:
                entries[entry.key_literal] = replace(
                    old,
                    runtime_type=old.runtime_type or entry.runtime_type,
                    kind=old.kind if old.kind != PropertyKind.UNKNOWN else entry.kind,
                    nested_schema_id=old.nested_schema_id or entry.nested_schema_id,
                    collection_element_schema_id=old.collection_element_schema_id or entry.collection_element_schema_id,
                    methods=tuple(dict.fromkeys((*old.methods, *entry.methods))),
                )
        merged[schema.schema_id] = replace(
            previous,
            method_ids=tuple(dict.fromkeys((*previous.method_ids, *schema.method_ids))),
            entries=tuple(entries.values()),
        )
    return list(merged.values())
