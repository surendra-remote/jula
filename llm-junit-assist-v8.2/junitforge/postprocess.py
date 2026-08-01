"""Deterministic post-processing and rejection gates for generated Java tests."""

from __future__ import annotations

import re
import unicodedata

try:
    import javalang
    from javalang import tree as J
    from javalang.parser import JavaSyntaxError
except Exception:  # pragma: no cover
    javalang = None
    JavaSyntaxError = Exception

    class _MissingTree:
        MethodDeclaration = type("MethodDeclaration", (), {})
        LocalVariableDeclaration = type("LocalVariableDeclaration", (), {})
        FieldDeclaration = type("FieldDeclaration", (), {})
        MethodInvocation = type("MethodInvocation", (), {})

    J = _MissingTree()

from junitforge.models import (
    ClassSymbol,
    Collaborator,
    EqualityAssertionMode,
    FinalizeResult,
    TemplateSpec,
)
from junitforge.parser.java_symbols import resolve_equality_descriptor

_TEST_ANNOS = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"}
_FENCE_BLOCK = re.compile(r"```(?:\s*java)?\s*([\s\S]*?)\s*```", re.IGNORECASE)

_SMART = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u00a0": " ",
    "\u2026": "...",
}

_FORBIDDEN_ANNOTATIONS = {
    "SpringBootTest", "WebMvcTest", "WebFluxTest", "DataJpaTest",
    "ContextConfiguration", "SpringJUnitConfig", "EnableFeignClients",
    "Autowired", "MockBean", "MockitoBean", "RunWith",
}

_ASSERTION_METHODS = {
    "assertEquals", "assertNotEquals", "assertTrue", "assertFalse", "assertNull",
    "assertNotNull", "assertSame", "assertNotSame", "assertThrows", "assertDoesNotThrow",
    "assertIterableEquals", "assertArrayEquals", "fail", "assertAll", "assertInstanceOf",
}

_MOCKITO_METHODS = {
    "when", "verify", "times", "never", "atLeastOnce", "atLeast", "atMost", "eq", "any",
    "anyString", "anyInt", "anyLong", "anyBoolean", "anyList", "anyMap", "isNull",
    "notNull", "doThrow", "doReturn", "doNothing", "doAnswer", "lenient", "verifyNoInteractions",
    "verifyNoMoreInteractions", "mock",
}

_STATIC_IMPORTS = {
    "assertions": "import static org.junit.jupiter.api.Assertions.*;",
    "mockito": "import static org.mockito.Mockito.*;",
    "mockito_matchers": "import static org.mockito.ArgumentMatchers.*;",
    "mockmvc_request": "import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;",
    "mockmvc_result": "import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;",
}




def _is_strict_serviceimpl_symbol(symbol: ClassSymbol | None) -> bool:
    if symbol is None or symbol.kind != "class" or "abstract" in (symbol.modifiers or set()):
        return False
    annos = {a.split(".")[-1] for a in (symbol.annotations or [])}
    impls = {name.split(".")[-1] for name in (symbol.implements or [])}
    return (
        "Service" in annos
        or symbol.name.endswith("ServiceImpl")
        or any(name.endswith("Service") for name in impls)
    )

def finalize_java(
    raw: str,
    *,
    test_package: str,
    test_class_name: str,
    template: TemplateSpec,
    cut_symbol: ClassSymbol | None = None,
    collaborators: list[Collaborator] | None = None,
    source_imports: list[str] | None = None,
) -> FinalizeResult:
    issues: list[str] = []

    text = strip_fences(raw)
    text = strip_garbage(text)
    text = _strip_leading_prose(text)

    if not text.strip():
        return FinalizeResult(ok=False, reason="empty output after cleaning", needs_reask=True)

    text = enforce_package(text, test_package)
    text, dropped = drop_forbidden_imports(text, template.forbidden_imports)
    if dropped:
        issues.append(f"dropped forbidden imports: {', '.join(sorted(dropped))}")

    text = _drop_forbidden_annotation_lines_if_import_only(text)
    text = _normalize_legacy_junit(text)
    text = _normalize_spring_boot3_servlet_imports(text)
    text = _rewrite_invented_cut_setter_injection(text, cut_symbol)
    text = _auto_add_missing_imports(text, source_imports=source_imports)
    text = dedupe_imports(text)
    text = _remove_duplicate_package_lines(text, test_package)

    # Last-mile deterministic stabilization before rejection gates.
    # These rewrites prevent otherwise useful entity/DTO tests from being
    # discarded because the model ignored prompt rules:
    #   1. assertNull(obj.getX()) must explicitly set obj.setX(null) first.
    #   2. Equality assertions outside the resolved identity/value/conservative
    #      strategy are removed instead of preserving a generic template.
    #   3. Unequal-object hashCode inequality is never a valid Java contract test.
    text = _insert_missing_null_setters_before_assertions(text, cut_symbol)
    equality_target = template.style in {"dto-pojo", "entity-pojo"}
    text = _remove_unsafe_two_instance_object_assertions(
        text, cut_symbol, equality_target=equality_target
    )
    text = _remove_test_methods_calling_cut_private_methods(text, cut_symbol)
    text = _normalize_mixed_mockito_matchers(text)

    forbidden_annos = _find_forbidden_annotations(text)
    if forbidden_annos:
        return FinalizeResult(
            ok=False,
            reason=f"forbidden Spring/context annotations present: {', '.join(sorted(forbidden_annos))}",
            needs_reask=True,
            issues=issues,
        )

    custom_servlet_mocks = _custom_servlet_mock_implementations(text)
    if custom_servlet_mocks:
        return FinalizeResult(
            ok=False,
            reason=(
                "custom servlet mock implementation generated; use Mockito mock(...) "
                "or org.springframework.mock.web MockHttpServletResponse/MockHttpServletRequest instead: "
                + ", ".join(custom_servlet_mocks[:10])
            ),
            needs_reask=True,
            issues=issues,
        )

    if test_class_name not in text:
        return FinalizeResult(ok=False, reason=f"expected test class name {test_class_name} not found", needs_reask=True, issues=issues)

    if not _contains_test_annotation(text):
        return FinalizeResult(ok=False, reason="no JUnit test annotation found", needs_reask=True, issues=issues)

    structure = _try_javalang_parse(text)
    if structure.ok:
        parsed_issues, rewritten = _validate_and_maybe_rename_with_ast(text, structure.tree, test_class_name)
        issues.extend(parsed_issues)
        text = rewritten
    elif _looks_structurally_complete(text):
        issues.append(f"lightweight parser skipped: {structure.reason}")
    else:
        return FinalizeResult(ok=False, reason=f"output does not look like a complete Java file: {structure.reason}", needs_reask=True, issues=issues)

    private_calls = _private_method_calls(text, cut_symbol)
    if private_calls:
        return FinalizeResult(ok=False, reason=f"direct private method calls: {', '.join(private_calls[:10])}", needs_reask=True, issues=issues)

    bad_accessors = _invalid_accessor_calls(text, cut_symbol)
    if bad_accessors:
        return FinalizeResult(ok=False, reason=f"invalid/non-field JavaBean accessor calls: {', '.join(bad_accessors[:10])}", needs_reask=True, issues=issues)

    unsafe_nulls = _unsafe_default_null_assertions(text, cut_symbol)
    if unsafe_nulls:
        return FinalizeResult(
            ok=False,
            reason=(
                "unsafe default-null field assertions; set field to null via setter before assertNull: "
                + ", ".join(unsafe_nulls[:10])
            ),
            needs_reask=True,
            issues=issues,
        )

    unsafe_object_methods = _unsafe_entity_object_method_assertions(
        text,
        cut_symbol,
        test_package=test_package,
        equality_target=equality_target,
    )
    if unsafe_object_methods:
        return FinalizeResult(
            ok=False,
            reason=f"fragile entity/DTO object-method assertions: {', '.join(unsafe_object_methods[:10])}",
            needs_reask=True,
            issues=issues,
        )

    strict_serviceimpl = _is_strict_serviceimpl_symbol(cut_symbol)
    suspects = suspect_invented_calls(
        text,
        cut_symbol,
        collaborators,
        strict_cut_public_only=strict_serviceimpl,
    )
    if suspects:
        return FinalizeResult(
            ok=False,
            reason=(
                "suspect invented calls: " + ", ".join(suspects[:20])
                + ("; ServiceImpl may call only source-declared public instance methods" if strict_serviceimpl else "")
            ),
            needs_reask=True,
            issues=issues,
        )

    return FinalizeResult(ok=True, code=text.strip() + "\n", issues=issues)


