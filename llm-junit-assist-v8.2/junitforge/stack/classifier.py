"""Deterministic class-type and structured-input classification.

TemplateSpec remains the source of truth for test style. Execution-target
classification independently decides whether reachable schema/fixture context
is required, so recursive object graphs are no longer restricted to
Controller/ServiceImpl stereotypes.
"""

from __future__ import annotations

from junitforge.execution.models import ExecutionTargetKind
from junitforge.models import ClassSymbol, ClasspathProfile, StackProfile, TemplateSpec, TestKind

_CONTEXT_FORBIDDEN_IMPORTS = [
    "org.junit.Test",
    "org.junit.runner.RunWith",
    "org.powermock",
    "org.springframework.boot.test.context.SpringBootTest",
    "org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest",
    "org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest",
    "org.springframework.boot.test.autoconfigure.web.reactive.WebFluxTest",
    "org.springframework.test.context.ContextConfiguration",
    "org.springframework.test.context.junit.jupiter.SpringJUnitConfig",
    "org.springframework.cloud.openfeign.EnableFeignClients",
    "org.springframework.beans.factory.annotation.Autowired",
    "org.springframework.boot.test.mock.mockito.MockBean",
    "org.springframework.test.context.bean.override.mockito.MockitoBean",
    "@SpringBootTest",
    "@DataJpaTest",
    "@WebMvcTest",
    "@WebFluxTest",
    "@ContextConfiguration",
    "@SpringJUnitConfig",
    "@EnableFeignClients",
    "@Autowired",
    "@MockBean",
    "@MockitoBean",
]

_REPO_SUPERS = {"JpaRepository", "CrudRepository", "PagingAndSortingRepository", "Repository"}
_CONFIG_ANNOS = {"configuration", "autoconfiguration", "springbootapplication", "enableautoconfiguration", "testconfiguration"}
_CONTROLLER_ANNOS = {"controller", "restcontroller"}
_CONTROLLER_ADVICE_ANNOS = {"controlleradvice", "restcontrolleradvice"}
_ENTITY_ANNOS = {"entity", "embeddable", "mappedsuperclass"}
_LOMBOK_POJO_ANNOS = {"data", "getter", "setter", "value", "builder", "superbuilder"}
_FEIGN_ANNOS = {"feignclient"}
_AOP_ANNOS = {"aspect"}
_VALIDATOR_SUPERS = {"ConstraintValidator", "Validator"}
_FILTER_SUPERS = {"OncePerRequestFilter", "GenericFilterBean", "Filter"}
_SCHEDULER_ANNOS = {"scheduled"}
_EXCEPTION_SUPERS = {"Throwable", "Exception", "RuntimeException", "Error"}


def _annos(symbol: ClassSymbol) -> set[str]:
    # Strip a leading '@' defensively: the CLI parse path removes it but the
    # javalang fallback path can retain it, which would make "@Service" fail to
    # match the "service" checks below and misclassify a @Service class as a
    # pure POJO (pure_unit). lstrip('@') makes classification parse-path-agnostic.
    return {a.split(".")[-1].lstrip("@").lower() for a in (symbol.annotations or [])}


def _impls(symbol: ClassSymbol) -> set[str]:
    return {_simple_parameter_type(i) for i in (symbol.implements or [])}


def _extends(symbol: ClassSymbol) -> str:
    return (symbol.extends or "").split(".")[-1]


def _simple_parameter_type(type_name: str) -> str:
    text = (type_name or "").strip()
    while "<" in text and ">" in text:
        left = text.find("<")
        right = text.rfind(">")
        if left < right:
            text = text[:left] + text[right + 1:]
        else:
            break
    return text.replace("[]", "").rsplit(".", 1)[-1]


_SIMPLE_PARAMETER_TYPES = {
    "boolean", "byte", "short", "int", "long", "float", "double", "char",
    "Boolean", "Byte", "Short", "Integer", "Long", "Float", "Double", "Character",
    "String", "CharSequence", "BigDecimal", "BigInteger", "UUID",
    "Date", "LocalDate", "LocalDateTime", "OffsetDateTime", "ZonedDateTime",
}


def _has_structured_public_entry(symbol: ClassSymbol) -> bool:
    """Return whether a concrete class exposes a source-level structured-input method.

    This is the capability gate for recursive payload analysis.  It is based on
    method inputs, not on the class stereotype, so processors/helpers with DTO or
    entity parameters receive the same object-graph catalog as ServiceImpl.
    """
    for method in symbol.methods or []:
        if not method.is_public or method.is_constructor or method.is_abstract:
            continue
        if "LombokGenerated" in {a.split(".")[-1] for a in (method.annotations or [])}:
            continue
        for type_name, _ in method.params:
            simple = _simple_parameter_type(type_name)
            if simple not in _SIMPLE_PARAMETER_TYPES:
                return True
    return False


