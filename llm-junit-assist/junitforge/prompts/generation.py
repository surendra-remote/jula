"""Class-type-aware initial JUnit generation prompts.

The prompt emitted here is intentionally deterministic and unit-test-only.  It is
optimized for fewer skipped/compile_failed files before coverage augmentation.
"""

from __future__ import annotations

import re

from junitforge.execution.assembly import cut_variable_name
from junitforge.execution.renderer import find_method_context, render_dependency_contracts, render_execution_context
from junitforge.models import GenerationContext, MethodSig
from junitforge.parser.collaborators import render_collaborator
from junitforge.prompts.exemplars import exemplar_for
from junitforge.prompts.system import build_system_prompt


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _common_header(ctx: GenerationContext) -> str:
    prompt = build_system_prompt(ctx.stack, ctx.template) or ""
    return (
        prompt.replace("{TEST_CLASS_NAME}", ctx.test_class_name or "GeneratedTest")
        .replace("{TEST_PACKAGE}", ctx.test_package or "com.fallback.test")
    )


def _collaborators_block(ctx: GenerationContext) -> str:
    if _is_strict_serviceimpl(ctx):
        return _strict_service_collaborator_whitelist(ctx)
    if ctx.execution_context is not None:
        return render_dependency_contracts(ctx.execution_context)
    if not ctx.collaborators:
        return "(none)"
    return "\n\n".join(render_collaborator(c) for c in ctx.collaborators).strip() or "(none)"


def _execution_context_block(ctx: GenerationContext, method: MethodSig | None = None) -> str:
    if ctx.execution_context is None:
        return ""
    method_id = None
    if method is not None:
        method_id = f"{method.name}({','.join(t for t, _ in method.params)})"
    return render_execution_context(ctx.execution_context, method_id=method_id)


def _annotation_names(ctx: GenerationContext) -> set[str]:
    return {a.split(".")[-1] for a in ((ctx.symbol.annotations if ctx.symbol else []) or [])}


def _annotation_names_lower(ctx: GenerationContext) -> set[str]:
    return {a.lower() for a in _annotation_names(ctx)}


def _template_style(ctx: GenerationContext) -> str:
    return ctx.template.style if ctx.template else "default"


def _template_kind(ctx: GenerationContext) -> str:
    if not ctx.template or not ctx.template.kind:
        return ""
    return str(getattr(ctx.template.kind, "value", ctx.template.kind))


def _java_bean_suffix(field_name: str) -> str:
    if not field_name:
        return ""
    if len(field_name) >= 2 and field_name[0].isupper() and field_name[1].isupper():
        return field_name
    return field_name[0].upper() + field_name[1:]


def _primitive_boolean(field_type: str | None) -> bool:
    return (field_type or "").strip() == "boolean"


def _bean_base(field_name: str, field_type: str | None) -> str:
    # boolean isActive -> isActive()/setActive(...)
    if _primitive_boolean(field_type) and field_name.startswith("is") and len(field_name) > 2 and field_name[2].isupper():
        return field_name[2:]
    return _java_bean_suffix(field_name)


def _getter_name(field_name: str, field_type: str | None) -> str:
    if _primitive_boolean(field_type):
        return f"is{_bean_base(field_name, field_type)}"
    # Boolean wrapper uses getX(), not isX().
    return f"get{_java_bean_suffix(field_name)}"


def _setter_name(field_name: str, field_type: str | None) -> str:
    return f"set{_bean_base(field_name, field_type)}"


def _method_name_from_signature(sig: str) -> str:
    m = re.search(r"\b([A-Za-z_$][\w$]*)\s*\(", sig or "")
    return m.group(1) if m else sig


