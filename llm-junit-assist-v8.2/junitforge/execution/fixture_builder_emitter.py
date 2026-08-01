"""Deterministic reusable Java fixture emission.

The emitter consumes the parser-built class-level schema catalog. It never
invents fields or accessors and emits only construction mechanisms verified by
that catalog. Unsupported schemas remain visible through diagnostics instead of
silently producing shallow objects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

from junitforge.execution.models import (
    ConstructionKind,
    FixtureSpec,
    MapEntrySchema,
    MapSchema,
    PayloadProperty,
    PayloadSchema,
    PropertyKind,
    ResolutionStatus,
)


def _simple(type_name: str | None) -> str:
    text = (type_name or "Object").strip().replace("...", "")
    return text.rsplit(".", 1)[-1]


def _raw_simple(type_name: str | None) -> str:
    return _simple((type_name or "Object").split("<", 1)[0].replace("[]", ""))


def _cap(value: str) -> str:
    return value[:1].upper() + value[1:] if value else value


def _safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_$]", "", value)
    if not cleaned:
        return "Fixture"
    return cleaned if cleaned[0].isalpha() or cleaned[0] in "_$" else f"T{cleaned}"


@dataclass(slots=True)
class EmittedFixtures:
    helpers: list[str] = field(default_factory=list)
    fixtures: list[FixtureSpec] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    diagnostics: list[str] = field(default_factory=list)
    back_edges: list[tuple[str, str]] = field(default_factory=list)


class FixtureBuilderEmitter:
    def __init__(
        self,
        schemas: Iterable[PayloadSchema],
        map_schemas: Iterable[MapSchema] = (),
        *,
        projection_aware: bool = False,
        strict_source_values: bool = False,
        scenario_schemas: Mapping[str, Iterable[PayloadSchema]] | None = None,
        scenario_map_schemas: Mapping[str, Iterable[MapSchema]] | None = None,
    ):
        self.schemas = list(schemas)
        self.map_schemas = list(map_schemas)
        self.projection_aware = projection_aware
        self.strict_source_values = strict_source_values
        self.scenario_schemas = {
            scenario: {
                schema.fqcn or schema.type_name: schema
                for schema in schemas_for_scenario
            }
            for scenario, schemas_for_scenario in (scenario_schemas or {}).items()
        }
        self.scenario_map_schemas = {
            scenario: {schema.schema_id: schema for schema in schemas_for_scenario}
            for scenario, schemas_for_scenario in (scenario_map_schemas or {}).items()
        }
        self.by_id: dict[str, PayloadSchema] = {}
        self.by_name: dict[str, PayloadSchema] = {}
        for schema in self.schemas:
            sid = schema.fqcn or schema.type_name
            self.by_id[sid] = schema
            self.by_name.setdefault(schema.type_name, schema)
            if schema.fqcn:
                self.by_name.setdefault(schema.fqcn, schema)
        self.map_by_id = {m.schema_id: m for m in self.map_schemas}
        self.imports: set[str] = set()
        self.diagnostics: list[str] = []
        self.back_edges: list[tuple[str, str]] = []
        self._method_name: dict[str, str] = {}
        self._used_names: set[str] = set()
        self._duplicate_simple = {
            name for name in {s.type_name for s in self.schemas}
            if sum(1 for s in self.schemas if s.type_name == name and s.fqcn) > 1
        }

    def emit_all(self) -> EmittedFixtures:
        helpers: list[str] = []
        fixtures: list[FixtureSpec] = []
        emitted: set[str] = set()

        def visit_schema(schema_id: str, stack: tuple[str, ...] = ()) -> None:
            if schema_id in emitted:
                return
            schema = self.by_id.get(schema_id) or self.by_name.get(schema_id)
            if schema is None:
                return
            sid = schema.fqcn or schema.type_name
            if sid in stack:
                return
            for prop in schema.properties:
                for ref in prop.nested_schema_refs:
                    child = self.by_id.get(ref) or self.by_name.get(ref) or self.by_name.get(_simple(ref))
                    if child and child.kind != "enum":
                        visit_schema(child.fqcn or child.type_name, (*stack, sid))
            spec = self._emit_schema(schema)
            emitted.add(sid)
            fixtures.append(spec)
            if spec.supported and spec.method_source:
                helpers.append(spec.method_source)

        for schema in sorted(self.schemas, key=lambda s: (s.depth, s.fqcn or s.type_name), reverse=True):
            if schema.kind != "enum":
                visit_schema(schema.fqcn or schema.type_name)

        emitted_maps: set[str] = set()

        def visit_map(schema_id: str, stack: tuple[str, ...] = ()) -> None:
            if schema_id in emitted_maps or schema_id in stack:
                return
            schema = self.map_by_id.get(schema_id)
            if schema is None:
                return
            for entry in schema.entries:
                if entry.nested_schema_id:
                    visit_map(entry.nested_schema_id, (*stack, schema_id))
            spec = self._emit_map(schema)
            emitted_maps.add(schema_id)
            fixtures.append(spec)
            if spec.supported and spec.method_source:
                helpers.append(spec.method_source)

        for schema in self.map_schemas:
            visit_map(schema.schema_id)

        if self.projection_aware and fixtures:
            support = self._projection_support_fixture()
            fixtures.append(support)
            helpers.append(support.method_source)

        return EmittedFixtures(
            helpers=helpers,
            fixtures=fixtures,
            imports=set(self.imports),
            diagnostics=list(dict.fromkeys(self.diagnostics)),
            back_edges=list(self.back_edges),
        )

    def _schema_method_name(self, schema: PayloadSchema) -> str:
        sid = schema.fqcn or schema.type_name
        if sid in self._method_name:
            return self._method_name[sid]
        base = schema.fixture_method_name or f"valid{_safe_identifier(schema.type_name)}"
        if schema.type_name in self._duplicate_simple and schema.package_name:
            base += _safe_identifier(schema.package_name.rsplit(".", 1)[-1])
        name = base
        suffix = 2
        while name in self._used_names:
            name = f"{base}{suffix}"
            suffix += 1
        self._used_names.add(name)
        self._method_name[sid] = name
        return name

    def _map_method_name(self, schema: MapSchema) -> str:
        key = schema.schema_id
        if key in self._method_name:
            return self._method_name[key]
        base = schema.fixture_method_name or f"valid{_safe_identifier(schema.semantic_name)}"
        name = base
        suffix = 2
        while name in self._used_names:
            name = f"{base}{suffix}"
            suffix += 1
        self._used_names.add(name)
        self._method_name[key] = name
        return name

    def _type_ref(self, schema: PayloadSchema) -> str:
        if schema.type_name in self._duplicate_simple and schema.fqcn:
            return schema.fqcn
        return schema.type_name

    def _emit_schema(self, schema: PayloadSchema) -> FixtureSpec:
        sid = schema.fqcn or schema.type_name
        method_name = self._schema_method_name(schema)
        type_ref = self._type_ref(schema)
        diagnostics = list(schema.diagnostics)
        if schema.resolution_status != ResolutionStatus.RESOLVED:
            diagnostics.append("schema unresolved")
        if schema.kind == "enum" or schema.construction_kind == ConstructionKind.ENUM:
            return FixtureSpec(sid, method_name, type_ref, sid, supported=False, diagnostics=("enum schemas use constants, not fixture methods",))
        if schema.construction_kind == ConstructionKind.UNSUPPORTED:
            diagnostics.append(f"unsupported construction kind for {type_ref}")
            return FixtureSpec(sid, method_name, type_ref, sid, supported=False, diagnostics=tuple(diagnostics))

        dependencies: list[str] = []
        stack = (sid,)
        prop_values: dict[str, str] = {}
        for prop in schema.properties:
            value = self._value_expr(schema, prop, stack)
            if value is None:
                continue
            if self.projection_aware:
                value = self._scenario_property_value(schema, prop, value, stack)
            prop_values[prop.name] = value
            for ref in prop.nested_schema_refs:
                child = self.by_id.get(ref) or self.by_name.get(ref) or self.by_name.get(_simple(ref))
                if child and child.kind != "enum" and child.construction_kind != ConstructionKind.UNSUPPORTED:
                    dep = child.fqcn or child.type_name
                    if dep not in dependencies:
                        dependencies.append(dep)

        var = re.sub(r"[^A-Za-z0-9_$]", "", schema.type_name[:1].lower() + schema.type_name[1:]) or "value"
        parameter = "String fixtureScenario, String... requiredPaths" if self.projection_aware else ""
        lines = [f"    private {type_ref} {method_name}({parameter}) {{"]
        if schema.construction_kind == ConstructionKind.BUILDER:
            lines.append(f"        {type_ref} {var} = {type_ref}.builder()")
            builder_count = 0
            for prop in schema.properties:
                value = prop_values.get(prop.name)
                if value is None or not prop.builder_method:
                    continue
                rendered_value = self._projected_value(prop, value)
                lines.append(f"                .{prop.builder_method}({rendered_value})")
                builder_count += 1
            lines.append("                .build();")
            # Verified setters can fill properties excluded from the builder.
            for prop in schema.properties:
                value = prop_values.get(prop.name)
                if value is not None and prop.setter and not prop.builder_method:
                    if self.projection_aware:
                        lines.extend((
                            f'        if (fixtureRequires(requiredPaths, "{prop.name}")) {{',
                            f"            {var}.{prop.setter}({value});",
                            "        }",
                        ))
                    else:
                        lines.append(f"        {var}.{prop.setter}({value});")
            if builder_count == 0:
                diagnostics.append("builder detected but no verified builder properties")
        elif schema.construction_kind in {ConstructionKind.CONSTRUCTOR, ConstructionKind.RECORD}:
            values: list[str] = []
            for ptype, pname in schema.constructor_parameters:
                prop = next((item for item in schema.properties if item.name == pname), None)
                value = prop_values.get(pname)
                if value is None and (not self.projection_aware or prop is not None):
                    value = self._scalar_literal(schema.type_name, pname, ptype, ())
                if value is None:
                    value = self._default_value(ptype)
                values.append(self._projected_value(prop, value, type_name=ptype) if prop else value)
            lines.append(f"        {type_ref} {var} = new {type_ref}({', '.join(values)});")
            if schema.construction_kind == ConstructionKind.CONSTRUCTOR:
                ctor_names = {name for _, name in schema.constructor_parameters}
                for prop in schema.properties:
                    value = prop_values.get(prop.name)
                    if value is None or prop.name in ctor_names:
                        continue
                    if prop.setter:
                        if self.projection_aware:
                            lines.extend((
                                f'        if (fixtureRequires(requiredPaths, "{prop.name}")) {{',
                                f"            {var}.{prop.setter}({value});",
                                "        }",
                            ))
                        else:
                            lines.append(f"        {var}.{prop.setter}({value});")
                    elif prop.field_public:
                        if self.projection_aware:
                            lines.extend((
                                f'        if (fixtureRequires(requiredPaths, "{prop.name}")) {{',
                                f"            {var}.{prop.name} = {value};",
                                "        }",
                            ))
                        else:
                            lines.append(f"        {var}.{prop.name} = {value};")
        else:
            lines.append(f"        {type_ref} {var} = new {type_ref}();")
            for prop in schema.properties:
                value = prop_values.get(prop.name)
                if value is None:
                    continue
                if prop.setter:
                    if self.projection_aware:
                        lines.extend((
                            f'        if (fixtureRequires(requiredPaths, "{prop.name}")) {{',
                            f"            {var}.{prop.setter}({value});",
                            "        }",
                        ))
                    else:
                        lines.append(f"        {var}.{prop.setter}({value});")
                elif prop.field_public:
                    if self.projection_aware:
                        lines.extend((
                            f'        if (fixtureRequires(requiredPaths, "{prop.name}")) {{',
                            f"            {var}.{prop.name} = {value};",
                            "        }",
                        ))
                    else:
                        lines.append(f"        {var}.{prop.name} = {value};")
                elif prop.constructor_index is None:
                    diagnostics.append(f"unwritable property omitted: {schema.type_name}.{prop.name}")
        lines.extend((f"        return {var};", "    }"))
        source = "\n".join(lines)
        return FixtureSpec(
            fixture_id=sid,
            method_name=method_name,
            return_type=type_ref,
            schema_id=sid,
            dependent_fixture_ids=tuple(dependencies),
            imports=tuple(sorted(self.imports)),
            method_source=source,
            supported=True,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            projection_aware=self.projection_aware,
        )

    def _projected_value(
        self,
        prop: PayloadProperty | None,
        value: str,
        *,
        type_name: str | None = None,
    ) -> str:
        if not self.projection_aware or prop is None:
            return value
        fallback = self._default_value(type_name or prop.type_name)
        return f'fixtureRequires(requiredPaths, "{prop.name}") ? {value} : {fallback}'

    @staticmethod
    def _java_string(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _scenario_expression(self, fallback: str, values: list[tuple[str, str]]) -> str:
        if not values or all(value == fallback for _, value in values):
            return fallback
        expression = fallback
        for scenario, value in reversed(values):
            expression = (
                f"({self._java_string(scenario)}.equals(fixtureScenario) "
                f"? {value} : {expression})"
            )
        return expression

    def _scenario_property_value(
        self,
        owner: PayloadSchema,
        prop: PayloadProperty,
        fallback: str,
        stack: tuple[str, ...],
    ) -> str:
        owner_id = owner.fqcn or owner.type_name
        values: list[tuple[str, str]] = []
        for scenario, schemas in self.scenario_schemas.items():
            scenario_owner = schemas.get(owner_id)
            if scenario_owner is None:
                continue
            scenario_prop = next(
                (
                    candidate for candidate in scenario_owner.properties
                    if candidate.name == prop.name
                    and (candidate.resolved_type or candidate.type_name)
                    == (prop.resolved_type or prop.type_name)
                ),
                None,
            )
            if scenario_prop is None:
                continue
            value = self._value_expr(scenario_owner, scenario_prop, stack)
            if value is not None:
                values.append((scenario, value))
        return self._scenario_expression(fallback, values)

    @staticmethod
    def _default_value(type_name: str) -> str:
        simple = _raw_simple(type_name)
        return {
            "boolean": "false", "byte": "(byte) 0", "short": "(short) 0",
            "int": "0", "long": "0L", "float": "0.0f", "double": "0.0d",
            "char": "'\\0'",
        }.get(simple, "null")

    def _value_expr(self, owner: PayloadSchema, prop: PayloadProperty, stack: tuple[str, ...]) -> str | None:
        refs = prop.nested_schema_refs
        child = None
        if refs:
            child = self.by_id.get(refs[0]) or self.by_name.get(refs[0]) or self.by_name.get(_simple(refs[0]))
        owner_id = owner.fqcn or owner.type_name
        child_id = (child.fqcn or child.type_name) if child else None
        if child and (child_id in stack or self._would_cycle(child_id, owner_id)):
            self.back_edges.append((owner_id, prop.name))
            self.diagnostics.append(f"cycle back-reference left unset: {owner.type_name}.{prop.name}")
            return None

        if prop.baseline_value:
            return prop.baseline_value
        # An explicit Java ``= null`` initializer describes the production
        # default, not a valid test baseline.  When the parser has resolved a
        # constructible nested schema, hydrate it instead of preserving null.
        # Non-null scalar initializers remain useful deterministic values.
        if (
            prop.default_initializer
            and prop.default_initializer.strip() != "null"
            and self._safe_initializer(prop.default_initializer)
        ):
            return prop.default_initializer
        if prop.kind == PropertyKind.ENUM:
            constants = prop.enum_constants or (child.enum_constants if child else ())
            if constants:
                type_ref = self._type_ref(child) if child else _raw_simple(prop.type_name)
                return f"{type_ref}.{constants[0]}"
            self.diagnostics.append(f"enum constant unresolved: {owner.type_name}.{prop.name}")
            return None
        if prop.kind == PropertyKind.OBJECT:
            if child and child.construction_kind != ConstructionKind.UNSUPPORTED and child.kind != "enum":
                args = f'fixtureScenario, fixtureChildPaths(requiredPaths, "{prop.name}")' if self.projection_aware else ""
                return f"{self._schema_method_name(child)}({args})"
            if child and child.kind == "enum" and child.enum_constants:
                return f"{self._type_ref(child)}.{child.enum_constants[0]}"
            return self._scalar_literal(owner.type_name, prop.name, prop.type_name, prop.enum_constants)
        if prop.kind in {PropertyKind.PRIMITIVE, PropertyKind.SCALAR, PropertyKind.TEMPORAL}:
            return self._scalar_literal(owner.type_name, prop.name, prop.type_name, prop.enum_constants)
        if prop.kind in {PropertyKind.LIST, PropertyKind.COLLECTION}:
            self.imports.update({"java.util.ArrayList", "java.util.List"})
            element = self._element_expr(owner, prop, child, stack)
            return "new ArrayList<>()" if element is None else self._collection_expr("ArrayList", element, prop.minimum_size)
        if prop.kind == PropertyKind.SET:
            self.imports.update({"java.util.LinkedHashSet", "java.util.List"})
            element = self._element_expr(owner, prop, child, stack)
            return "new LinkedHashSet<>()" if element is None else self._collection_expr("LinkedHashSet", element, prop.minimum_size)
        if prop.kind == PropertyKind.MAP:
            self.imports.add("java.util.LinkedHashMap")
            return "new LinkedHashMap<>()"
        if prop.kind == PropertyKind.OPTIONAL:
            self.imports.add("java.util.Optional")
            inner = self._wrapped_expr(owner, prop, child, stack)
            return "Optional.empty()" if inner is None else f"Optional.of({inner})"
        if prop.kind == PropertyKind.RESPONSE_ENTITY:
            self.imports.add("org.springframework.http.ResponseEntity")
            inner = self._wrapped_expr(owner, prop, child, stack)
            return "ResponseEntity.ok().build()" if inner is None else f"ResponseEntity.ok({inner})"
        if prop.kind == PropertyKind.PAGE:
            self.imports.update({"java.util.ArrayList", "java.util.List", "org.springframework.data.domain.PageImpl"})
            inner = self._element_expr(owner, prop, child, stack)
            return "new PageImpl<>(new ArrayList<>())" if inner is None else f"new PageImpl<>({self._collection_expr('ArrayList', inner, prop.minimum_size)})"
        if prop.kind == PropertyKind.ARRAY:
            component = prop.array_component_type or "Object"
            inner = self._element_expr(owner, prop, child, stack)
            if inner is None:
                return f"new {_raw_simple(component)}[0]"
            count = max(1, prop.minimum_size)
            return f"new {_raw_simple(component)}[] {{{', '.join(inner for _ in range(count))}}}"
        if prop.kind == PropertyKind.WRAPPER:
            raw = _raw_simple(prop.type_name)
            inner = self._wrapped_expr(owner, prop, child, stack)
            if raw == "CompletableFuture":
                self.imports.add("java.util.concurrent.CompletableFuture")
                return "CompletableFuture.completedFuture(null)" if inner is None else f"CompletableFuture.completedFuture({inner})"
            if raw == "Mono":
                return "Mono.empty()" if inner is None else f"Mono.just({inner})"
            if raw == "Flux":
                return "Flux.empty()" if inner is None else f"Flux.just({inner})"
        return None

    @staticmethod
    def _collection_expr(kind: str, element: str, minimum_size: int) -> str:
        count = max(1, minimum_size)
        values = ", ".join(element for _ in range(count))
        return f"new {kind}<>(List.of({values}))"

    def _element_expr(self, owner: PayloadSchema, prop: PayloadProperty, child: PayloadSchema | None, stack: tuple[str, ...]) -> str | None:
        if child:
            if child.kind == "enum" and child.enum_constants:
                return f"{self._type_ref(child)}.{child.enum_constants[0]}"
            if child.construction_kind != ConstructionKind.UNSUPPORTED:
                args = f'fixtureScenario, fixtureChildPaths(requiredPaths, "{prop.name}")' if self.projection_aware else ""
                return f"{self._schema_method_name(child)}({args})"
        element_type = prop.element_type or prop.array_component_type
        if element_type:
            return self._scalar_literal(owner.type_name, prop.name, element_type, prop.enum_constants)
        return None

    def _wrapped_expr(self, owner: PayloadSchema, prop: PayloadProperty, child: PayloadSchema | None, stack: tuple[str, ...]) -> str | None:
        if child:
            if child.kind == "enum" and child.enum_constants:
                return f"{self._type_ref(child)}.{child.enum_constants[0]}"
            if child.construction_kind != ConstructionKind.UNSUPPORTED:
                args = f'fixtureScenario, fixtureChildPaths(requiredPaths, "{prop.name}")' if self.projection_aware else ""
                return f"{self._schema_method_name(child)}({args})"
        if prop.wrapped_type:
            return self._scalar_literal(owner.type_name, prop.name, prop.wrapped_type, prop.enum_constants)
        return None

    def _would_cycle(self, start_id: str | None, target_id: str, seen: set[str] | None = None) -> bool:
        if not start_id:
            return False
        if start_id == target_id:
            return True
        seen = set() if seen is None else seen
        if start_id in seen:
            return False
        seen.add(start_id)
        schema = self.by_id.get(start_id) or self.by_name.get(start_id) or self.by_name.get(_simple(start_id))
        if schema is None:
            return False
        for prop in schema.properties:
            for ref in prop.nested_schema_refs:
                child = self.by_id.get(ref) or self.by_name.get(ref) or self.by_name.get(_simple(ref))
                child_id = (child.fqcn or child.type_name) if child else ref
                if child_id == target_id or self._would_cycle(child_id, target_id, seen):
                    return True
        return False

    @staticmethod
    def _safe_initializer(value: str) -> bool:
        text = value.strip()
        return bool(re.fullmatch(r"(?:true|false|null|-?\d+(?:\.\d+)?[fFdDlL]?|'(?:\\.|[^'])'|\"(?:\\.|[^\"])*\")", text))

    def _scalar_literal(self, owner: str, field_name: str, java_type: str, enum_constants: tuple[str, ...]) -> str:
        simple = _raw_simple(java_type)
        lowered = field_name.lower()
        if enum_constants:
            return f"{simple}.{enum_constants[0]}"
        if simple in {"boolean", "Boolean"}:
            return "false"
        # Java permits constant narrowing in assignments, but not in method
        # invocation conversion.  Generated fixture values are passed to setter
        # methods, so byte/short arguments require explicit casts.  The same
        # primitive literals also box correctly for Byte/Short parameters.
        if simple in {"byte", "Byte"}:
            return "(byte) 1"
        if simple in {"short", "Short"}:
            return "(short) 1"
        if simple in {"int", "Integer"}:
            return "1"
        if simple in {"long", "Long"}:
            return "1L"
        if simple in {"float", "Float"}:
            return "1.0f"
        if simple in {"double", "Double"}:
            return "1.0d"
        if simple in {"char", "Character"}:
            return "'A'"
        if simple == "BigDecimal":
            self.imports.add("java.math.BigDecimal")
            return 'new BigDecimal("100.00")'
        if simple == "BigInteger":
            self.imports.add("java.math.BigInteger")
            return "BigInteger.ONE"
        if simple == "UUID":
            self.imports.add("java.util.UUID")
            return 'UUID.fromString("00000000-0000-0000-0000-000000000001")'
        if simple == "LocalDate":
            self.imports.add("java.time.LocalDate")
            return "LocalDate.of(2020, 1, 1)"
        if simple == "LocalDateTime":
            self.imports.add("java.time.LocalDateTime")
            return "LocalDateTime.of(2020, 1, 1, 0, 0)"
        if simple == "LocalTime":
            self.imports.add("java.time.LocalTime")
            return "LocalTime.NOON"
        if simple == "Instant":
            self.imports.add("java.time.Instant")
            return "Instant.EPOCH"
        if simple == "Date":
            self.imports.add("java.util.Date")
            return "new Date(0L)"
        if simple == "String":
            if self.strict_source_values:
                # Equality/enum/configuration-derived values arrive through
                # PayloadProperty.baseline_value before this fallback. For an
                # open non-null String domain, "A" is a bounded witness—not an
                # invented status/type/code token—and the limitation is reported.
                self.diagnostics.append(
                    f"bounded nonblank String witness used for {owner}.{field_name}; exact path value was not source-constrained"
                )
                return '"A"'
            if "email" in lowered:
                value = "test@example.com"
            elif any(x in lowered for x in ("dob", "birthdate", "birth_date")):
                value = "1990-01-01"
            elif "date" in lowered or lowered.endswith("from") or lowered.endswith("to"):
                value = "2020-01-01"
            elif "gender" in lowered:
                value = "M"
            elif "country" in lowered or "nationality" in lowered:
                value = "SG"
            elif "postal" in lowered or "zipcode" in lowered:
                value = "123456"
            elif any(x in lowered for x in ("phone", "mobile", "contactvalue")):
                value = "91234567"
            elif "url" in lowered:
                value = "https://example.test"
            elif "name" in lowered:
                value = "Test Name"
            elif "code" in lowered or "ref" in lowered:
                value = "CODE1"
            elif lowered.endswith("no") or lowered.endswith("number"):
                value = "NO123"
            elif lowered.endswith("id"):
                value = "ID123"
            elif "type" in lowered:
                value = "TYPE1"
            else:
                value = f"{field_name}-value"
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
        # A resolved enum should have been classified earlier. Returning null for
        # unknown external leaves is explicit and reported, not hidden invention.
        self.diagnostics.append(f"no deterministic scalar value for {owner}.{field_name}: {java_type}")
        return "null"

    def _emit_map(self, schema: MapSchema) -> FixtureSpec:
        method_name = self._map_method_name(schema)
        dependencies: list[str] = []
        self.imports.update({"java.util.Map", "java.util.LinkedHashMap", "java.util.ArrayList", "java.util.List"})
        parameter = "String fixtureScenario, String... requiredPaths" if self.projection_aware else ""
        lines = [
            f"    private Map<String, Object> {method_name}({parameter}) {{",
            "        Map<String, Object> value = new LinkedHashMap<>();",
        ]
        for entry in schema.entries:
            expression = self._map_entry_expr(schema, entry)
            if entry.nested_schema_id:
                dependencies.append(entry.nested_schema_id)
            if entry.collection_element_schema_id:
                dependencies.append(entry.collection_element_schema_id)
            if self.projection_aware:
                lines.extend((
                    f'        if (fixtureRequires(requiredPaths, "{entry.key_literal}")) {{',
                    f'            value.put("{entry.key_literal}", {expression});',
                    "        }",
                ))
            else:
                lines.append(f'        value.put("{entry.key_literal}", {expression});')
        lines.extend(("        return value;", "    }"))
        return FixtureSpec(
            fixture_id=schema.schema_id,
            method_name=method_name,
            return_type="Map<String, Object>",
            schema_id=schema.schema_id,
            dependent_fixture_ids=tuple(dict.fromkeys(dependencies)),
            imports=tuple(sorted(self.imports)),
            method_source="\n".join(lines),
            supported=True,
            diagnostics=() if schema.entries else ("map schema has no verified literal keys",),
            projection_aware=self.projection_aware,
        )

    def _map_entry_expr(self, owner: MapSchema, entry: MapEntrySchema) -> str:
        fallback = self._map_entry_expr_base(entry)
        if not self.projection_aware:
            return fallback
        values: list[tuple[str, str]] = []
        for scenario, schemas in self.scenario_map_schemas.items():
            scenario_owner = schemas.get(owner.schema_id)
            if scenario_owner is None:
                continue
            scenario_entry = next(
                (candidate for candidate in scenario_owner.entries if candidate.key_literal == entry.key_literal),
                None,
            )
            if scenario_entry is not None:
                values.append((scenario, self._map_entry_expr_base(scenario_entry)))
        return self._scenario_expression(fallback, values)

    def _map_entry_expr_base(self, entry: MapEntrySchema) -> str:
        if entry.baseline_value:
            return entry.baseline_value
        if entry.nested_schema_id and entry.nested_schema_id in self.map_by_id:
            args = f'fixtureScenario, fixtureChildPaths(requiredPaths, "{entry.key_literal}")' if self.projection_aware else ""
            return f"{self._map_method_name(self.map_by_id[entry.nested_schema_id])}({args})"
        if entry.collection_element_schema_id and entry.collection_element_schema_id in self.map_by_id:
            child = self._map_method_name(self.map_by_id[entry.collection_element_schema_id])
            args = f'fixtureScenario, fixtureChildPaths(requiredPaths, "{entry.key_literal}")' if self.projection_aware else ""
            return f"new ArrayList<>(List.of({child}({args})))"
        runtime = entry.runtime_type or "String"
        kind = entry.kind
        if kind in {PropertyKind.LIST, PropertyKind.SET, PropertyKind.COLLECTION}:
            return "new ArrayList<>()"
        if kind == PropertyKind.MAP:
            return "new LinkedHashMap<>()"
        return self._scalar_literal("Map", entry.key_literal, runtime, ())

    def _projection_support_fixture(self) -> FixtureSpec:
        source = """    private boolean fixtureRequires(String[] requiredPaths, String property) {
        if (requiredPaths == null) {
            return false;
        }
        for (String path : requiredPaths) {
            if (property.equals(path) || path.startsWith(property + ".")) {
                return true;
            }
        }
        return false;
    }

    private String[] fixtureChildPaths(String[] requiredPaths, String property) {
        List<String> childPaths = new ArrayList<>();
        if (requiredPaths == null) {
            return new String[0];
        }
        String prefix = property + ".";
        for (String path : requiredPaths) {
            if (path.startsWith(prefix) && path.length() > prefix.length()) {
                childPaths.add(path.substring(prefix.length()));
            }
        }
        return childPaths.toArray(new String[0]);
    }"""
        return FixtureSpec(
            fixture_id="__service_fixture_projection_support__",
            method_name="fixtureRequires",
            return_type="boolean",
            schema_id="__service_fixture_projection_support__",
            imports=("java.util.ArrayList", "java.util.List"),
            method_source=source,
            supported=True,
            projection_aware=True,
            internal=True,
        )


def emit_fixture_catalog(
    schemas: Iterable[PayloadSchema],
    map_schemas: Iterable[MapSchema] = (),
    *,
    projection_aware: bool = False,
    strict_source_values: bool = False,
    scenario_schemas: Mapping[str, Iterable[PayloadSchema]] | None = None,
    scenario_map_schemas: Mapping[str, Iterable[MapSchema]] | None = None,
) -> EmittedFixtures:
    return FixtureBuilderEmitter(
        schemas,
        map_schemas,
        projection_aware=projection_aware,
        strict_source_values=strict_source_values,
        scenario_schemas=scenario_schemas,
        scenario_map_schemas=scenario_map_schemas,
    ).emit_all()