def classify_execution_target(symbol: ClassSymbol) -> ExecutionTargetKind:
    """Select classes that receive deterministic reachable object-graph analysis.

    Controller and ServiceImpl retain method-wise generation. Validators,
    utilities, mappers, and any other concrete class with a structured public
    parameter receive the same schema/fixture catalog while retaining their
    existing class-specific generation template.
    """
    annos = _annos(symbol)
    if symbol.kind != "class" or "abstract" in (symbol.modifiers or set()):
        return ExecutionTargetKind.NONE
    if annos & _CONTROLLER_ADVICE_ANNOS:
        return ExecutionTargetKind.NONE
    if annos & _CONTROLLER_ANNOS:
        return ExecutionTargetKind.CONTROLLER
    if "service" in annos or symbol.name.endswith("ServiceImpl"):
        return ExecutionTargetKind.SERVICE_IMPL
    if any(name.endswith("Service") for name in _impls(symbol)):
        return ExecutionTargetKind.SERVICE_IMPL
    if _is_validator(symbol, annos):
        return ExecutionTargetKind.VALIDATOR
    if _is_utility(symbol):
        return ExecutionTargetKind.UTILITY
    if _is_mapper(symbol, annos):
        return ExecutionTargetKind.MAPPER
    if _has_structured_public_entry(symbol):
        return ExecutionTargetKind.STRUCTURED_UNIT
    return ExecutionTargetKind.NONE


def _forbidden(stack: StackProfile) -> list[str]:
    out = list(_CONTEXT_FORBIDDEN_IMPORTS)
    if stack.jakarta:
        out += ["javax.servlet", "javax.persistence", "javax.annotation", "javax.validation"]
    return out


def _junit_imports(include_before_each: bool = False) -> list[str]:
    imports = [
        "org.junit.jupiter.api.Test",
        "static org.junit.jupiter.api.Assertions.*",
    ]
    if include_before_each:
        imports.insert(1, "org.junit.jupiter.api.BeforeEach")
    return imports


def _mockito_imports(include_mockmvc: bool = False, include_reflection: bool = False) -> list[str]:
    imports = _junit_imports(include_before_each=True) + [
        "org.junit.jupiter.api.extension.ExtendWith",
        "org.mockito.InjectMocks",
        "org.mockito.Mock",
        "org.mockito.junit.jupiter.MockitoExtension",
        "static org.mockito.Mockito.*",
    ]
    if include_mockmvc:
        imports += [
            "org.springframework.test.web.servlet.MockMvc",
            "org.springframework.test.web.servlet.setup.MockMvcBuilders",
            "static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*",
            "static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*",
        ]
    if include_reflection:
        imports.append("org.springframework.test.util.ReflectionTestUtils")
    return imports


def _has_value_field(symbol: ClassSymbol) -> bool:
    for f in symbol.fields or []:
        if any(a.split(".")[-1] == "Value" for a in (f.annotations or [])):
            return True
    return False


def _returns_reactive(symbol: ClassSymbol) -> bool:
    for m in symbol.methods or []:
        rt = m.return_type or ""
        if rt.startswith("Mono") or rt.startswith("Flux") or "reactor.core.publisher" in rt:
            return True
    return False


def _has_any_public_logic(symbol: ClassSymbol) -> bool:
    return bool(symbol.public_methods()) or bool(symbol.constructors) or bool(symbol.fields)


def _pure(style: str, reasons: list[str], *, line: float = 90.0, branch: float = 80.0) -> TemplateSpec:
    return TemplateSpec(
        kind=TestKind.PURE_UNIT,
        style=style,
        class_level_annotations=[],
        mock_annotation="",
        required_imports=_junit_imports(),
        forbidden_imports=_forbidden(StackProfile()),
        reactive=False,
        testable=True,
        line_target=line,
        branch_target=branch,
        reasons=reasons,
        guidance="Use pure JUnit 5. Do not start Spring context. Do not use Mockito unless the class has real collaborators.",
    )