def strip_fences(text: str) -> str:
    blocks = _FENCE_BLOCK.findall(text or "")
    if blocks:
        java_blocks = [b.strip() for b in blocks if b.strip()]
        if java_blocks:
            text = max(java_blocks, key=len)
    text = re.sub(r"^\s*```(?:\s*java)?\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    return text.replace("```java", "").replace("```", "").strip()


def strip_garbage(text: str) -> str:
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    out: list[str] = []
    for ch in text:
        if ch in ("\t", "\n", "\r"):
            out.append(ch)
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
            continue
        out.append(ch)
    text = "".join(out)
    return re.sub(r"([^\w\s{}()\[\];])\1{40,}", r"\1\1\1", text)


def _strip_leading_prose(text: str) -> str:
    starts = ("package ", "import ", "@", "public ", "final ", "class ", "abstract ", "interface ", "enum ", "record ", "/*", "//")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(starts):
            return "\n".join(lines[i:])
    return text


def enforce_package(text: str, package: str) -> str:
    body = re.sub(r"^\s*package\s+[\w.]+\s*;\s*$", "", text, count=1, flags=re.MULTILINE).lstrip("\n")
    return body if not package else f"package {package};\n\n{body}"


def drop_forbidden_imports(text: str, forbidden: list[str]) -> tuple[str, set[str]]:
    if not forbidden:
        return text, set()
    forbidden_imports = [f for f in forbidden if not f.startswith("@")]
    dropped: set[str] = set()
    kept: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;", line)
        if m:
            imp = m.group(2)
            if any(_matches_forbidden_import(imp, f) for f in forbidden_imports):
                dropped.add(imp)
                continue
        kept.append(line)
    return "\n".join(kept), dropped


def _matches_forbidden_import(imp: str, forbidden: str) -> bool:
    forbidden = forbidden.strip()
    if not forbidden:
        return False
    if forbidden.endswith(".*"):
        base = forbidden[:-2]
        return imp == base or imp.startswith(base + ".")
    return imp == forbidden or imp.startswith(forbidden + ".")


def _drop_forbidden_annotation_lines_if_import_only(text: str) -> str:
    # Do not silently remove class/method annotations; finalize_java rejects them.
    return text


def _normalize_legacy_junit(text: str) -> str:
    text = re.sub(r"import\s+org\.junit\.Test\s*;", "import org.junit.jupiter.api.Test;", text)
    text = re.sub(r"import\s+static\s+org\.junit\.Assert\.\*\s*;", "import static org.junit.jupiter.api.Assertions.*;", text)
    return text


def _normalize_spring_boot3_servlet_imports(text: str) -> str:
    """Normalize accidental legacy servlet package names for Boot 3 tests.

    This is a narrow syntax repair. Custom servlet mock implementations are
    still rejected separately because manually implementing the servlet API is
    fragile and caused controller compile failures.
    """
    return text.replace("javax.servlet.", "jakarta.servlet.")


def _custom_servlet_mock_implementations(text: str) -> list[str]:
    """Detect generated custom servlet API implementations.

    Controller tests must use standalone MockMvc, Mockito mocks, or Spring's
    mock-web classes. They must not generate nested classes that implement or
    extend servlet APIs such as HttpServletResponse.
    """
    bad: set[str] = set()

    servlet_types = (
        "HttpServletResponse",
        "HttpServletRequest",
        "ServletResponse",
        "ServletRequest",
        "FilterChain",
        "ServletOutputStream",
        "PrintWriter",
    )
    type_alt = "|".join(servlet_types)

    for match in re.finditer(
        rf"\bclass\s+(?P<cls>[A-Za-z_$][\w$]*)\s+(?:extends|implements)\s+(?:[\w.]+\.)?(?P<api>{type_alt})\b",
        text,
    ):
        bad.add(f"{match.group('cls')} {match.group('api')}")

    # A nested class named MockHttpServletResponse/Request is almost always an
    # LLM-created fake. The real Spring class should be imported, not declared.
    for name in ("MockHttpServletResponse", "MockHttpServletRequest", "MockFilterChain", "MockServletOutputStream"):
        if re.search(rf"\bclass\s+{re.escape(name)}\b", text):
            bad.add(name)

    # Directly catch the exact bad forms even if the class name is unusual.
    for api in servlet_types:
        if re.search(rf"\b(?:extends|implements)\s+(?:[\w.]+\.)?{api}\b", text):
            bad.add(api)

    return sorted(bad)



def _rewrite_invented_cut_setter_injection(text: str, cut_symbol: ClassSymbol | None) -> str:
    """Rewrite fake setter injection on CUT fields to ReflectionTestUtils.

    Controller-advice/handler classes often have private collaborators such as
    ``Environment env`` and no setter.  The LLM sometimes emits
    ``handler.setEnv(environment);`` even though no such method exists.  That is
    not a valid business-method call; it is dependency injection setup.  When a
    matching non-static field exists and the setter is not explicitly declared,
    rewrite it to:

        ReflectionTestUtils.setField(handler, "env", environment);

    This preserves strict invented-call rejection for real method calls while
    salvaging common no-Spring-context tests for @RestControllerAdvice and
    @ControllerAdvice classes.
    """
    if cut_symbol is None or not cut_symbol.fields:
        return text

    explicit_methods = {m.name for m in (cut_symbol.methods or [])}
    setter_to_field = {
        _setter_name(f.name, f.type): f.name
        for f in (cut_symbol.fields or [])
        if "static" not in (f.modifiers or set())
    }
    if not setter_to_field:
        return text

    cut_vars = _cut_vars_in_body(text, cut_symbol.name)
    if not cut_vars:
        return text

    owner_alt = "|".join(re.escape(v) for v in sorted(cut_vars, key=len, reverse=True))
    setter_alt = "|".join(re.escape(s) for s in sorted(setter_to_field, key=len, reverse=True) if s not in explicit_methods)
    if not owner_alt or not setter_alt:
        return text

    pattern = re.compile(
        rf"\b(?P<owner>{owner_alt})\s*\.\s*(?P<setter>{setter_alt})\s*\(\s*(?P<arg>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\)\s*;"
    )

    def repl(match: re.Match[str]) -> str:
        field_name = setter_to_field.get(match.group("setter"))
        if not field_name:
            return match.group(0)
        return f'ReflectionTestUtils.setField({match.group("owner")}, "{field_name}", {match.group("arg")});'

    return pattern.sub(repl, text)


def _auto_add_missing_imports(text: str, *, source_imports: list[str] | None = None) -> str:
    needed: list[str] = []
    if any(re.search(rf"\b{m}\s*\(", text) for m in _ASSERTION_METHODS) and "org.junit.jupiter.api.Assertions" not in text:
        needed.append(_STATIC_IMPORTS["assertions"])
    if any(re.search(rf"\b{m}\s*\(", text) for m in _MOCKITO_METHODS) and "org.mockito.Mockito" not in text:
        needed.append(_STATIC_IMPORTS["mockito"])
    if any(re.search(r"\b(?:eq|any|anyString|anyInt|anyLong|anyBoolean|anyList|anyMap|isNull|notNull)\s*\(", text) for _ in [0]) and "org.mockito.ArgumentMatchers" not in text and "org.mockito.Mockito.*" not in text:
        needed.append(_STATIC_IMPORTS["mockito_matchers"])
    if re.search(r"\b(?:get|post|put|delete|patch)\s*\(", text) and "MockMvc" in text and "MockMvcRequestBuilders" not in text:
        needed.append(_STATIC_IMPORTS["mockmvc_request"])
    if re.search(r"\b(?:status|content|jsonPath|header)\s*\(", text) and "MockMvc" in text and "MockMvcResultMatchers" not in text:
        needed.append(_STATIC_IMPORTS["mockmvc_result"])

    if "@ExtendWith(MockitoExtension.class)" in text and "org.junit.jupiter.api.extension.ExtendWith" not in text:
        needed.append("import org.junit.jupiter.api.extension.ExtendWith;")
    if "MockitoExtension" in text and "org.mockito.junit.jupiter.MockitoExtension" not in text:
        needed.append("import org.mockito.junit.jupiter.MockitoExtension;")
    if "@Mock" in text and "org.mockito.Mock" not in text:
        needed.append("import org.mockito.Mock;")
    if "@InjectMocks" in text and "org.mockito.InjectMocks" not in text:
        needed.append("import org.mockito.InjectMocks;")
    if "@BeforeEach" in text and "org.junit.jupiter.api.BeforeEach" not in text:
        needed.append("import org.junit.jupiter.api.BeforeEach;")
    if "@Test" in text and "org.junit.jupiter.api.Test" not in text:
        needed.append("import org.junit.jupiter.api.Test;")
    if "ReflectionTestUtils" in text and "org.springframework.test.util.ReflectionTestUtils" not in text:
        needed.append("import org.springframework.test.util.ReflectionTestUtils;")
    if "MockMvcBuilders" in text and "org.springframework.test.web.servlet.setup.MockMvcBuilders" not in text:
        needed.append("import org.springframework.test.web.servlet.setup.MockMvcBuilders;")
    if re.search(r"\bMockMvc\b", text) and "org.springframework.test.web.servlet.MockMvc" not in text:
        needed.append("import org.springframework.test.web.servlet.MockMvc;")

    # Controller servlet helpers. Do not add Spring mock-web imports when the
    # generated file defines its own class with the same name; such custom
    # servlet implementations are rejected by _custom_servlet_mock_implementations.
    if re.search(r"\bnew\s+MockHttpServletResponse\s*\(", text) and "org.springframework.mock.web.MockHttpServletResponse" not in text and not re.search(r"\bclass\s+MockHttpServletResponse\b", text):
        needed.append("import org.springframework.mock.web.MockHttpServletResponse;")
    if re.search(r"\bnew\s+MockHttpServletRequest\s*\(", text) and "org.springframework.mock.web.MockHttpServletRequest" not in text and not re.search(r"\bclass\s+MockHttpServletRequest\b", text):
        needed.append("import org.springframework.mock.web.MockHttpServletRequest;")
    if re.search(r"\bHttpServletResponse\b", text) and "jakarta.servlet.http.HttpServletResponse" not in text and "javax.servlet.http.HttpServletResponse" not in text:
        needed.append("import jakarta.servlet.http.HttpServletResponse;")
    if re.search(r"\bHttpServletRequest\b", text) and "jakarta.servlet.http.HttpServletRequest" not in text and "javax.servlet.http.HttpServletRequest" not in text:
        needed.append("import jakarta.servlet.http.HttpServletRequest;")
    if re.search(r"\bFilterChain\b", text) and "jakarta.servlet.FilterChain" not in text and "javax.servlet.FilterChain" not in text:
        needed.append("import jakarta.servlet.FilterChain;")

    needed.extend(_source_imports_needed(text, source_imports or []))

    unique = []
    seen = set()
    for imp in needed:
        if imp not in text and imp not in seen:
            seen.add(imp)
            unique.append(imp)
    if not unique:
        return text
    return _insert_imports(text, unique)



def _source_imports_needed(text: str, source_imports: list[str]) -> list[str]:
    """Add production enum/type/static imports only when referenced.

    The JavaParser CLI preserves source imports, including static imports as
    strings like ``static com.acme.Constants.FOO``.  This postprocessor uses
    them as an allow-list; it does not invent project imports.
    """
    if not source_imports:
        return []

    existing = set(_existing_import_lines(text))
    code_wo_imports = _strip_import_lines(text)
    additions: list[str] = []

    for raw in source_imports:
        imp = (raw or "").strip().rstrip(";")
        if not imp:
            continue

        is_static = imp.startswith("static ")
        name = imp[len("static "):].strip() if is_static else imp
        if not name or name.startswith("java.lang."):
            continue

        if is_static:
            line = f"import static {name};"
            if line in existing:
                continue
            if name.endswith(".*"):
                # Wildcard static imports are allowed only when the source used
                # them.  Add them when the test contains likely unqualified
                # constants; unused imports are harmless, but this avoids adding
                # project-wide wildcards to every test.
                if _contains_likely_static_constant_reference(code_wo_imports):
                    additions.append(line)
                continue
            member = name.split(".")[-1]
            if re.search(rf"(?<![.\w$]){re.escape(member)}(?![\w$])", code_wo_imports):
                additions.append(line)
            continue

        if name.endswith(".*"):
            # Normal wildcard source imports are not added automatically because
            # they can mask missing type mistakes.  Prefer concrete type imports.
            continue

        simple = name.split(".")[-1]
        line = f"import {name};"
        if line not in existing and re.search(rf"(?<![.\w$]){re.escape(simple)}(?![\w$])", code_wo_imports):
            additions.append(line)

    return additions


def _existing_import_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if re.match(r"\s*import\s+", line)]


