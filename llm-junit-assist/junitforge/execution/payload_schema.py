"""Bounded, projected payload-schema extraction for Controller/ServiceImpl methods.

V15.3 deliberately avoids recursively expanding every field of every type.
Schemas are projected from the property paths actually accessed by the selected
production method. Root types with no known path are included at depth zero but
are not recursively expanded.
"""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from time import perf_counter

from junitforge.execution.dependencies import simple_type_name
from junitforge.execution.models import PayloadProperty, PayloadSchema, ResolutionStatus
from junitforge.models import ClassSymbol
from junitforge.timing import event as timing_event

LookupFn = Callable[[str], ClassSymbol | None]

MAX_PAYLOAD_DEPTH = int(os.getenv("JUNITFORGE_PAYLOAD_MAX_DEPTH", "4"))
MAX_PAYLOAD_TYPES_PER_METHOD = int(os.getenv("JUNITFORGE_PAYLOAD_MAX_TYPES", "25"))
_SKIP_TYPES = {
    "void", "boolean", "byte", "short", "int", "long", "float", "double", "char",
    "Boolean", "Byte", "Short", "Integer", "Long", "Float", "Double", "Character",
    "String", "Object", "BigDecimal", "BigInteger", "UUID", "Date", "LocalDate",
    "LocalDateTime", "ZonedDateTime", "OffsetDateTime", "Instant", "Duration",
    "List", "Set", "Map", "Collection", "Iterable", "Iterator", "Stream",
    "Optional", "Page", "Slice", "ResponseEntity", "HttpHeaders", "HttpStatus",
    "CompletableFuture", "Mono", "Flux", "Class", "Throwable", "Exception",
    "RuntimeException", "ServletRequest", "ServletResponse", "HttpServletRequest",
    "HttpServletResponse", "BindingResult", "Model", "Principal",
}
_TYPE_TOKEN_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")


def extract_application_type_names(rendered: str | None) -> tuple[str, ...]:
    """Extract candidate application type names from generic/array declarations."""
    if not rendered:
        return ()
    out: list[str] = []
    for token in _TYPE_TOKEN_RE.findall(rendered):
        simple = token.rsplit(".", 1)[-1]
        if simple in _SKIP_TYPES or simple in {"extends", "super"}:
            continue
        if simple and simple not in out:
            out.append(simple)
    return tuple(out)


def _annotation_names(sym: ClassSymbol) -> set[str]:
    return {a.split(".")[-1].lower() for a in (sym.annotations or [])}


def _property_method_maps(sym: ClassSymbol) -> tuple[dict[str, str], dict[str, str]]:
    getters: dict[str, str] = {}
    setters: dict[str, str] = {}
    for method in sym.methods or []:
        if method.is_static or "private" in method.modifiers:
            continue
        if method.arity == 0 and method.return_type and method.return_type != "void":
            if method.name.startswith("get") and len(method.name) > 3:
                name = method.name[3:4].lower() + method.name[4:]
                getters[name] = method.name
            elif method.name.startswith("is") and len(method.name) > 2:
                name = method.name[2:3].lower() + method.name[3:]
                getters[name] = method.name
            elif sym.kind == "record":
                getters[method.name] = method.name
        elif method.arity == 1 and method.name.startswith("set") and len(method.name) > 3:
            name = method.name[3:4].lower() + method.name[4:]
            setters[name] = method.name
    return getters, setters


def _constructor_index(sym: ClassSymbol, field_name: str, field_type: str) -> int | None:
    matches: list[int] = []
    for ctor in sym.constructors or []:
        if "private" in ctor.modifiers:
            continue
        for index, (ptype, pname) in enumerate(ctor.params):
            if pname == field_name and simple_type_name(ptype) == simple_type_name(field_type):
                matches.append(index)
    return matches[0] if len(set(matches)) == 1 else None