def _skip(style: str, reasons: list[str], stack: StackProfile) -> TemplateSpec:
    return TemplateSpec(
        kind=TestKind.INTEGRATION_ONLY,
        style=style,
        class_level_annotations=[],
        mock_annotation="",
        required_imports=[],
        forbidden_imports=_forbidden(stack),
        reactive=False,
        testable=False,
        line_target=0.0,
        branch_target=0.0,
        reasons=reasons,
        guidance="Skipped for the current no-Spring-context unit-generation phase.",
    )


def _unit(style: str, reasons: list[str], stack: StackProfile, *, mockmvc: bool = False, reflection: bool = False, line: float = 85.0, branch: float = 80.0) -> TemplateSpec:
    return TemplateSpec(
        kind=TestKind.UNIT,
        style=style,
        class_level_annotations=["@ExtendWith(MockitoExtension.class)"],
        mock_annotation="@Mock",
        required_imports=_mockito_imports(include_mockmvc=mockmvc, include_reflection=reflection),
        forbidden_imports=_forbidden(stack),
        reactive=False,
        testable=True,
        line_target=line,
        branch_target=branch,
        reasons=reasons,
        guidance="Use MockitoExtension, @Mock, and @InjectMocks only. Do not use Spring context or bean override annotations.",
    )


def _is_entity(symbol: ClassSymbol, annos: set[str]) -> bool:
    return bool(annos & _ENTITY_ANNOS) or symbol.name.endswith("Entity")


def _is_dto(symbol: ClassSymbol, annos: set[str]) -> bool:
    if annos & (_CONFIG_ANNOS | _CONTROLLER_ANNOS | {"service", "component", "repository"}):
        return False
    if _is_entity(symbol, annos):
        return False
    if symbol.kind == "record":
        return True
    # A class named like a component is NEVER a DTO, even if it carries Lombok
    # annotations such as @Slf4j / @RequiredArgsConstructor / @Getter. Without
    # this guard, a ServiceImpl with @RequiredArgsConstructor matches
    # _LOMBOK_POJO_ANNOS below and is misclassified as a pure POJO -> pure_unit,
    # which sends it down a no-mock path and it fails to generate.
    component_suffixes = ("ServiceImpl", "Service", "Manager", "Processor",
                          "Handler", "Facade", "Controller", "Repository",
                          "Mapper", "Validator", "Client")
    if symbol.name.endswith(component_suffixes):
        return False
    suffixes = ("Dto", "DTO", "Request", "Response", "Model", "Payload", "Command", "Event")
    return symbol.name.endswith(suffixes) or bool(annos & _LOMBOK_POJO_ANNOS)


def _is_exception(symbol: ClassSymbol) -> bool:
    ext = _extends(symbol)
    return ext in _EXCEPTION_SUPERS or symbol.name.endswith(("Exception", "Error"))


def _is_repository(symbol: ClassSymbol, annos: set[str]) -> bool:
    ext = _extends(symbol)
    impls = _impls(symbol)
    return "repository" in annos or ext in _REPO_SUPERS or bool(impls & _REPO_SUPERS) or symbol.name.endswith("Repository")


def _is_feign(symbol: ClassSymbol, annos: set[str]) -> bool:
    return bool(annos & _FEIGN_ANNOS) or symbol.name.endswith(("FeignClient", "Client")) and symbol.kind == "interface"


def _is_service(symbol: ClassSymbol, annos: set[str]) -> bool:
    return "service" in annos or "component" in annos or symbol.name.endswith(("Service", "ServiceImpl", "Manager", "Processor", "Handler", "Facade"))


def _is_mapper(symbol: ClassSymbol, annos: set[str]) -> bool:
    return "mapper" in annos or symbol.name.endswith(("Mapper", "Assembler", "Converter"))


def _is_validator(symbol: ClassSymbol, annos: set[str]) -> bool:
    return "validator" in annos or bool(_impls(symbol) & _VALIDATOR_SUPERS) or symbol.name.endswith(("Validator", "Validation"))


def _is_utility(symbol: ClassSymbol) -> bool:
    if symbol.name.endswith(("Util", "Utils", "Helper", "Constants")):
        return True
    methods = symbol.methods or []
    public_methods = [m for m in methods if m.is_public]
    static_public = [m for m in public_methods if m.is_static]
    return bool(public_methods) and len(static_public) == len(public_methods) and not symbol.constructors


def _is_aop(symbol: ClassSymbol, annos: set[str]) -> bool:
    return bool(annos & _AOP_ANNOS) or symbol.name.endswith(("Aspect", "Advice"))


