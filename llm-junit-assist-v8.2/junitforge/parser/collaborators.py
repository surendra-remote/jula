"""Resolve direct collaborators for Mockito unit-test generation.

Only structural dependencies are mocked: constructor parameters and dependency
fields. DTOs/entities/value objects/collections/primitives are intentionally not
mocked.
"""

from __future__ import annotations

import re
from typing import Callable

from junitforge.models import ClassSymbol, Collaborator

_JDK_PREFIXES = ("java.", "javax.", "jakarta.")
_PRIMITIVES_AND_VALUES = {
    "int", "long", "short", "byte", "char", "boolean", "float", "double", "void",
    "String", "Object", "Integer", "Long", "Boolean", "Double", "Float", "Short", "Byte", "Character",
    "BigDecimal", "BigInteger", "LocalDate", "LocalDateTime", "ZonedDateTime", "OffsetDateTime", "Instant", "Date",
    "List", "Map", "Set", "Collection", "Optional", "Stream", "Page", "Slice", "ResponseEntity",
    "HttpHeaders", "HttpStatus", "Logger", "CompletableFuture",
}
_VALUE_OBJECT_SUFFIXES = (
    "Dto", "DTO", "Request", "Response", "Model", "Entity", "Payload", "Command", "Event", "Vo", "VO",
)

LookupFn = Callable[[str], ClassSymbol | None]


def base_type_name(rendered: str) -> str:
    t = (rendered or "").strip()
    t = re.sub(r"<.*>", "", t)
    t = t.replace("[]", "").replace("...", "").strip()
    return t.split(".")[-1]


def _annotation_names(symbol: ClassSymbol) -> set[str]:
    return {a.split(".")[-1] for a in (symbol.annotations or [])}


def _field_has_annotation(field, anno: str) -> bool:
    return any(a.split(".")[-1] == anno for a in (field.annotations or []))


def _should_skip_type(simple: str, sym: ClassSymbol | None) -> bool:
    if not simple or simple in _PRIMITIVES_AND_VALUES:
        return True
    if simple.endswith(_VALUE_OBJECT_SUFFIXES):
        return True
    if sym is None:
        return False
    annos = _annotation_names(sym)
    if annos & {"Entity", "Embeddable", "MappedSuperclass", "Data", "Getter", "Setter", "Value"}:
        return True
    return False


def resolve_collaborators(symbol: ClassSymbol, lookup: LookupFn) -> list[Collaborator]:
    candidates: dict[str, str] = {}

    for ctor in symbol.constructors or []:
        if "private" in ctor.modifiers:
            continue
        for ptype, _ in ctor.params:
            name = base_type_name(ptype)
            candidates.setdefault(name, "ctor-param")

    for fld in symbol.fields or []:
        if "static" in fld.modifiers:
            continue
        if _field_has_annotation(fld, "Value"):
            continue
        name = base_type_name(fld.type)
        candidates.setdefault(name, "field")

    out: list[Collaborator] = []
    seen: set[str] = set()

    for simple, origin in candidates.items():
        if simple in seen or simple == symbol.name:
            continue
        sym = lookup(simple)
        if _should_skip_type(simple, sym):
            continue
        seen.add(simple)

        if sym is None:
            # Unknown external dependency. Still expose it as a collaborator shell so
            # prompts can create @Mock fields when the source clearly depends on it.
            out.append(Collaborator(fqcn=simple, simple=simple, signatures=[], origin=origin))
            continue

        if sym.kind == "interface":
            sigs = list(sym.methods)
        else:
            sigs = [m for m in sym.methods if m.is_public]
        out.append(Collaborator(fqcn=sym.fqcn, simple=sym.name, signatures=sigs, origin=origin))

    return out


def render_collaborator(c: Collaborator, max_methods: int = 40) -> str:
    lines = [f"// import {c.fqcn};", f"class {c.simple} {{"]
    for m in c.signatures[:max_methods]:
        lines.append(f"    {m.render()};")
    if len(c.signatures) > max_methods:
        lines.append(f"    // ... {len(c.signatures) - max_methods} more signatures hidden")
    if not c.signatures:
        lines.append("    // signatures unavailable; only mock interactions visible in source may be used")
    lines.append("}")
    return "\n".join(lines)