def schema_for_symbol(
    sym: ClassSymbol,
    *,
    depth: int = 0,
    recursive_reference: bool = False,
    include_properties: set[str] | None = None,
) -> PayloadSchema:
    """Build a schema, optionally retaining only explicitly accessed properties."""
    getters, setters = _property_method_maps(sym)
    annos = _annotation_names(sym)
    has_builder = "builder" in annos or "superbuilder" in annos or any(
        m.name == "builder" and m.is_static for m in (sym.methods or [])
    )

    properties: list[PayloadProperty] = []
    for field in sym.fields or []:
        if "static" in field.modifiers:
            continue
        if include_properties is not None and field.name not in include_properties:
            continue
        constructor_index = _constructor_index(sym, field.name, field.type)
        getter = getters.get(field.name)
        setter = setters.get(field.name)
        builder_method = field.name if has_builder else None
        nested_types = extract_application_type_names(field.type)
        nested_ref = nested_types[0] if len(nested_types) == 1 else None
        properties.append(
            PayloadProperty(
                name=field.name,
                type_name=field.type,
                readable=getter is not None or "public" in field.modifiers,
                writable=(
                    setter is not None
                    or constructor_index is not None
                    or builder_method is not None
                    or "public" in field.modifiers
                    or sym.kind == "record"
                ),
                getter=getter,
                setter=setter,
                builder_method=builder_method,
                constructor_index=constructor_index,
                nested_schema_ref=nested_ref,
                source="accessed-path" if include_properties is not None else "declared-field",
            )
        )

    constructors = tuple(
        ctor.render() for ctor in (sym.constructors or []) if "private" not in ctor.modifiers
    )
    return PayloadSchema(
        type_name=sym.name,
        fqcn=sym.fqcn,
        kind=sym.kind,
        constructors=constructors,
        properties=tuple(properties),
        enum_constants=tuple(sym.enum_constants or ()),
        resolution_status=ResolutionStatus.RESOLVED,
        depth=depth,
        recursive_reference=recursive_reference,
    )


def _normalize_path(path: str | None) -> tuple[str, ...]:
    if not path:
        return ()
    parts: list[str] = []
    for raw in path.split("."):
        value = raw.strip()
        if not value:
            continue
        if value.endswith("()"):
            value = value[:-2]
        if value.startswith("get") and len(value) > 3:
            value = value[3:4].lower() + value[4:]
        elif value.startswith("is") and len(value) > 2:
            value = value[2:3].lower() + value[3:]
        parts.append(value)
    return tuple(parts)


def _merge_requested_paths(
    path_requests: Iterable[tuple[str, str]],
) -> dict[str, set[tuple[str, ...]]]:
    merged: dict[str, set[tuple[str, ...]]] = {}
    for rendered_type, raw_path in path_requests:
        candidates = extract_application_type_names(rendered_type)
        if not candidates:
            continue
        root = candidates[0]
        merged.setdefault(root, set()).add(_normalize_path(raw_path))
    return merged