def _is_filter(symbol: ClassSymbol) -> bool:
    return _extends(symbol) in _FILTER_SUPERS or bool(_impls(symbol) & _FILTER_SUPERS) or symbol.name.endswith(("Filter", "Interceptor"))


def _is_scheduler(symbol: ClassSymbol, annos: set[str]) -> bool:
    return symbol.name.endswith(("Scheduler", "Job", "Task")) or bool(annos & _SCHEDULER_ANNOS)


def classify(symbol: ClassSymbol, stack: StackProfile, cp: ClasspathProfile) -> TemplateSpec:  # noqa: ARG001
    """Return a no-Spring-context test template for one Java type."""
    annos = _annos(symbol)
    forbidden = _forbidden(stack)

    if _is_repository(symbol, annos):
        return _skip("skip-repository", ["repository tests require Spring Data/JPA context; skipped in unit-only phase"], stack)
    if _is_feign(symbol, annos):
        return _skip("skip-feign-interface", ["OpenFeign interface has no business logic; mock it from ServiceImpl tests"], stack)
    if annos & _CONFIG_ANNOS:
        return _skip("skip-configuration", ["configuration classes often require application.yaml/external beans; skipped in unit-only phase"], stack)
    if _is_aop(symbol, annos):
        return _skip("skip-aop", ["aspect/proxy behavior is fragile without Spring AOP context; skipped initially"], stack)
    if _is_filter(symbol) or _is_scheduler(symbol, annos):
        return _skip("skip-scheduler-filter", ["scheduler/filter/container behavior skipped unless simple public logic is later whitelisted"], stack)
    if _is_exception(symbol):
        return TemplateSpec(TestKind.PURE_UNIT, "exception-unit", [], "", _junit_imports(), forbidden, False, True, 95.0, 90.0, ["exception class -> constructor/accessor tests"], "Use pure JUnit constructor/accessor tests only.")
    if _is_entity(symbol, annos):
        return TemplateSpec(TestKind.PURE_UNIT, "entity-pojo", [], "", _junit_imports(), forbidden, False, True, 95.0, 90.0, ["entity/embeddable/mapped-superclass -> pure POJO tests"], "Use pure JUnit for fields, accessors, equals/hashCode, and toString. Never test JPA/database behavior.")
    if _is_dto(symbol, annos):
        return TemplateSpec(TestKind.PURE_UNIT, "dto-pojo", [], "", _junit_imports(), forbidden, False, True, 95.0, 90.0, ["DTO/model/request/response/Lombok POJO -> pure object tests"], "Use pure JUnit for constructors, accessors, equals/hashCode, toString, and builder only if present.")
    if annos & _CONTROLLER_ANNOS:
        return _unit("controller-standalone-mockmvc", ["controller -> standalone MockMvc + Mockito only"], stack, mockmvc=True, line=80.0, branch=70.0)
    if _is_service(symbol, annos):
        return _unit("service-mockito", ["service/component/business class -> Mockito unit test"], stack, reflection=_has_value_field(symbol), line=90.0, branch=85.0)
    if _is_mapper(symbol, annos):
        return TemplateSpec(TestKind.PURE_UNIT, "mapper-unit", [], "", _junit_imports(), forbidden, False, True, 90.0, 80.0, ["mapper/converter -> pure source/target object mapping tests"], "Use real DTO/entity objects; do not mock payloads.")
    if _is_validator(symbol, annos):
        if symbol.fields or symbol.constructors:
            return _unit("validator-mockito", ["validator with collaborators -> Mockito unit test"], stack, line=90.0, branch=85.0)
        return TemplateSpec(TestKind.PURE_UNIT, "validator-unit", [], "", _junit_imports(), forbidden, False, True, 90.0, 85.0, ["validator without collaborators -> pure JUnit"], "Test valid/null/empty/blank/invalid/boundary paths only when source supports them.")
    if _is_utility(symbol):
        return TemplateSpec(TestKind.PURE_UNIT, "utility-unit", [], "", _junit_imports(), forbidden, _returns_reactive(symbol), _has_any_public_logic(symbol), 95.0, 90.0, ["utility/helper/static logic -> pure JUnit"], "Exercise static/direct methods with null, empty, boundary, parse/format success and failure only when source shows those paths.")
    return TemplateSpec(TestKind.PURE_UNIT, "plain-unit", [], "", _junit_imports(), forbidden, _returns_reactive(symbol), _has_any_public_logic(symbol), 85.0, 75.0, ["plain Java class fallback -> pure JUnit"], "Use compile-safe JUnit 5 against explicit public methods only. Do not invent framework behavior.")