def _dedupe_signatures(signatures: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sig in signatures:
        clean = " ".join((sig or "").strip().split())
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _is_strict_serviceimpl(ctx: GenerationContext) -> bool:
    """Return True only for the ServiceImpl generation path.

    This deliberately does not broaden generic @Component classes into the
    strict path.  When execution context is present, its deterministic target
    kind is authoritative.  The template-style fallback preserves compatibility
    for older runs that do not build execution context.
    """
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


def _is_lombok_generated_method(method: MethodSig) -> bool:
    return "LombokGenerated" in {a.split(".")[-1] for a in (method.annotations or [])}


def _strict_service_public_methods(ctx: GenerationContext) -> list[MethodSig]:
    """Source-declared public instance methods that may be called on ServiceImpl.

    Lombok-synthesized accessors/object methods and static helpers are excluded.
    Service tests must target business entry methods that are actually declared
    on the concrete class.
    """
    if not ctx.symbol:
        return []
    return [
        method
        for method in (ctx.symbol.methods or [])
        if method.is_public
        and not method.is_static
        and not method.is_constructor
        and not _is_lombok_generated_method(method)
    ]


def _strict_service_cut_whitelist(ctx: GenerationContext) -> str:
    methods = _strict_service_public_methods(ctx)
    lines = [
        "AUTHORITATIVE SERVICEIMPL CUT METHOD WHITELIST:",
        f"- CUT type: {ctx.symbol.fqcn if ctx.symbol else 'Unknown'}",
        "- Tests may call only the exact public instance methods listed below.",
    ]
    if methods:
        lines.extend(f"- {method.render()}" for method in methods)
    else:
        lines.append("- (none detected; do not invent a business method and do not generate a fake behavioral test)")
    lines.extend([
        "- Method names inferred from the class name, comments, examples, interfaces not resolved here, or business terminology are forbidden.",
        "- The CUT is a real @InjectMocks instance. Never use it as a Mockito when(...), doReturn(...).when(...), doThrow(...).when(...), doNothing(...).when(...), or verify(...) receiver.",
    ])
    return "\n".join(lines)


def _strict_service_collaborator_whitelist(ctx: GenerationContext) -> str:
    """Render exact Mockito receivers/calls for ServiceImpl generation."""
    lines = [
        "AUTHORITATIVE SERVICEIMPL MOCKITO WHITELIST:",
        "- Only the dependency fields below may be Mockito receivers.",
    ]
    execution = getattr(ctx, "execution_context", None)
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    if execution is not None:
        for method_context in execution.methods:
            for invocation in method_context.dependency_invocations:
                key = (
                    invocation.dependency_field,
                    invocation.method_name,
                    tuple(invocation.contract.parameter_types or ()),
                )
                if key in seen:
                    continue
                seen.add(key)
                params = ", ".join(invocation.contract.parameter_types or ())
                if not params:
                    params = ", ".join(arg.inferred_type or "?" for arg in invocation.arguments)
                return_type = invocation.contract.return_type or "<unresolved>"
                lines.append(
                    f"- {invocation.dependency_field} : {invocation.dependency_type} -> "
                    f"{return_type} {invocation.method_name}({params})"
                )
    if not seen:
        for collaborator in ctx.collaborators or []:
            signatures = [
                method.render()
                for method in (collaborator.signatures or [])
                if not method.is_static and "private" not in method.modifiers
            ]
            if signatures:
                lines.append(f"- dependency type {collaborator.simple}:")
                lines.extend(f"  - {sig}" for sig in signatures)
            else:
                lines.append(
                    f"- dependency type {collaborator.simple}: signatures unresolved; "
                    "do not invent or stub a method unless the exact invocation is visible in the CUT source"
                )
    if len(lines) == 2:
        lines.append("- (none; do not create Mockito stubs)")
    lines.extend([
        "- A method absent from this whitelist must not appear in when(...), doReturn/doThrow/doNothing, or verify(...).",
        "- Do not substitute a plausible method such as process, execute, handle, save, find, getData, or validate.",
    ])
    return "\n".join(lines)


def _safe_exemplar(ctx: GenerationContext, style: str) -> str:
    if _is_strict_serviceimpl(ctx):
        return "(disabled for ServiceImpl; generic examples may contain method names that do not exist on this CUT)"
    return exemplar_for(style) if ctx.template else ""


# ---------------------------------------------------------------------------
# Class-type detection
# ---------------------------------------------------------------------------


def _is_entity_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    name = ctx.symbol.name if ctx.symbol else ""
    return bool(annos & {"Entity", "Embeddable", "MappedSuperclass"}) or name.endswith("Entity")


def _is_exception_like(ctx: GenerationContext) -> bool:
    if not ctx.symbol:
        return False
    ext = (ctx.symbol.extends or "").split(".")[-1]
    return ext in {"Throwable", "Exception", "RuntimeException", "Error"} or ctx.symbol.name.endswith(("Exception", "Error"))


def _is_dto_like(ctx: GenerationContext) -> bool:
    if not ctx.symbol or _is_entity_like(ctx):
        return False
    annos = _annotation_names(ctx)
    name = ctx.symbol.name
    if annos & {"Service", "Component", "Repository", "Controller", "RestController", "Configuration", "SpringBootApplication"}:
        return False
    return (
        name.endswith(("Dto", "DTO", "Request", "Response", "Model", "Payload", "Command", "Event"))
        or bool(annos & {"Data", "Getter", "Setter", "Value", "Builder", "SuperBuilder"})
    )


def _is_controller_advice_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    name = ctx.symbol.name if ctx.symbol else ""
    return bool(annos & {"ControllerAdvice", "RestControllerAdvice"}) or name.endswith(("ExceptionHandler", "ControllerAdvice"))


def _is_controller_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    return bool(annos & {"Controller", "RestController", "ControllerAdvice", "RestControllerAdvice"}) or _template_style(ctx) == "controller-standalone-mockmvc"


def _is_repository_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    name = ctx.symbol.name if ctx.symbol else ""
    ext = ctx.symbol.extends if ctx.symbol else ""
    impls = set(ctx.symbol.implements if ctx.symbol else [])
    return "Repository" in annos or name.endswith("Repository") or ext in {"JpaRepository", "CrudRepository", "Repository"} or bool(impls & {"JpaRepository", "CrudRepository", "Repository"})


def _is_config_like(ctx: GenerationContext) -> bool:
    return bool(_annotation_names(ctx) & {"Configuration", "SpringBootApplication", "AutoConfiguration", "EnableAutoConfiguration", "TestConfiguration"})


def _is_feign_like(ctx: GenerationContext) -> bool:
    source = ctx.cut_source or ""
    annos = _annotation_names(ctx)
    return "FeignClient" in annos or "@FeignClient" in source


def _is_aop_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    name = ctx.symbol.name if ctx.symbol else ""
    return "Aspect" in annos or name.endswith(("Aspect", "Advice"))


def _is_mapper_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names_lower(ctx)
    name = ctx.symbol.name if ctx.symbol else ""
    return "mapper" in annos or name.endswith(("Mapper", "Assembler", "Converter"))


def _is_validator_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names_lower(ctx)
    name = ctx.symbol.name if ctx.symbol else ""
    impls = set(ctx.symbol.implements if ctx.symbol else [])
    return "validator" in annos or name.endswith(("Validator", "Validation")) or bool(impls & {"ConstraintValidator", "Validator"})


def _is_utility_like(ctx: GenerationContext) -> bool:
    if not ctx.symbol:
        return False
    if ctx.symbol.name.endswith(("Util", "Utils", "Helper", "Constants")):
        return True
    methods = ctx.symbol.public_methods()
    return bool(methods) and all(m.is_static for m in methods)


def _is_service_like(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    name = ctx.symbol.name if ctx.symbol else ""
    return bool(annos & {"Service", "Component"}) or name.endswith(("Service", "ServiceImpl", "Manager", "Processor", "Handler", "Facade"))


# ---------------------------------------------------------------------------
# Method / field rendering
# ---------------------------------------------------------------------------


def _explicit_public_methods(ctx: GenerationContext) -> list[str]:
    if not ctx.symbol:
        return []
    try:
        methods = ctx.symbol.public_methods()
    except Exception:
        methods = []
    return [m.render() for m in methods]


def _constructor_signatures(ctx: GenerationContext) -> list[str]:
    if not ctx.symbol:
        return []
    return [c.render() for c in (ctx.symbol.constructors or []) if "private" not in c.modifiers]


def _synthesize_lombok_methods(ctx: GenerationContext, existing: list[str]) -> list[str]:
    if not ctx.symbol:
        return existing

    annos = _annotation_names(ctx)
    source = ctx.cut_source or ""
    has_data = "Data" in annos or "@Data" in source
    has_value = "Value" in annos or "@Value" in source
    has_equals_hashcode = "EqualsAndHashCode" in annos or "@EqualsAndHashCode" in source
    has_to_string = "ToString" in annos or "@ToString" in source
    has_getter = has_data or has_value or "Getter" in annos or "@Getter" in source
    has_setter = has_data or "Setter" in annos or "@Setter" in source
    has_builder = "Builder" in annos or "SuperBuilder" in annos or "@Builder" in source or "@SuperBuilder" in source

    out = list(existing)
    names = {_method_name_from_signature(s) for s in out}

    for field in ctx.symbol.fields or []:
        if "static" in field.modifiers:
            continue
        field_annos = {a.split(".")[-1] for a in (field.annotations or [])}
        ftype = field.type or "Object"
        if has_getter or "Getter" in field_annos:
            getter = _getter_name(field.name, ftype)
            if getter not in names:
                out.append(f"public {ftype} {getter}() [Lombok generated from Java field '{field.name}']")
                names.add(getter)
        if has_setter or "Setter" in field_annos:
            setter = _setter_name(field.name, ftype)
            if setter not in names:
                out.append(f"public void {setter}({ftype} {field.name}) [Lombok generated from Java field '{field.name}']")
                names.add(setter)

    if has_data or has_value or has_equals_hashcode:
        hash_guidance = (
            "public int hashCode() [Lombok generated; for callSuper=true use same-object self-consistency assertions only]"
            if _has_equals_hashcode_call_super(ctx)
            else "public int hashCode() [Lombok generated; hashCode consistency/equal-object tests allowed]"
        )
        for sig in (
            "public boolean equals(Object o) [Lombok generated; equality tests allowed using safe scalar fields only]",
            hash_guidance,
            "protected boolean canEqual(Object other) [Lombok generated; call only when test package equals source package]",
        ):
            name = _method_name_from_signature(sig)
            if name not in names:
                out.append(sig)
                names.add(name)

    if has_data or has_value or has_to_string:
        sig = "public String toString() [Lombok generated; prefer non-null and stable field-content checks only for scalar fields]"
        if "toString" not in names:
            out.append(sig)
            names.add("toString")

    if has_builder and "builder" not in names:
        out.append(f"public static {ctx.symbol.name}.{ctx.symbol.name}Builder builder() [Lombok generated]")
        names.add("builder")

    return _dedupe_signatures(out)


def _allowed_methods_block(ctx: GenerationContext) -> str:
    if _is_strict_serviceimpl(ctx):
        return _strict_service_cut_whitelist(ctx)
    methods = _dedupe_signatures(_synthesize_lombok_methods(ctx, _explicit_public_methods(ctx)))
    ctors = _constructor_signatures(ctx)
    lines: list[str] = []
    if ctors:
        lines.append("Constructors explicitly available:")
        lines.extend(f"- {c}" for c in ctors)
    if methods:
        lines.append("Callable methods explicitly/virtually available:")
        lines.extend(f"- {m}" for m in methods)
    if not lines:
        if _is_feign_like(ctx):
            return "- Feign interface target detected. Skip direct tests; mock this type only as a collaborator in ServiceImpl tests."
        return "- No callable public methods detected. Generate only construction/basic state tests if compile-safe, otherwise minimal test."
    return "\n".join(lines)


def _private_methods_block(ctx: GenerationContext) -> str:
    if not ctx.symbol:
        return "(none)"
    privates = [m.render() for m in (ctx.symbol.methods or []) if "private" in m.modifiers]
    return "\n".join(f"- {m}" for m in privates) if privates else "(none)"


def _fields_block(ctx: GenerationContext) -> str:
    if not ctx.symbol or not ctx.symbol.fields:
        return "(no fields detected)"
    lines: list[str] = []
    for f in ctx.symbol.fields:
        mods = " ".join(m for m in ("public", "protected", "private", "static", "final") if m in f.modifiers)
        annos = " ".join("@" + a for a in (f.annotations or []))
        prefix = " ".join(x for x in (annos, mods) if x)
        rendered = f"{prefix} {f.type} {f.name}".strip()
        if _is_strict_serviceimpl(ctx):
            # ServiceImpl fields are dependencies/configuration, not an accessor
            # test surface. Rendering guessed JavaBean names encourages the LLM
            # to create fake service getters/setters.
            lines.append(f"- {rendered} | no accessor call is authorized by this field listing")
        else:
            getter = _getter_name(f.name, f.type)
            setter = _setter_name(f.name, f.type)
            lines.append(f"- {rendered} | bean getter={getter}, setter={setter}")
    return "\n".join(lines)


def _imports_block(ctx: GenerationContext) -> str:
    imports = ctx.template.required_imports if ctx.template else []
    if not imports:
        return "(add only imports required by generated code)"
    return "\n".join(f"- {i}" for i in imports)


def _source_imports_block(ctx: GenerationContext) -> str:
    imports = list(getattr(ctx, "source_imports", None) or [])
    if not imports:
        return "(none detected)"
    return "\n".join(f"- {imp}" for imp in imports)


def _class_level_annotations_block(ctx: GenerationContext) -> str:
    annos = ctx.template.class_level_annotations if ctx.template else []
    return "\n".join(f"- {a}" for a in annos) if annos else "(none)"


def _injected_config_block(ctx: GenerationContext) -> str:
    """Render @Value fields WITH the exact values the skeleton injects.

    The test skeleton injects these via ReflectionTestUtils before the test runs.
    The model must therefore stub collaborator calls that receive them using the
    same literal values, or the stub will not match at runtime.
    """
    execution = getattr(ctx, "execution_context", None)
    if execution is None:
        return "(none)"
    lines: list[str] = []
    for field in getattr(execution, "configuration_fields", ()) or ():
        if field.static or field.test_value is None:
            continue
        lines.append(f"- {field.type_name} {field.field_name} = {field.test_value}")
    return "\n".join(lines) if lines else "(none)"


def _value_fields_block(ctx: GenerationContext) -> str:
    if not ctx.symbol:
        return "(none)"
    lines = []
    for f in ctx.symbol.fields or []:
        if any(a.split(".")[-1] == "Value" for a in (f.annotations or [])):
            lines.append(f"- {f.type} {f.name}")
    return "\n".join(lines) if lines else "(none)"



def _field_annotation_set(field) -> set[str]:
    return {a.split(".")[-1] for a in (getattr(field, "annotations", None) or [])}


def _is_relationship_or_collection_field(field) -> bool:
    annos = _field_annotation_set(field)
    ftype = (getattr(field, "type", "") or "").strip()
    if annos & {"OneToOne", "OneToMany", "ManyToOne", "ManyToMany", "ElementCollection"}:
        return True
    return any(token in ftype for token in ("List<", "Set<", "Map<", "Collection<", "[]"))


def _safe_object_contract_fields(ctx: GenerationContext) -> list[str]:
    if not ctx.symbol:
        return []
    names: list[str] = []
    source = ctx.cut_source or ""
    for f in ctx.symbol.fields or []:
        if "static" in f.modifiers:
            continue
        annos = _field_annotation_set(f)
        if annos & {"Transient", "EqualsAndHashCode.Exclude", "Exclude", "ToString.Exclude"}:
            continue
        if _is_relationship_or_collection_field(f):
            continue
        # Keep embeddable/id/scalar/enum/string/date/number fields; avoid deep object graphs.
        names.append(f.name)
    # If explicit equals mentions a subset of fields, expose that subset first.
    explicit = []
    for name in names:
        if re.search(rf"\b{re.escape(name)}\b", source) and re.search(r"\bboolean\s+equals\s*\(", source):
            explicit.append(name)
    return explicit or names


def _has_method(ctx: GenerationContext, name: str, *, lombok_ok: bool = True) -> bool:
    if not ctx.symbol:
        return False
    for m in (ctx.symbol.methods or []):
        if m.name != name:
            continue
        if lombok_ok:
            return True
        if "LombokGenerated" not in {a.split(".")[-1] for a in (m.annotations or [])}:
            return True
    return False


def _has_equality_contract(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    source = ctx.cut_source or ""
    return (
        _has_method(ctx, "equals")
        or _has_method(ctx, "hashCode")
        or bool(annos & {"Data", "Value", "EqualsAndHashCode"})
        or "@EqualsAndHashCode" in source
        or re.search(r"\bboolean\s+equals\s*\(", source) is not None
    )


def _has_equals_hashcode_call_super(ctx: GenerationContext) -> bool:
    """Return True only for Lombok @EqualsAndHashCode(callSuper = true)."""
    source = ctx.cut_source or ""
    return re.search(
        r"@(?:lombok\.)?EqualsAndHashCode\s*\([^)]*\bcallSuper\s*=\s*true\b[^)]*\)",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    ) is not None


def _has_to_string_contract(ctx: GenerationContext) -> bool:
    annos = _annotation_names(ctx)
    source = ctx.cut_source or ""
    return _has_method(ctx, "toString") or bool(annos & {"Data", "Value", "ToString"}) or "@ToString" in source


def _same_test_package_as_cut(ctx: GenerationContext) -> bool:
    if not ctx.symbol or not ctx.symbol.fqcn or "." not in ctx.symbol.fqcn:
        return False
    source_pkg = ctx.symbol.fqcn.rsplit(".", 1)[0]
    return source_pkg == (ctx.test_package or "")


def _object_contract_block(ctx: GenerationContext) -> str:
    if not (_is_entity_like(ctx) or _is_dto_like(ctx)):
        return "(not applicable)"

    fields = _safe_object_contract_fields(ctx)
    field_text = ", ".join(fields) if fields else "(none detected)"
    lines = [
        "Object-method contract guidance:",
        f"- Safe equality/toString fields detected: {field_text}",
    ]

    if _has_equals_hashcode_call_super(ctx):
        lines.extend([
            "- SPECIAL RULE FOR @EqualsAndHashCode(callSuper = true): generate one simple equality/hashCode contract test only.",
            "- Create exactly two separate objects of this same concrete class, normally named first and second.",
            "- Set identical values on both objects for every writable Java field listed for this class. Include inherited fields only when their exact setter/getter methods are explicitly available; never invent parent accessors.",
            "- Compare every initialized field using its verified getter, for example assertEquals(first.getName(), second.getName()).",
            "- After the field-by-field comparisons, assertEquals(first, second).",
            "- HashCode must use same-object self-consistency checks only: assertEquals(first.hashCode(), first.hashCode()); and assertEquals(second.hashCode(), second.hashCode());",
            "- Do not compare first.hashCode() with second.hashCode() for this annotation.",
            "- Do not create parent-class objects, do not compare parent and child instances, and do not call canEqual directly.",
        ])
    elif _has_equality_contract(ctx):
        lines.extend([
            "- Generate equals/hashCode tests. Cover: same instance, null, different type, equal object, and one different participating field when a safe field is available.",
            "- For manually declared equals/hashCode, read the source body and test only the fields actually used by that implementation. Do not assume every field participates.",
            "- For Lombok @EqualsAndHashCode.Include/@Exclude, honor Include/Exclude. Do not use excluded or relationship fields for equality differences.",
            "- For plain Lombok @Data/@Value without explicit Include/Exclude, use safe scalar/non-relationship fields only.",
        ])
    else:
        lines.append("- No reliable equality contract detected. Use only same-instance, null, different-type, hashCode self-consistency, and toString non-null smoke assertions. Do not compare two distinct CUT instances with assertEquals/assertNotEquals, and do not compare hashCode across two distinct instances.")

    if _has_equals_hashcode_call_super(ctx):
        lines.append("- For this callSuper=true contract, skip direct canEqual assertions even when Lombok exposes canEqual.")
    elif _has_method(ctx, "canEqual") and _same_test_package_as_cut(ctx):
        lines.append("- canEqual is listed and the test package matches the source package; generate simple canEqual true/false assertions if they compile.")
    else:
        lines.append("- Do not call canEqual unless it is listed and package-accessible from the generated test.")

    if _has_to_string_contract(ctx):
        lines.append("- Generate toString coverage. Prefer assertNotNull(toString()). You may assert contains(class name) or safe scalar field values only when stable and not excluded.")
    else:
        lines.append("- toString has no explicit/generated contract detected; only assertNotNull(toString()) if callable.")

    return "\n".join(lines)


def _constructor_builder_block(ctx: GenerationContext) -> str:
    if not (_is_entity_like(ctx) or _is_dto_like(ctx)):
        return "(not applicable)"
    lines = ["Constructor and builder guidance:"]
    ctors = _constructor_signatures(ctx)
    if ctors:
        lines.append("- Generate constructor tests for every non-private constructor listed in ALLOWED METHODS / CONSTRUCTORS, including Lombok @AllArgsConstructor and manually declared argument constructors.")
        lines.append("- For argument constructors, pass source-faithful sample values and assert the corresponding getters. Do not call constructors that are not listed.")
    else:
        lines.append("- No non-private constructors listed. Do not invent no-args or all-args constructor tests.")
    if _has_method(ctx, "builder"):
        lines.append("- Generate a @Builder/@SuperBuilder test using ClassName.builder().fieldName(value).build(), then assert getters for populated fields. Use only field names listed in JAVA FIELDS DETECTED.")
    else:
        lines.append("- builder() is not listed. Do not generate builder tests.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Strategy blocks
# ---------------------------------------------------------------------------


def _strategy_block(ctx: GenerationContext) -> str:
    if _is_entity_like(ctx):
        return (
            "ENTITY TEST STRATEGY:\n"
            "- Use pure JUnit 5 only. No Mockito and no Spring context.\n"
            "- Target Lombok/JPA POJO coverage, not database behavior.\n"
            "- For Lombok @Data/@Getter/@Setter/@Value, cover compact getter/setter happy path and one compact null-field path. For null-field testing, explicitly call each non-primitive field setter with null before asserting the getter is null; never assert default-null values on a freshly constructed object. This also applies to relationship fields such as entity references and collections.\n"
            "- Generate equals/hashCode/toString/canEqual according to the OBJECT-METHOD CONTRACT block below. Do not keep only smoke assertions when the source exposes an explicit or Lombok equality contract.\n"
            "- Primitive boolean active uses isActive(); Boolean active uses getActive(); boolean isActive uses isActive() and setActive(...).\n"
            "- Generate constructor tests for @AllArgsConstructor or manually declared argument constructors only when they are listed. @Data alone is not enough.\n"
            "- Generate @Builder/@SuperBuilder tests only when builder() is listed.\n"
        )

    if _is_dto_like(ctx):
        return (
            "DTO / MODEL / REQUEST / RESPONSE TEST STRATEGY:\n"
            "- Use pure JUnit 5 only. No Mockito and no Spring context.\n"
            "- Apply the same Lombok/accessor/object-method/constructor/builder coverage rules as entities.\n"
            "- Keep tests compact: one happy-path accessor test and one compact null-field test are enough. In the null-field test, explicitly set non-primitive fields to null through setters before asserting getters return null; do not rely on default field values.\n"
            "- Do not call private helper methods such as toIndentedString(Object) directly. Cover them through public toString().\n"
            "- For generated or custom toString(), follow the OBJECT-METHOD CONTRACT block. Do not assert unstable formatting.\n"
        )

    if _is_exception_like(ctx):
        return (
            "EXCEPTION TEST STRATEGY:\n"
            "- Use pure JUnit 5 only.\n"
            "- Test only constructors that are explicitly available.\n"
            "- Cover no-args, message, cause, message+cause constructors if listed.\n"
            "- Cover custom fields/accessors if present.\n"
        )

    if _is_service_like(ctx):
        return (
            "SERVICEIMPL / BUSINESS SERVICE TEST STRATEGY:\n"
            "- Use @ExtendWith(MockitoExtension.class), @Mock dependencies, and @InjectMocks for the class under test.\n"
            "- No Spring context, @Autowired, @MockBean, @MockitoBean, @SpringBootTest, or application.yaml loading.\n"
            "- Feign clients are normal @Mock interfaces. Do not instantiate or enable Feign.\n"
            "- Generate tests for real source paths only: happy path, each visible if/else branch, empty-list path only when source checks empty, null-response path only when source handles null, and exception path only when source catches or propagates it.\n"
            "- Prefer exact Mockito arguments when the value is known. Use any()/anyString() only when the source transforms the input or exact value is irrelevant.\n"
            "- Initialize all fields on mocked return objects that production code reads.\n"
            "- Verify repository/client interactions that source actually performs.\n"
            "- Treat the AUTHORITATIVE SERVICEIMPL CUT METHOD WHITELIST as an exact allow-list. Every behavioral @Test must call one listed CUT method; never derive a method name from the class name or business wording.\n"
            "- Treat the AUTHORITATIVE SERVICEIMPL MOCKITO WHITELIST as an exact allow-list. Stub/verify only listed dependency receiver+method combinations.\n"
            "- Never mock, spy, stub, or verify the @InjectMocks ServiceImpl itself. Call its listed public method normally.\n"
        )

    if _is_controller_advice_like(ctx):
        return (
            "RESTCONTROLLERADVICE / CONTROLLERADVICE TEST STRATEGY:\n"
            "- Use direct Mockito/JUnit tests for public @ExceptionHandler methods. Do not start Spring MVC or Spring context.\n"
            "- Use @ExtendWith(MockitoExtension.class) when the advice has collaborators. Mock collaborators such as Environment.\n"
            "- Instantiate the advice directly, or use @InjectMocks only when constructor/field injection is straightforward.\n"
            "- If the advice has private collaborator/config fields such as Environment env and no real setter, inject with ReflectionTestUtils.setField(advice, \"env\", environment).\n"
            "- Do not invent setter methods such as setEnv(), setEnvironment(), setMessageSource(), or setObjectMapper() unless that exact method is listed in ALLOWED METHODS / CONSTRUCTORS.\n"
            "- If source calls Environment.getProperty(...), stubbing environment.getProperty(...) is valid. Do not reject it as invented.\n"
            "- Create real exception instances and call the public handler method directly; assert ResponseEntity status/body or returned error DTO fields.\n"
            "- Do not use @WebMvcTest, MockMvc, servlet container, @Autowired, @MockBean, @MockitoBean, or application.yaml loading.\n"
        )

    if _is_controller_like(ctx):
        return (
            "CONTROLLER TEST STRATEGY:\n"
            "- Use standalone MockMvc only: MockMvcBuilders.standaloneSetup(controller).build().\n"
            "- Use MockitoExtension, @Mock service dependencies, and @InjectMocks controller.\n"
            "- Do not use @WebMvcTest, Spring context, security context, servlet container, @Autowired, @MockBean, or @MockitoBean.\n"
            "- Never generate custom nested servlet mock classes such as MockHttpServletResponse, MockHttpServletRequest, MockFilterChain, or custom ServletOutputStream.\n"
            "- Never implement or extend HttpServletResponse, HttpServletRequest, ServletResponse, ServletRequest, FilterChain, ServletOutputStream, or PrintWriter in the generated test.\n"
            "- If a controller method needs HttpServletResponse/HttpServletRequest, use Mockito mock(HttpServletResponse.class) / mock(HttpServletRequest.class) or Spring's org.springframework.mock.web.MockHttpServletResponse / MockHttpServletRequest.\n"
            "- Use jakarta.servlet.* imports only; never use javax.servlet.* in Spring Boot 3.x tests.\n"
            "- Avoid fragile tests if source needs complex validators/security/exception handlers.\n"
        )

    if _is_repository_like(ctx):
        return (
            "REPOSITORY STRATEGY:\n"
            "- Repository tests are skipped in this no-Spring-context phase. Do not generate @DataJpaTest or inherited Spring Data method tests.\n"
        )

    if _is_config_like(ctx):
        return (
            "CONFIGURATION STRATEGY:\n"
            "- Configuration tests are skipped in this phase. Do not start Spring context or load application.yaml.\n"
        )

    if _is_feign_like(ctx):
        return (
            "OPENFEIGN STRATEGY:\n"
            "- Skip direct Feign interface tests. In ServiceImpl tests, mock Feign clients with Mockito.\n"
        )

    if _is_aop_like(ctx):
        return (
            "AOP / ASPECT STRATEGY:\n"
            "- Skip proxy behavior. Only direct public method tests are allowed if compile-safe.\n"
        )

    if _is_mapper_like(ctx):
        return (
            "MAPPER TEST STRATEGY:\n"
            "- Use pure JUnit 5. Do not mock DTOs/entities.\n"
            "- Create real source/target objects and assert mapped fields.\n"
            "- Test null/empty/partial mapping only when source explicitly handles it.\n"
        )

    if _is_validator_like(ctx):
        return (
            "VALIDATOR TEST STRATEGY:\n"
            "- Use pure JUnit unless direct collaborators require Mockito.\n"
            "- Test valid, null, empty, blank, invalid format, boundary length, and error path only when source code supports those branches.\n"
        )

    if _is_utility_like(ctx):
        return (
            "UTILITY TEST STRATEGY:\n"
            "- Use pure JUnit 5.\n"
            "- Test static/direct methods with null, empty, boundary values, parse/format success, and parse/format failure only when source logic supports them.\n"
            "- Private constructor reflection is optional and only if compile-safe and needed for coverage.\n"
        )

    return (
        "GENERAL UNIT TEST STRATEGY:\n"
        "- Use compile-safe JUnit 5 tests against explicit public methods only.\n"
        "- Use Mockito only when class has direct collaborators.\n"
        "- Do not start Spring context.\n"
    )


def _global_rules_block(ctx: GenerationContext) -> str:  # noqa: ARG001
    return (
        "GLOBAL NON-NEGOTIABLE RULES:\n"
        "- Return exactly one complete Java test file. Raw Java only. No markdown fences or explanations.\n"
        "- Use JUnit 5 Jupiter only. Never use JUnit 4.\n"
        "- Use Java 17-compatible simple syntax; avoid var, records, text blocks, switch expressions, and preview features.\n"
        "- Generate exactly one top-level test class with the required class name and exact target package.\n"
        "- Do not use @SpringBootTest, @WebMvcTest, @DataJpaTest, @ContextConfiguration, @SpringJUnitConfig, @EnableFeignClients, @Autowired, @MockBean, or @MockitoBean.\n"
        "- Do not load application.yaml or use Spring application context.\n"
        "- Do not invent methods, constructors, fields, enum values, constants, nested classes, or collaborator methods.\n"
        "- Only call methods listed in ALLOWED METHODS / CONSTRUCTORS.\n"
        "- For enum or constant references, use only values visible in the class source/imports; otherwise prefer null or a real object path that does not require an enum constant.\n"
        "- Never derive getter/setter names from @Column, @JoinColumn, @Table, SQL/database names, or annotation values.\n"
        "- Getter/setter names must come only from Java field names listed in JAVA FIELDS DETECTED.\n"
        "- Do not mention or test any field name that is not listed in JAVA FIELDS DETECTED for this exact class.\n"
        "- Do not test private methods directly. Cover private helpers through public methods only.\n"
        "- Reflection is allowed only for @Value field injection or unavoidable private constructor coverage.\n"
        "- Spring @Value fields are configuration inputs, not JavaBean properties. Do not call getX()/setX() for @Value fields unless that exact method is listed in ALLOWED METHODS / CONSTRUCTORS.\n"
        "- For non-static @Value fields, inject the value on the object instance with ReflectionTestUtils.setField(instance, \"fieldName\", value), not ClassName.class.\n"
        "- For private collaborator/config fields such as Environment env in @RestControllerAdvice/@ControllerAdvice classes, use ReflectionTestUtils.setField(instance, \"env\", environment) unless a real constructor or setter is listed. Never invent setEnv()/setEnvironment().\n"
        "- Do not assert @Value fields directly through generated getters; verify behavior only through real public methods that read those fields.\n"
        "- Lombok @Data does not imply @NoArgsConstructor or @AllArgsConstructor.\n"
        "- Do not assume null input throws, empty list returns null, or exceptions are caught unless source code proves it.\n"
        "- Use exact Mockito arguments when known; use any()/anyString() only when exact matching is impossible or irrelevant.\n"
        "- Keep tests compact; do not create separate null tests for every getter/setter.\n"
        "- Null-field tests must set each tested non-primitive field to null via its setter immediately before assertNull(getter()). Never assert default-null values on a new object. For relationship/entity/collection fields, either set the field to null first or skip the null assertion. Skip primitives in null-field tests.\n"
        "- Use two-instance equals tests only when the OBJECT-METHOD CONTRACT block permits it. For @EqualsAndHashCode(callSuper = true), follow its special rule exactly: same concrete class, identical field values, field-by-field getter comparisons, assertEquals(first, second), and hashCode self-comparisons only.\n"
        "- Call canEqual only when it is listed in ALLOWED METHODS and the test package is the same as the source package. Otherwise skip canEqual.\n"
        "- For toString, prefer assertNotNull(toString()). Assert class name or field-value contents only when the OBJECT-METHOD CONTRACT block indicates a stable generated or explicit toString contract.\n"
    )


def _value_rule_block(ctx: GenerationContext) -> str:
    if "@Value" not in (ctx.cut_source or "") and _value_fields_block(ctx) == "(none)":
        return ""
    return (
        "SPRING @VALUE FIELD RULE:\n"
        "- @Value fields are null when the class is created manually.\n"
        "- Treat @Value fields as private configuration inputs, not JavaBean properties.\n"
        "- If a tested public method reads a non-static @Value field, create the class under test as an object instance and set the field in @BeforeEach using ReflectionTestUtils.setField(instance, \"fieldName\", mockValue).\n"
        "- Do not use ReflectionTestUtils.setField(ClassName.class, ...) for non-static fields.\n"
        "- Do not call getFieldName()/setFieldName() for @Value fields unless that exact getter/setter is explicitly listed in ALLOWED METHODS / CONSTRUCTORS.\n"
        "- Do not assert the injected @Value field directly. Assert the output/effect of a real public method that uses the field.\n"
        "- Do not load application.yaml.\n"
        "- Inject host:port if production code prefixes http:// itself; inject full http://localhost:8080 only if production code expects a full URL.\n"
    )


# ---------------------------------------------------------------------------
# Public prompt builders
# ---------------------------------------------------------------------------


def build_generation_messages(ctx: GenerationContext) -> list[dict]:
    system = _common_header(ctx)
    source_code = ctx.cut_source or ""
    target_class = ctx.test_class_name or "GeneratedTest"
    target_package = ctx.test_package or "com.fallback.test"
    fqcn = ctx.symbol.fqcn if ctx.symbol else "Unknown"
    kind = ctx.symbol.kind if ctx.symbol else "unknown"
    style = _template_style(ctx)
    exemplar = _safe_exemplar(ctx, style)
    execution_block = _execution_context_block(ctx)
    execution_section = f"{execution_block}\n\n" if execution_block else ""

    user = (
        "Write a compile-safe JUnit 5 test class for the Java class under test.\n\n"
        f"Target test class : {target_class}\n"
        f"Target package    : {target_package}\n"
        f"Class under test  : {fqcn}\n"
        f"Class kind        : {kind}\n"
        f"Template style    : {style}\n\n"
        "=== GLOBAL RULES ===\n"
        f"{_global_rules_block(ctx)}\n\n"
        "=== CLASS-SPECIFIC TEST STRATEGY ===\n"
        f"{_strategy_block(ctx)}\n"
        f"{_value_rule_block(ctx)}\n"
        "=== CLASS-LEVEL ANNOTATIONS TO USE ===\n"
        f"{_class_level_annotations_block(ctx)}\n\n"
        "=== REQUIRED / RECOMMENDED IMPORTS ===\n"
        f"{_imports_block(ctx)}\n\n"
        "=== SOURCE IMPORTS FROM CLASS UNDER TEST ===\n"
        "These are available production types/static constants seen in the source. Reuse them only when generated code references the same simple type or static member. Do not copy unrelated imports.\n"
        f"{_source_imports_block(ctx)}\n\n"
        "=== EXEMPLAR STYLE ONLY ===\n"
        "Use this only for formatting style. Do not copy methods/classes from it.\n"
        f"{exemplar}\n\n"
        "=== CLASS UNDER TEST SOURCE ===\n"
        f"```java\n{source_code}\n```\n\n"
        "=== JAVA FIELDS DETECTED ===\n"
        "Use only these Java field names for accessor tests. Ignore annotation/database names.\n"
        f"{_fields_block(ctx)}\n\n"
        "=== @VALUE FIELDS DETECTED ===\n"
        f"{_value_fields_block(ctx)}\n\n"
        "=== PRIVATE METHODS DETECTED — DO NOT CALL DIRECTLY ===\n"
        f"{_private_methods_block(ctx)}\n\n"
        "=== DIRECT COLLABORATORS ===\n"
        "Only these dependencies may be mocked/stubbed. DTOs/entities/collections/primitives/String/Optional/BigDecimal/ResponseEntity should be real objects, not mocks.\n"
        f"{_collaborators_block(ctx)}\n\n"
        f"{execution_section}"
        "=== ALLOWED METHODS / CONSTRUCTORS TO CALL ON CLASS UNDER TEST ===\n"
        f"{_allowed_methods_block(ctx)}\n\n"
        "=== OBJECT-METHOD CONTRACT FOR ENTITY/DTO/MODEL ===\n"
        f"{_object_contract_block(ctx)}\n\n"
        "=== CONSTRUCTOR / BUILDER CONTRACT FOR ENTITY/DTO/MODEL ===\n"
        f"{_constructor_builder_block(ctx)}\n\n"
        "=== COVERAGE OBJECTIVE ===\n"
        "- For entity/DTO/model: maximize accessor, null-field, constructor, builder, equals/hashCode/toString, and package-accessible canEqual coverage according to the contract blocks above.\n"
        "- For ServiceImpl/business logic: cover real branches visible in source, with source-faithful assertions.\n"
        "- For mapper/validator/utility: cover real null/empty/boundary/error branches only if source supports them.\n"
        "- Do not chase framework-generated behavior or impossible branches.\n\n"
        "Return only the complete Java test file."
    )

    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_scaffolding_messages(ctx: GenerationContext) -> list[dict]:
    system = _common_header(ctx)
    target_class = ctx.test_class_name or "GeneratedTest"
    target_package = ctx.test_package or "com.fallback.test"
    fqcn = ctx.symbol.fqcn if ctx.symbol else "Unknown"
    style = _template_style(ctx)
    exemplar = _safe_exemplar(ctx, style)
    execution_block = _execution_context_block(ctx)
    execution_section = f"{execution_block}\n\n" if execution_block else ""

    user = (
        "Generate ONLY a baseline Java JUnit 5 test class skeleton. Do not write @Test methods yet.\n\n"
        f"Target test class : {target_class}\n"
        f"Target package    : {target_package}\n"
        f"Class under test  : {fqcn}\n"
        f"Template style    : {style}\n\n"
        "=== GLOBAL RULES ===\n"
        f"{_global_rules_block(ctx)}\n\n"
        "=== CLASS-SPECIFIC STRATEGY ===\n"
        f"{_strategy_block(ctx)}\n\n"
        "=== CLASS-LEVEL ANNOTATIONS TO USE ===\n"
        f"{_class_level_annotations_block(ctx)}\n\n"
        "=== IMPORTS ===\n"
        f"{_imports_block(ctx)}\n\n"
        "=== SOURCE IMPORTS FROM CLASS UNDER TEST ===\n"
        f"{_source_imports_block(ctx)}\n\n"
        "=== EXEMPLAR STYLE ONLY ===\n"
        f"{exemplar}\n\n"
        "=== DIRECT COLLABORATORS ===\n"
        f"{_collaborators_block(ctx)}\n\n"
        f"{execution_section}"
        "Return complete raw Java with package, imports, annotations, fields, mocks, and @BeforeEach only if needed."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_method_generation_messages(
    ctx: GenerationContext,
    method: MethodSig,
    current_suite_code: str,
) -> list[dict]:
    """Build one prompt that generates all tests for exactly one production method."""
    system = _common_header(ctx)
    method_rendered = method.render()
    method_id = f"{method.name}({','.join(type_name for type_name, _ in method.params)})"
    method_context = find_method_context(ctx.execution_context, method_id)
    execution_block = _execution_context_block(ctx, method)
    config_block = _injected_config_block(ctx)
    cut_var = cut_variable_name(ctx.symbol.name)
    existing = current_suite_code.strip() or "(none generated yet)"

    if method_context is None:
        method_source = "(method execution context unavailable; do not infer missing behavior)"
        collaborator_block = "(none authorized)"
    else:
        method_source = method_context.method_source
        collaborator_block = render_dependency_contracts(ctx.execution_context, method_id=method_id)  # type: ignore[arg-type]

    user = f"""Generate all compile-safe JUnit 5 @Test methods needed to cover ONE production method.

=== CLASS UNDER TEST ===
Type: {ctx.symbol.fqcn}
Existing CUT field in test skeleton: {cut_var}

=== ONLY AUTHORIZED CUT METHOD ===
{method_rendered}

No other method on {ctx.symbol.name} may be called by the generated tests.

=== EXACT TARGET METHOD SOURCE ===
```java
{method_source}
```

{execution_block}

=== INJECTED CONFIGURATION VALUES (already set by the test skeleton) ===
{config_block}

These exact values are live on the class under test before your test body runs.
When the target method passes one of these fields to a collaborator, you MUST
stub that call using the exact literal shown above (wrapped in eq(...)), never
any()/anyString() and never an invented literal. A mismatched stub returns null
and causes a NullPointerException or a failed assertion.

=== METHOD-SPECIFIC COLLABORATOR CONTRACTS ===
{collaborator_block}

=== EXISTING TEST METHOD NAMES ===
{existing}

=== REQUIRED COVERAGE FOR THIS ONE METHOD ===
- Generate tests for every meaningful if/else arm, nested condition, ternary arm, switch/default path, catch path, loop boundary, null guard, return, and throw listed in the authoritative context.
- Include the normal success path and all source-proven null/empty/error paths.
- For each path, initialize the complete verified nested parameter/request object graph before calling the CUT. Every intermediate dereference prefix listed in the execution context must be non-null; a null-safe terminal utility does not protect a null parent object.
- For each collaborator return object, initialize every verified nested property read by that selected path before thenReturn(...). For StringUtils.defaultString(unit.getValue())-style code, create unit and allow only the terminal value to be null when that branch is intended.
- Keep each Mockito stub inside only the test that executes that call. Do not use lenient().
- Do not stub collaborators for early-return paths that never invoke them.
- Call private helpers only indirectly through {cut_var}.{method.name}(...).

=== STRICT AUTHORIZATION ===
- Use only payload fields/getters/setters/builders/constructors/enum constants listed in the method-specific recursive payload schemas.
- Use only collaborator receiver+method combinations listed above.
- Do not invent CUT methods, collaborator methods, fields, accessors, constructors, builders, constants, branches, exceptions, HTTP statuses, or JSON properties.
- If a required type or contract is unresolved, omit that unsupported path instead of guessing.
- Never place {cut_var} inside when(...), doReturn/doThrow/doNothing(...).when(...), or verify(...).
- No @SpringBootTest, @WebMvcTest, @DataJpaTest, @Autowired, @MockBean, or @MockitoBean.
- No direct private-method invocation and no ReflectionTestUtils.invokeMethod.
- Direct @Value fields are injected by the deterministic test skeleton using exact field names. Override a value inside a test only when the selected branch requires a different configuration value; never load application.yaml.
- For Controller methods, use the existing standalone MockMvc field named mockMvc; do not create a Spring context.
- For Controller JSON assertions, use only verified payload property names.

=== OUTPUT CONTRACT ===
- Output raw Java @Test methods only.
- Do not output package, imports, class declaration, fields, @BeforeEach, or prose.
- Do not write tests that target a private CUT helper directly; cover private helpers
  only through the public method that reaches them.
- Do not define private methods in your output. Any fixture builders you need already
  exist in the test class; call them. Do not redefine or duplicate them.
- Do not duplicate any existing test method name.
- Every generated test must invoke only {cut_var}.{method.name}(...), directly or through MockMvc for this exact endpoint.
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]