def build_payload_schemas(
    seed_types: Iterable[str],
    lookup: LookupFn,
    *,
    path_requests: Iterable[tuple[str, str]] = (),
    max_depth: int = MAX_PAYLOAD_DEPTH,
    max_types: int = MAX_PAYLOAD_TYPES_PER_METHOD,
    schema_cache: dict[tuple[str, tuple[tuple[str, ...], ...], int], PayloadSchema] | None = None,
    trace_label: str = "",
) -> tuple[list[PayloadSchema], list[str]]:
    """Resolve only roots and nested properties reached by actual access paths.

    ``path_requests`` contains ``(root_type, property_path)`` pairs. A root with
    an empty path is emitted at depth zero but its nested fields are not walked.
    This prevents enterprise DTO/entity graphs from expanding combinatorially.
    """
    started = perf_counter()
    cache = schema_cache if schema_cache is not None else {}
    requested = _merge_requested_paths(path_requests)

    # Every seed appears at least as a depth-zero schema. Only path requests
    # authorize recursive traversal.
    for rendered in seed_types:
        for simple in extract_application_type_names(rendered):
            requested.setdefault(simple, set()).add(())

    queue: deque[tuple[str, int, tuple[str, ...], set[tuple[str, ...]]]] = deque()
    for root, paths in requested.items():
        queue.append((root, 0, (), set(paths)))

    schemas_by_name: dict[str, PayloadSchema] = {}
    diagnostics: list[str] = []
    expanded_states: set[tuple[str, int, tuple[tuple[str, ...], ...]]] = set()
    lookup_count = 0

    timing_event(
        "payload.build.start",
        method=trace_label,
        roots=len(requested),
        max_depth=max_depth,
        max_types=max_types,
    )

    while queue and len(schemas_by_name) < max_types:
        simple, depth, ancestry, paths = queue.popleft()
        canonical_paths = tuple(sorted(paths))
        state = (simple, depth, canonical_paths)
        if state in expanded_states:
            continue
        expanded_states.add(state)

        recursive = simple in ancestry
        if recursive:
            diagnostics.append(f"payload recursion stopped: {' -> '.join((*ancestry, simple))}")
            continue
        if depth > max_depth:
            diagnostics.append(f"payload depth limit reached: {simple} at depth {depth}")
            continue

        lookup_count += 1
        if lookup_count == 1 or lookup_count % 5 == 0:
            timing_event(
                "payload.build.progress",
                method=trace_label,
                current_type=simple,
                depth=depth,
                schemas=len(schemas_by_name),
                queued=len(queue),
                lookups=lookup_count,
            )

        sym = lookup(simple)
        if sym is None:
            diagnostics.append(f"payload type unresolved: {simple}")
            schemas_by_name.setdefault(
                simple,
                PayloadSchema(
                    type_name=simple,
                    fqcn=None,
                    kind="unknown",
                    resolution_status=ResolutionStatus.UNRESOLVED,
                    depth=depth,
                ),
            )
            continue

        non_empty_paths = {path for path in paths if path}
        include_properties = {path[0] for path in non_empty_paths} if non_empty_paths else None
        cache_key = (sym.fqcn or sym.name, canonical_paths, depth)
        schema = cache.get(cache_key)
        if schema is None:
            schema = schema_for_symbol(
                sym,
                depth=depth,
                recursive_reference=False,
                include_properties=include_properties,
            )
            cache[cache_key] = schema

        previous = schemas_by_name.get(schema.type_name)
        if previous is None or len(schema.properties) > len(previous.properties):
            schemas_by_name[schema.type_name] = schema

        # Empty-path roots are intentionally not expanded.
        if not non_empty_paths or depth >= max_depth:
            continue

        property_by_name = {prop.name: prop for prop in schema.properties}
        child_requests: dict[str, set[tuple[str, ...]]] = {}
        for path in non_empty_paths:
            first, remainder = path[0], path[1:]
            prop = property_by_name.get(first)
            if prop is None:
                diagnostics.append(f"payload property unresolved: {simple}.{first}")
                continue
            nested = extract_application_type_names(prop.type_name)
            if remainder and nested:
                child_requests.setdefault(nested[0], set()).add(remainder)

        next_ancestry = (*ancestry, simple)
        for child_type, child_paths in child_requests.items():
            queue.append((child_type, depth + 1, next_ancestry, child_paths))

    if queue:
        diagnostics.append(f"payload schema limit reached: max_types={max_types}")

    schemas = list(schemas_by_name.values())
    timing_event(
        "payload.build.end",
        elapsed=perf_counter() - started,
        method=trace_label,
        schemas=len(schemas),
        diagnostics=len(diagnostics),
        lookups=lookup_count,
        remaining_queue=len(queue),
    )
    return schemas, list(dict.fromkeys(diagnostics))


def schema_index(schemas: Iterable[PayloadSchema]) -> dict[str, PayloadSchema]:
    return {schema.type_name: schema for schema in schemas}


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
    """Translate getter chains into verified object graph paths where possible."""
    by_name = schema_index(schemas)
    out: list[str] = []
    for variable, raw_path in reads:
        root_type = simple_type_name(parameter_types.get(variable))
        if not root_type:
            continue
        current_type = root_type
        rendered_parts = [variable]
        valid = True
        segments = [segment for segment in raw_path.split(".") if segment]
        for segment in segments:
            method_name = segment[:-2] if segment.endswith("()") else segment
            prop_name = getter_property_name(method_name) or method_name
            schema = by_name.get(current_type)
            prop = next((p for p in (schema.properties if schema else ()) if p.name == prop_name), None)
            if prop is None:
                valid = False
                break
            rendered_parts.append(prop.name)
            nested = extract_application_type_names(prop.type_name)
            if nested:
                current_type = nested[0]
        if valid and len(rendered_parts) > 1:
            path = ".".join(rendered_parts)
            if path not in out:
                out.append(path)
    return tuple(out)