def _strip_import_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not re.match(r"\s*import\s+", line))


def _contains_likely_static_constant_reference(code: str) -> bool:
    # Project constants are commonly UPPER_SNAKE_CASE.  Exclude Java keywords
    # and assertion/static-method names by requiring at least one underscore or
    # length >= 4 all-uppercase token.
    for token in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", code):
        if token in {"Test", "Mock", "InjectMocks"}:
            continue
        if "_" in token or len(token) >= 4:
            return True
    return False

def _insert_imports(text: str, imports: list[str]) -> str:
    lines = text.splitlines()
    last_import = -1
    package_idx = -1
    for i, line in enumerate(lines):
        if re.match(r"\s*package\s+", line):
            package_idx = i
        if re.match(r"\s*import\s+", line):
            last_import = i
    insert_at = last_import + 1 if last_import >= 0 else package_idx + 1 if package_idx >= 0 else 0
    if insert_at < len(lines) and lines[insert_at].strip():
        imports = imports + [""]
    lines[insert_at:insert_at] = imports
    return "\n".join(lines)


def dedupe_imports(text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        if re.match(r"\s*import\s+.*;", line):
            key = line.strip()
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out)


def _remove_duplicate_package_lines(text: str, package: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    seen_package = False
    for line in lines:
        if re.match(r"\s*package\s+[\w.]+\s*;", line):
            if seen_package:
                continue
            seen_package = True
            out.append(f"package {package};" if package else "")
            continue
        out.append(line)
    return "\n".join(l for l in out if l != "" or package)


def _find_forbidden_annotations(text: str) -> set[str]:
    found = set()
    for name in _FORBIDDEN_ANNOTATIONS:
        if re.search(rf"@\s*(?:[\w.]+\.)?{name}\b", text):
            found.add(name)
    return found


def _contains_test_annotation(text: str) -> bool:
    return bool(re.search(r"@\s*(?:org\.junit\.jupiter\.api\.)?(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b", text))


def _looks_structurally_complete(text: str) -> bool:
    return "class " in text and "{" in text and "}" in text and _contains_test_annotation(text)


class _ParseResult:
    def __init__(self, ok: bool, tree=None, reason: str = ""):
        self.ok = ok
        self.tree = tree
        self.reason = reason


def _try_javalang_parse(text: str) -> _ParseResult:
    if javalang is None:
        return _ParseResult(False, None, "javalang unavailable")
    try:
        return _ParseResult(True, javalang.parse.parse(text))
    except (JavaSyntaxError, Exception) as exc:
        return _ParseResult(False, None, f"{type(exc).__name__}: {str(exc)[:160]}")


def _validate_and_maybe_rename_with_ast(text: str, tree, test_class_name: str) -> tuple[list[str], str]:
    issues: list[str] = []
    types = list(tree.types or [])
    if not types:
        issues.append("no top-level type found by lightweight parser")
        return issues, text
    public_types = [t for t in types if "public" in (t.modifiers or set())]
    if len(public_types) > 1:
        issues.append(f"{len(public_types)} public top-level types found")
    main_type = public_types[0] if public_types else types[0]
    if getattr(main_type, "name", None) and main_type.name != test_class_name and "public" in (main_type.modifiers or set()):
        text = _rename_public_class_declaration(text, main_type.name, test_class_name)
        issues.append(f"renamed public class {main_type.name} -> {test_class_name}")
    if not _has_test_method(main_type):
        issues.append("lightweight parser did not find @Test method, regex check passed")
    return issues, text


def _rename_public_class_declaration(text: str, old: str, new: str) -> str:
    return re.sub(rf"\bpublic\s+(final\s+)?class\s+{re.escape(old)}\b", lambda m: m.group(0).replace(old, new), text, count=1)


def _has_test_method(type_node) -> bool:
    for member in getattr(type_node, "body", None) or []:
        if isinstance(member, J.MethodDeclaration):
            for a in getattr(member, "annotations", None) or []:
                if getattr(a, "name", "").split(".")[-1] in _TEST_ANNOS:
                    return True
        elif type(member).__name__.endswith("Declaration") and hasattr(member, "body"):
            if _has_test_method(member):
                return True
    return False


def _remove_test_methods_calling_cut_private_methods(code: str, cut_symbol: ClassSymbol | None) -> str:
    """Remove generated @Test methods that directly call CUT private helpers.

    This is a salvage step.  If the LLM generated three useful ServiceImpl tests
    and one invalid private-helper test, keep the useful tests instead of
    failing the whole class at postprocess time.  Remaining private calls are
    still rejected by _private_method_calls.
    """
    if cut_symbol is None:
        return code

    private_names = {m.name for m in (cut_symbol.methods or []) if "private" in m.modifiers}
    if not private_names:
        return code

    changed = True
    out = code
    while changed:
        changed = False
        for test_match in list(re.finditer(r"@\s*(?:org\.junit\.jupiter\.api\.)?Test\b", out)):
            start = _annotation_block_start(out, test_match.start())
            open_idx = out.find("{", test_match.end())
            if open_idx < 0:
                continue
            close_idx = _matching_brace(out, open_idx)
            if close_idx <= open_idx:
                continue

            method_block = out[start:close_idx + 1]
            if _block_calls_cut_private_method(method_block, cut_symbol, private_names):
                out = out[:start].rstrip() + "\n\n" + out[close_idx + 1:].lstrip("\n")
                changed = True
                break

    return out


def _annotation_block_start(code: str, test_annotation_start: int) -> int:
    """Return start of an annotation block above a generated test method."""
    line_start = code.rfind("\n", 0, test_annotation_start) + 1
    pos = line_start
    # Include simple annotations immediately above @Test, for example
    # @DisplayName or @SuppressWarnings.  Stop at blank lines or method/class text.
    while True:
        prev_end = pos - 1
        if prev_end <= 0:
            return pos
        prev_start = code.rfind("\n", 0, prev_end) + 1
        prev_line = code[prev_start:prev_end].strip()
        if not prev_line.startswith("@"):
            return pos
        pos = prev_start


def _block_calls_cut_private_method(block: str, cut_symbol: ClassSymbol, private_names: set[str]) -> bool:
    cut_vars = _cut_vars_in_body(block, cut_symbol.name)
    owner_tokens = set(cut_vars) | {cut_symbol.name}

    for name in private_names:
        if re.search(rf"ReflectionTestUtils\.invokeMethod\([^;]*\"{re.escape(name)}\"", block, flags=re.S):
            return True
        for owner in owner_tokens:
            if re.search(rf"\b{re.escape(owner)}\s*\.\s*{re.escape(name)}\s*\(", block):
                return True
        if re.search(rf"(?<![.\w$]){re.escape(name)}\s*\(", block):
            return True
    return False


_MOCKITO_MATCHER_CALLS = {
    "any", "anyString", "anyInt", "anyLong", "anyBoolean", "anyDouble", "anyFloat",
    "anyShort", "anyByte", "anyChar", "anyList", "anyMap", "anySet", "anyCollection",
    "anyIterable", "isNull", "notNull", "nullable", "eq", "same", "refEq",
    "argThat", "booleanThat", "byteThat", "charThat", "doubleThat", "floatThat",
    "intThat", "longThat", "shortThat", "contains", "startsWith", "endsWith", "matches",
}


def _normalize_mixed_mockito_matchers(code: str) -> str:
    """Repair common Mockito matcher misuse in generated ServiceImpl tests.

    Mockito requires all arguments in a single mocked method invocation to be
    matchers when any argument is a matcher.  The LLM commonly emits:

        when(client.call("123", any())).thenReturn(...)
        verify(client).call("123", any())

    which throws InvalidUseOfMatchersException at runtime.  This deterministic
    pass rewrites only simple method invocations that already contain Mockito
    matchers, wrapping raw sibling arguments with eq(...):

        when(client.call(eq("123"), any())).thenReturn(...)

    It does not introduce any()/anyString(); it only makes existing matcher
    usage internally consistent.
    """
    if not any(f"{name}(" in code for name in _MOCKITO_MATCHER_CALLS):
        return code

    code = _normalize_chained_mockito_matcher_calls(code)

    out: list[str] = []
    i = 0
    n = len(code)
    while i < n:
        m = re.search(r"\b[A-Za-z_$][\w$]*\s*\.\s*[A-Za-z_$][\w$]*\s*\(", code[i:])
        if not m:
            out.append(code[i:])
            break
        open_idx = i + m.end() - 1
        close_idx = _matching_paren(code, open_idx)
        if close_idx < 0:
            out.append(code[i:])
            break

        out.append(code[i:open_idx + 1])
        args = code[open_idx + 1:close_idx]
        out.append(_normalize_matcher_args(args))
        i = close_idx
    return "".join(out)


def _normalize_chained_mockito_matcher_calls(code: str) -> str:
    """Normalize verify(mock).method(...) and when(mock).method(...)."""
    out: list[str] = []
    i = 0
    pattern = re.compile(r"\b(?:verify|when)\s*\([^;\n]*?\)\s*\.\s*[A-Za-z_$][\w$]*\s*\(")
    while i < len(code):
        m = pattern.search(code, i)
        if not m:
            out.append(code[i:])
            break
        open_idx = m.end() - 1
        close_idx = _matching_paren(code, open_idx)
        if close_idx < 0:
            out.append(code[i:])
            break
        out.append(code[i:open_idx + 1])
        args = code[open_idx + 1:close_idx]
        out.append(_normalize_matcher_args(args))
        i = close_idx
    return "".join(out)


def _matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    line_comment = False
    block_comment = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            if ch in "\r\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if in_char:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_char = False
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "'":
            in_char = True
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _normalize_matcher_args(args: str) -> str:
    parts = _split_top_level_args(args)
    if len(parts) < 2:
        return args
    if not any(_is_mockito_matcher_arg(p.strip()) for p in parts):
        return args
    rebuilt: list[str] = []
    changed = False
    for part in parts:
        stripped = part.strip()
        if not stripped or _is_mockito_matcher_arg(stripped):
            rebuilt.append(part)
            continue
        wrapped = _wrap_arg_with_eq_preserving_ws(part)
        rebuilt.append(wrapped)
        changed = True
    return ",".join(rebuilt) if changed else args


def _split_top_level_args(args: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth_paren = depth_bracket = depth_brace = 0
    in_string = in_char = escaped = False
    i = 0
    while i < len(args):
        ch = args[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if in_char:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_char = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch == "'":
            in_char = True
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket = max(0, depth_bracket - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
        elif ch == "," and depth_paren == depth_bracket == depth_brace == 0:
            parts.append(args[start:i])
            start = i + 1
        i += 1
    parts.append(args[start:])
    return parts


def _is_mockito_matcher_arg(arg: str) -> bool:
    return bool(re.match(r"(?:org\.mockito\.ArgumentMatchers\.)?(?:" + "|".join(sorted(map(re.escape, _MOCKITO_MATCHER_CALLS), key=len, reverse=True)) + r")\s*\(", arg))


def _wrap_arg_with_eq_preserving_ws(arg: str) -> str:
    prefix = re.match(r"^\s*", arg).group(0)
    suffix = re.search(r"\s*$", arg).group(0)
    core = arg[len(prefix): len(arg) - len(suffix) if suffix else len(arg)]
    if not core:
        return arg
    # Do not wrap lambdas or already complex Mockito verification mode fragments.
    if "->" in core:
        return arg
    return f"{prefix}eq({core}){suffix}"


def _private_method_calls(code: str, cut_symbol: ClassSymbol | None) -> list[str]:
    """Detect direct calls to private methods on the class under test only.

    ServiceImpl tests often create DTO/entity/response helper objects.  Earlier
    versions scanned for any ``.privateName(...)`` occurrence anywhere in the
    generated file.  That was too broad and could reject valid tests when a
    helper object happened to have the same method name.

    The gate remains strict for the CUT itself:
    - service.privateHelper(...) is invalid
    - ClassName.privateStaticHelper(...) is invalid
    - ReflectionTestUtils.invokeMethod(..., "privateHelper", ...) is invalid
    """
    if cut_symbol is None:
        return []

    private_names = {m.name for m in (cut_symbol.methods or []) if "private" in m.modifiers}
    if not private_names:
        return []

    cut_vars = _cut_vars_in_body(code, cut_symbol.name)
    owner_tokens = set(cut_vars) | {cut_symbol.name}

    bad: set[str] = set()
    for name in private_names:
        if re.search(rf"ReflectionTestUtils\.invokeMethod\([^;]*\"{re.escape(name)}\"", code, flags=re.S):
            bad.add(name)
            continue

        for owner in owner_tokens:
            if re.search(rf"\b{re.escape(owner)}\s*\.\s*{re.escape(name)}\s*\(", code):
                bad.add(name)
                break

        # Bare privateHelper(...) inside a test class is also invalid because
        # the test class is not the CUT and cannot call the CUT private method.
        if re.search(rf"(?<![.\w$]){re.escape(name)}\s*\(", code):
            bad.add(name)

    return sorted(bad)


def _invalid_accessor_calls(code: str, cut_symbol: ClassSymbol | None) -> list[str]:
    """Reject JavaBean accessor calls only when they target the CUT itself.

    Earlier versions scanned every ``.getX()/setX()/isX()`` call in the
    generated test.  That is safe for entity-only tests, but it is wrong for
    ServiceImpl tests because service tests normally create DTO/entity/response
    objects and call their accessors.  Those helper/return-object fields are
    not fields of the ServiceImpl under test, so global scanning produced:

        invalid/non-field JavaBean accessor calls

    for almost every ServiceImpl.

    The correct gate is narrower: validate accessor names only when the receiver
    variable is statically declared as the class under test.  Accessors on
    DTOs, entities, response objects, request objects, mocks, and local helper
    objects are left to Java compilation.
    """
    if cut_symbol is None or not cut_symbol.fields:
        return []

    cut_vars = _cut_vars_in_body(code, cut_symbol.name)
    if not cut_vars:
        return []

    # Start with explicit source methods only.  Do not treat every private
    # field as a JavaBean property.  This is important for @Component utility
    # classes with private Spring @Value fields: those fields are configuration
    # inputs, not testable properties, and most such classes do not declare
    # getX()/setX() methods.
    explicit_methods = {m.name for m in (cut_symbol.methods or [])}
    allowed = {"getClass"} | explicit_methods
    if _is_exception_like_symbol(cut_symbol):
        # Throwable/Exception inherited methods such as getMessage() are real
        # public API even though JavaParser does not list them on the subclass.
        # Do not reject CustomException tests that assert constructor messages.
        allowed |= _THROWABLE_INHERITED_METHODS

    annos = {a.split(".")[-1] for a in (cut_symbol.annotations or [])}
    class_has_data = "Data" in annos
    class_has_getter = class_has_data or "Getter" in annos or "Value" in annos
    class_has_setter = class_has_data or "Setter" in annos

    for f in cut_symbol.fields or []:
        if "static" in f.modifiers:
            continue

        field_annos = {a.split(".")[-1] for a in (f.annotations or [])}

        # Allow Lombok-synthesized accessors only when Lombok evidence exists.
        # Field-level Spring @Value is intentionally not evidence of a getter or
        # setter.  If the class really has a getter/setter, JavaParser lists it
        # in explicit_methods above.
        if class_has_getter or "Getter" in field_annos:
            allowed.add(_getter_name(f.name, f.type))
        if class_has_setter or "Setter" in field_annos:
            allowed.add(_setter_name(f.name, f.type))

    bad = set()
    owner_pattern = "|".join(re.escape(v) for v in sorted(cut_vars, key=len, reverse=True))
    for match in re.finditer(
        rf"\b(?P<owner>{owner_pattern})\s*\.\s*(?P<name>(?:get|set|is)[A-Z][A-Za-z0-9_$]*)\s*\(",
        code,
    ):
        name = match.group("name")
        if name not in allowed:
            bad.add(name)
    return sorted(bad)


def _looks_like_accessor_for_cut(code: str, call_start: int) -> bool:
    # Avoid rejecting MockMvc status()/jsonPath() and collaborator accessors too aggressively.
    prefix = code[max(0, call_start - 80):call_start]
    return not any(x in prefix for x in ("mockMvc", "response", "result", "json", "status"))


def _getter_name(field_name: str, field_type: str | None) -> str:
    if (field_type or "").strip() == "boolean":
        return f"is{_bean_base(field_name, field_type)}"
    return f"get{_java_bean_suffix(field_name)}"


def _setter_name(field_name: str, field_type: str | None) -> str:
    return f"set{_bean_base(field_name, field_type)}"


def _bean_base(field_name: str, field_type: str | None) -> str:
    if (field_type or "").strip() == "boolean" and field_name.startswith("is") and len(field_name) > 2 and field_name[2].isupper():
        return field_name[2:]
    return _java_bean_suffix(field_name)


def _java_bean_suffix(field_name: str) -> str:
    if not field_name:
        return ""
    if len(field_name) >= 2 and field_name[0].isupper() and field_name[1].isupper():
        return field_name
    return field_name[0].upper() + field_name[1:]


_PRIMITIVE_TYPES = {"boolean", "byte", "short", "int", "long", "float", "double", "char"}


def _normalized_type_name(type_name: str | None) -> str:
    """Return a comparison-safe simple Java type name.

    Field and method signatures may use either simple or fully-qualified names.
    Generic suffixes do not affect fluent-mutator matching for a single field.
    """
    text = (type_name or "").strip()
    text = re.sub(r"<.*>", "", text)
    text = text.replace("...", "[]")
    return text.rsplit(".", 1)[-1]


def _null_mutator_names(cut_symbol: ClassSymbol, field) -> tuple[str, ...]:
    """Return verified methods that can explicitly assign null to ``field``.

    Supports both conventional JavaBean setters (``setPolicyPlanType``) and
    fluent setters (``policyPlanType``) that return the CUT instance.  The
    latter is common in OpenAPI-generated DTOs and was the cause of the v6
    ``unsafe default-null`` false rejection.
    """
    expected_type = _normalized_type_name(field.type)
    standard = _setter_name(field.name, field.type)
    candidates: list[str] = []
    for method in cut_symbol.methods or []:
        if method.is_static or len(method.params) != 1:
            continue
        parameter_type, _ = method.params[0]
        if _normalized_type_name(parameter_type) != expected_type:
            continue
        if method.name == standard:
            candidates.append(method.name)
            continue
        if method.name == field.name:
            return_type = _normalized_type_name(method.return_type)
            if return_type in {"", "void", cut_symbol.name, _normalized_type_name(cut_symbol.fqcn)}:
                candidates.append(method.name)
    # Preserve deterministic preference: JavaBean setter first, fluent setter second.
    return tuple(dict.fromkeys(sorted(candidates, key=lambda name: (name != standard, name))))


def _insert_missing_null_setters_before_assertions(code: str, cut_symbol: ClassSymbol | None) -> str:
    """Insert an explicit null write before default-null assertions.

    Handles JavaBean setters and fluent mutators, including assertions nested in
    ``assertAll`` and common multiline/message-overload formatting.  The rewrite
    remains source-grounded: a call is inserted only when the CUT exposes a
    verified one-argument mutator for the exact field type.
    """
    if cut_symbol is None or not cut_symbol.fields:
        return code

    nullable_fields = [
        field for field in cut_symbol.fields
        if "static" not in field.modifiers
        and (field.type or "").strip() not in _PRIMITIVE_TYPES
    ]
    field_contracts = [
        (field, _getter_name(field.name, field.type), _null_mutator_names(cut_symbol, field))
        for field in nullable_fields
    ]
    field_contracts = [item for item in field_contracts if item[2]]
    if not field_contracts:
        return code

    lines = code.splitlines(keepends=True)
    insertions: dict[int, list[str]] = {}
    latest_test_line = 0

    for index, line in enumerate(lines):
        if "@Test" in line:
            latest_test_line = index
        if "assertNull" not in line or line.lstrip().startswith("//"):
            continue

        # Capture a bounded assertion statement so multiline assertNull calls and
        # assertAll(() -> assertNull(...)) are repaired as one unit.
        statement_lines = [line]
        cursor = index
        while ";" not in "".join(statement_lines) and cursor + 1 < len(lines) and cursor - index < 12:
            cursor += 1
            statement_lines.append(lines[cursor])
        statement = "".join(statement_lines)
        body_prefix = "".join(lines[latest_test_line:index])
        indent = re.match(r"^[ \t]*", line).group(0)

        for field, getter, mutators in field_contracts:
            for match in re.finditer(
                rf"\b([A-Za-z_$][\w$]*)\s*\.\s*{re.escape(getter)}\s*\(\s*\)",
                statement,
            ):
                var_name = match.group(1)
                already_written = any(
                    re.search(
                        rf"\b{re.escape(var_name)}\s*\.\s*{re.escape(name)}\s*\(\s*null\s*\)",
                        body_prefix,
                    )
                    for name in mutators
                )
                if already_written:
                    continue
                call = f"{indent}{var_name}.{mutators[0]}(null);\n"
                insertions.setdefault(index, [])
                if call not in insertions[index]:
                    insertions[index].append(call)
                body_prefix += call

    if not insertions:
        return code

    out: list[str] = []
    for index, line in enumerate(lines):
        out.extend(insertions.get(index, ()))
        out.append(line)
    return "".join(out)


def _remove_unsafe_two_instance_object_assertions(
    code: str,
    cut_symbol: ClassSymbol | None,
    *,
    equality_target: bool = False,
) -> str:
    """Drop only assertions forbidden by the effective equality strategy."""
    if cut_symbol is None:
        return code
    if equality_target:
        mode = _equality_mode(cut_symbol)
    else:
        # Preserve behavior outside the DTO/entity equality phase.
        if _has_equality_contract(cut_symbol):
            return code
        mode = EqualityAssertionMode.CONSERVATIVE

    lines = code.splitlines()
    current_method_lines: list[str] = []
    method_start_index: int | None = None
    out_lines: list[str] = []
    in_test_method = False
    brace_depth = 0

    for line in lines:
        if not in_test_method:
            out_lines.append(line)
            if re.search(r"@\s*(?:org\.junit\.jupiter\.api\.)?Test\b", line):
                in_test_method = True
                method_start_index = len(out_lines) - 1
                current_method_lines = []
                brace_depth = 0
            continue

        current_method_lines.append(line)
        brace_depth += line.count("{") - line.count("}")
        if brace_depth <= 0 and "}" in line:
            # Re-process the just-captured method block, keeping the @Test line
            # already in out_lines and replacing the following method lines.
            if method_start_index is not None:
                del out_lines[method_start_index + 1:]
                method_text = "\n".join(current_method_lines)
                method_text = _strip_unsafe_two_instance_lines_from_method(
                    method_text,
                    cut_symbol.name,
                    mode,
                    enforce_hash_contract=equality_target,
                )
                out_lines.extend(method_text.splitlines())
            in_test_method = False
            method_start_index = None
            current_method_lines = []
            brace_depth = 0

    if in_test_method and current_method_lines:
        method_text = "\n".join(current_method_lines)
        method_text = _strip_unsafe_two_instance_lines_from_method(
            method_text,
            cut_symbol.name,
            mode,
            enforce_hash_contract=equality_target,
        )
        if method_start_index is not None:
            del out_lines[method_start_index + 1:]
            out_lines.extend(method_text.splitlines())
    return "\n".join(out_lines)


def _strip_unsafe_two_instance_lines_from_method(
    method_text: str,
    cut_name: str,
    mode: EqualityAssertionMode,
    *,
    enforce_hash_contract: bool,
) -> str:
    vars_for_cut = _cut_vars_in_body(method_text, cut_name)
    if not vars_for_cut:
        return method_text
    var_alt = "|".join(re.escape(v) for v in sorted(vars_for_cut, key=len, reverse=True))

    cleaned: list[str] = []
    for line in method_text.splitlines():
        # Remove simple one-line assertions only. Complex multiline cases remain
        # visible to the rejection gate for safety.
        object_assertion = re.search(
            rf"\bassert(?P<kind>Equals|NotEquals)\s*\(\s*"
            rf"(?P<left>{var_alt})\s*,\s*(?P<right>{var_alt})\s*\)\s*;",
            line,
        )
        if object_assertion and object_assertion.group("left") != object_assertion.group("right"):
            kind = object_assertion.group("kind")
            if mode is EqualityAssertionMode.CONSERVATIVE:
                continue
            if mode is EqualityAssertionMode.IDENTITY and kind == "Equals":
                continue

        hash_assertion = re.search(
            rf"\bassert(?P<kind>Equals|NotEquals)\s*\(\s*"
            rf"(?P<left>{var_alt})\s*\.\s*hashCode\s*\(\s*\)\s*,\s*"
            rf"(?P<right>{var_alt})\s*\.\s*hashCode\s*\(\s*\)\s*\)\s*;",
            line,
        )
        if hash_assertion:
            left = hash_assertion.group("left")
            right = hash_assertion.group("right")
            if hash_assertion.group("kind") == "NotEquals":
                if enforce_hash_contract:
                    # Unequal values are permitted to collide.
                    continue
                cleaned.append(line)
                continue
            if left != right and mode is not EqualityAssertionMode.VALUE:
                continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _unsafe_default_null_assertions(code: str, cut_symbol: ClassSymbol | None) -> list[str]:
    """Reject brittle POJO null tests that assert constructor defaults.

    Enterprise entities often have field initializers, builder defaults, or
    constructor normalization.  A stable null-field test must explicitly call
    setX(null) before assertNull(getX()).
    """
    if cut_symbol is None or not cut_symbol.fields:
        return []

    nullable_fields = [
        f for f in cut_symbol.fields
        if "static" not in f.modifiers and (f.type or "").strip() not in _PRIMITIVE_TYPES
    ]
    if not nullable_fields:
        return []

    bad: set[str] = set()
    for body in _test_method_bodies(code) or [code]:
        for field in nullable_fields:
            getter = _getter_name(field.name, field.type)
            mutators = _null_mutator_names(cut_symbol, field)
            for m in re.finditer(rf"\bassertNull\s*\(\s*([A-Za-z_$][\w$]*)\s*\.\s*{re.escape(getter)}\s*\(\s*\)\s*\)", body):
                var_name = m.group(1)
                before_assert = body[:m.start()]
                if not mutators or not any(
                    re.search(rf"\b{re.escape(var_name)}\s*\.\s*{re.escape(name)}\s*\(\s*null\s*\)", before_assert)
                    for name in mutators
                ):
                    bad.add(f"{field.name} via {getter}()")
    return sorted(bad)


def _unsafe_entity_object_method_assertions(
    code: str,
    cut_symbol: ClassSymbol | None,
    *,
    test_package: str | None = None,
    equality_target: bool = False,
) -> list[str]:
    """Reject only provably unsafe object-method assertions.

    Earlier versions rejected all two-instance equality/hashCode tests and all
    canEqual calls. That protected compile/runtime stability, but it also blocked
    valid coverage for manual equals/hashCode, Lombok @Data/@Value/
    @EqualsAndHashCode, package-accessible canEqual, and stable toString.
    """
    if cut_symbol is None:
        return []

    bad: set[str] = set()
    equality_mode = (
        _equality_mode(cut_symbol)
        if equality_target
        else (
            EqualityAssertionMode.VALUE
            if _has_equality_contract(cut_symbol)
            else EqualityAssertionMode.CONSERVATIVE
        )
    )
    allow_strict_to_string = _has_to_string_contract(cut_symbol)
    allow_can_equal = _can_call_can_equal(cut_symbol, test_package)

    if re.search(r"\.\s*canEqual\s*\(", code) and not allow_can_equal:
        bad.add("direct canEqual() call without package-accessible canEqual contract")

    for body in _test_method_bodies(code) or [code]:
        vars_for_cut = _cut_vars_in_body(body, cut_symbol.name)
        if not vars_for_cut:
            continue
        var_alt = "|".join(re.escape(v) for v in sorted(vars_for_cut, key=len, reverse=True))

        for m in re.finditer(
            rf"\bassert(?P<kind>Equals|NotEquals)\s*\(\s*"
            rf"(?P<left>{var_alt})\s*,\s*(?P<right>{var_alt})\s*\)",
            body,
        ):
            if m.group("left") == m.group("right"):
                continue
            if equality_mode is EqualityAssertionMode.CONSERVATIVE:
                bad.add("independent two-instance assertion with unresolved equality basis")
            elif (
                equality_mode is EqualityAssertionMode.IDENTITY
                and m.group("kind") == "Equals"
            ):
                bad.add("independent same-value equality assertion for Object identity")

        for m in re.finditer(
            rf"\bassertEquals\s*\(\s*(?P<left>{var_alt})\s*\.\s*"
            rf"hashCode\s*\(\s*\)\s*,\s*(?P<right>{var_alt})\s*\.\s*"
            rf"hashCode\s*\(\s*\)\s*\)",
            body,
        ):
            if (
                m.group("left") != m.group("right")
                and equality_mode is not EqualityAssertionMode.VALUE
            ):
                bad.add("two-instance hashCode comparison without resolved value equality")

        if equality_target:
            if re.search(
                rf"\bassertNotEquals\s*\(\s*(?:{var_alt})\s*\.\s*"
                rf"hashCode\s*\(\s*\)\s*,\s*(?:{var_alt})\s*\.\s*"
                rf"hashCode\s*\(\s*\)",
                body,
                flags=re.S,
            ):
                bad.add("unequal objects required to have different hash codes")
            if re.search(
                rf"\bassertTrue\s*\([^;]*(?:{var_alt})\s*\.\s*hashCode"
                rf"\s*\(\s*\)\s*!=\s*(?:{var_alt})\s*\.\s*hashCode",
                body,
                flags=re.S,
            ) or re.search(
                rf"\bassertFalse\s*\([^;]*(?:{var_alt})\s*\.\s*hashCode"
                rf"\s*\(\s*\)\s*==\s*(?:{var_alt})\s*\.\s*hashCode",
                body,
                flags=re.S,
            ):
                bad.add("unequal objects required to have different hash codes")

        if re.search(r"\bassertTrue\s*\([^;]*toString\s*\(\s*\)\.\s*contains\s*\(", body, flags=re.S) and not allow_strict_to_string:
            bad.add("strict toString contains assertion without toString contract")
        for string_var, owner in re.findall(rf"\bString\s+([A-Za-z_$][\w$]*)\s*=\s*({var_alt})\s*\.\s*toString\s*\(\s*\)\s*;", body):
            if re.search(rf"\bassertTrue\s*\([^;]*\b{re.escape(string_var)}\s*\.\s*contains\s*\(", body, flags=re.S) and not allow_strict_to_string:
                bad.add("strict toString contains assertion without toString contract")

    return sorted(bad)


def _equality_mode(symbol: ClassSymbol) -> EqualityAssertionMode:
    descriptor = getattr(symbol, "equality_descriptor", None)
    if descriptor is None:
        descriptor = resolve_equality_descriptor(symbol)
        symbol.equality_descriptor = descriptor
    return descriptor.assertion_mode


def _method_annotations(method) -> set[str]:
    return {a.split(".")[-1] for a in (getattr(method, "annotations", None) or [])}


def _class_annotations(symbol: ClassSymbol | None) -> set[str]:
    return {a.split(".")[-1] for a in ((symbol.annotations if symbol else []) or [])}


def _has_method_contract(symbol: ClassSymbol | None, name: str, *, include_lombok: bool = True) -> bool:
    if symbol is None:
        return False
    for method in symbol.methods or []:
        if method.name != name:
            continue
        if include_lombok:
            return True
        if "LombokGenerated" not in _method_annotations(method):
            return True
    return False


def _has_equality_contract(symbol: ClassSymbol | None) -> bool:
    annos = _class_annotations(symbol)
    return (
        _has_method_contract(symbol, "equals")
        or _has_method_contract(symbol, "hashCode")
        or bool(annos & {"Data", "Value", "EqualsAndHashCode"})
    )


def _has_to_string_contract(symbol: ClassSymbol | None) -> bool:
    annos = _class_annotations(symbol)
    return _has_method_contract(symbol, "toString") or bool(annos & {"Data", "Value", "ToString"})


def _can_call_can_equal(symbol: ClassSymbol | None, test_package: str | None) -> bool:
    if symbol is None:
        return False
    methods = [m for m in (symbol.methods or []) if m.name == "canEqual"]
    if not methods:
        return False
    for method in methods:
        if "private" in method.modifiers:
            continue
        if "public" in method.modifiers:
            return True
        if "protected" in method.modifiers:
            if symbol.fqcn and "." in symbol.fqcn:
                source_pkg = symbol.fqcn.rsplit(".", 1)[0]
                if source_pkg == (test_package or ""):
                    return True
    return False


def _cut_vars_in_body(body: str, cut_name: str) -> set[str]:
    if not cut_name:
        return set()
    vars_found: set[str] = set()
    # Local declarations: EntityA a = new EntityA(); / final EntityA a = ...
    for m in re.finditer(rf"\b(?:final\s+)?{re.escape(cut_name)}(?:\s*<[^;=()]+>)?\s+([A-Za-z_$][\w$]*)\b", body):
        vars_found.add(m.group(1))
    return vars_found


def _test_method_bodies(code: str) -> list[str]:
    bodies: list[str] = []
    for m in re.finditer(r"@\s*(?:org\.junit\.jupiter\.api\.)?Test\b", code):
        open_idx = code.find("{", m.end())
        if open_idx < 0:
            continue
        close_idx = _matching_brace(code, open_idx)
        if close_idx > open_idx:
            bodies.append(code[open_idx + 1:close_idx])
    return bodies


def _matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    line_comment = False
    block_comment = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if line_comment:
            if ch in "\r\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if in_char:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_char = False
            i += 1
            continue

        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "'":
            in_char = True
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


_THROWABLE_INHERITED_METHODS: set[str] = {
    "getMessage", "getLocalizedMessage", "getCause", "initCause",
    "toString", "printStackTrace", "fillInStackTrace", "getStackTrace",
    "setStackTrace", "addSuppressed", "getSuppressed",
}


def _is_exception_like_symbol(symbol: ClassSymbol | None) -> bool:
    if symbol is None:
        return False
    ext = ((symbol.extends or "").split(".")[-1]).strip()
    return ext in {"Throwable", "Exception", "RuntimeException", "Error"} or symbol.name.endswith(("Exception", "Error"))


def _allowed_cut_method_names(cut_symbol: ClassSymbol, *, strict_public_only: bool = False) -> set[str]:
    """Allowed CUT method names for invention checks.

    JavaParser sees source methods only; Lombok-generated accessors are absent
    from the AST.  The postprocessor must still allow those calls when Lombok
    annotations are present, while continuing to reject DB-column-derived or
    cross-class leaked accessors through _invalid_accessor_calls().
    """
    if strict_public_only:
        names = {
            method.name
            for method in (cut_symbol.methods or [])
            if method.is_public
            and not method.is_static
            and not method.is_constructor
            and "LombokGenerated" not in {a.split(".")[-1] for a in (method.annotations or [])}
        }
        # Object methods are real but are not business generation targets. Keep
        # them allowed so harmless assertions do not become false positives.
        names |= {"toString", "equals", "hashCode", "getClass"}
        return names

    names = cut_symbol.all_method_names() | {"toString", "equals", "hashCode", "getClass"}
    if _is_exception_like_symbol(cut_symbol):
        names |= _THROWABLE_INHERITED_METHODS
    annos = {a.split(".")[-1] for a in (cut_symbol.annotations or [])}
    has_data = "Data" in annos
    has_getter = has_data or "Getter" in annos or "Value" in annos
    has_setter = has_data or "Setter" in annos

    for field in cut_symbol.fields or []:
        if "static" in field.modifiers:
            continue
        field_annos = {a.split(".")[-1] for a in (field.annotations or [])}
        if has_getter or "Getter" in field_annos:
            names.add(_getter_name(field.name, field.type))
        if has_setter or "Setter" in field_annos:
            names.add(_setter_name(field.name, field.type))
    return names


_SPRING_DATA_REPOSITORY_METHODS: set[str] = {
    # CrudRepository / ListCrudRepository
    "save", "saveAll", "findById", "existsById", "findAll", "findAllById",
    "count", "deleteById", "delete", "deleteAllById", "deleteAllByIdInBatch",
    "deleteAll",
    # JpaRepository
    "flush", "saveAndFlush", "saveAllAndFlush", "deleteInBatch",
    "deleteAllInBatch", "getOne", "getById", "getReferenceById",
    # QueryByExampleExecutor common inherited methods
    "findOne", "exists",
}



_SPRING_ENVIRONMENT_METHODS: set[str] = {
    # org.springframework.core.env.Environment / PropertyResolver
    "getProperty", "getRequiredProperty", "containsProperty",
    "resolvePlaceholders", "resolveRequiredPlaceholders",
    "getActiveProfiles", "getDefaultProfiles", "acceptsProfiles",
}


def _is_environment_collaborator(c: Collaborator) -> bool:
    simple = (c.simple or "").split(".")[-1]
    fqcn = c.fqcn or ""
    return simple == "Environment" or fqcn == "org.springframework.core.env.Environment" or fqcn.endswith(".Environment")


def _is_repository_collaborator(c: Collaborator) -> bool:
    simple = (c.simple or "").split(".")[-1]
    fqcn = c.fqcn or ""
    text = f"{simple} {fqcn}".lower()
    return simple.endswith("Repository") or ".repository." in fqcn.lower() or "jparepository" in text or "crudrepository" in text


def _allowed_collaborator_method_names(c: Collaborator) -> set[str]:
    names = {m.name for m in c.signatures} | {"toString", "equals", "hashCode", "getClass"}
    if _is_repository_collaborator(c):
        # Spring Data repository interfaces inherit these methods from
        # CrudRepository/JpaRepository/etc. They usually do not appear as
        # declared methods in the project repository interface AST. Do not treat
        # them as invented when a ServiceImpl test stubs/verifies a repository.
        # Derived query methods such as findByStatus must still be declared in
        # the repository interface and are intentionally not wildcard-allowed.
        names |= _SPRING_DATA_REPOSITORY_METHODS
    if _is_environment_collaborator(c):
        # Environment is a framework interface. JavaParser often cannot see its
        # inherited PropertyResolver methods from the project source, but tests
        # for @ControllerAdvice/@RestControllerAdvice commonly stub getProperty.
        names |= _SPRING_ENVIRONMENT_METHODS
    return names


def suspect_invented_calls(
    code: str,
    cut_symbol: ClassSymbol | None,
    collaborators: list[Collaborator] | None,
    *,
    strict_cut_public_only: bool = False,
) -> list[str]:
    if cut_symbol is None:
        return []

    if javalang is None:
        return _suspect_invented_calls_regex(
            code,
            cut_symbol,
            collaborators,
            strict_cut_public_only=strict_cut_public_only,
        )

    try:
        tree = javalang.parse.parse(code)
    except Exception:
        return _suspect_invented_calls_regex(
            code,
            cut_symbol,
            collaborators,
            strict_cut_public_only=strict_cut_public_only,
        )

    type_methods: dict[str, set[str]] = {
        cut_symbol.name: _allowed_cut_method_names(cut_symbol, strict_public_only=strict_cut_public_only)
    }
    for c in collaborators or []:
        type_methods[c.simple] = _allowed_collaborator_method_names(c)

    var_types: dict[str, str] = {}
    for _, node in tree.filter(J.LocalVariableDeclaration):
        tname = getattr(node.type, "name", None)
        if tname in type_methods:
            for d in node.declarators:
                var_types[d.name] = tname
    for _, node in tree.filter(J.FieldDeclaration):
        tname = getattr(node.type, "name", None)
        if tname in type_methods:
            for d in node.declarators:
                var_types[d.name] = tname

    suspects: set[str] = set()
    for _, inv in tree.filter(J.MethodInvocation):
        owner = getattr(inv, "qualifier", None)
        if not owner:
            continue
        owner = str(owner).split(".")[-1]
        if owner not in var_types:
            continue
        tname = var_types[owner]
        if inv.member not in type_methods.get(tname, set()):
            suspects.add(f"{tname}.{inv.member}")
    return sorted(suspects)


def _suspect_invented_calls_regex(
    code: str,
    cut_symbol: ClassSymbol,
    collaborators: list[Collaborator] | None,
    *,
    strict_cut_public_only: bool = False,
) -> list[str]:
    type_methods: dict[str, set[str]] = {
        cut_symbol.name: _allowed_cut_method_names(cut_symbol, strict_public_only=strict_cut_public_only)
    }
    for c in collaborators or []:
        type_methods[c.simple] = _allowed_collaborator_method_names(c)

    var_types: dict[str, str] = {}
    for type_name in type_methods:
        for m in re.finditer(rf"\b{re.escape(type_name)}\s+([A-Za-z_$][\w$]*)\b", code):
            var_types[m.group(1)] = type_name
    suspects: set[str] = set()
    for owner, method in re.findall(r"\b([A-Za-z_$][\w$]*)\s*\.\s*([A-Za-z_$][\w$]*)\s*\(", code):
        tname = var_types.get(owner)
        if not tname:
            continue
        if method not in type_methods.get(tname, set()):
            suspects.add(f"{tname}.{method}")
    return sorted(suspects)
