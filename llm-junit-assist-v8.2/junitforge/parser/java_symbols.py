"""Java symbol extraction for junitforge.

Primary path uses the JavaParser CLI helper when available.  When the CLI is
not configured or fails, javalang is attempted.  When javalang fails on modern
Java or enterprise syntax, a brace-aware regex fallback salvages
the package, imports, primary type, class annotations, fields, constructors, and
public methods so the generator does not skip ServiceImpl/classes unnecessarily.

Lombok synthesis is intentionally conservative:
- @Data/@Getter/@Setter expose virtual accessors, equals, hashCode, toString.
- @Data does NOT expose no-args or all-args constructors.
- Constructors are exposed only when explicit or when Lombok constructor
  annotations are explicit.
- Accessor names are derived only from Java field identifiers, never from
  @Column/@JoinColumn/@Table/database names.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

try:
    import javalang
    from javalang import tree as J
    from javalang.parser import JavaSyntaxError
except Exception:  # pragma: no cover - runtime fallback for minimal deployments
    javalang = None
    JavaSyntaxError = Exception

    class _MissingJavalangTree:
        ClassDeclaration = type("ClassDeclaration", (), {})
        InterfaceDeclaration = type("InterfaceDeclaration", (), {})
        EnumDeclaration = type("EnumDeclaration", (), {})
        AnnotationDeclaration = type("AnnotationDeclaration", (), {})
        FieldDeclaration = type("FieldDeclaration", (), {})
        MethodDeclaration = type("MethodDeclaration", (), {})
        ConstructorDeclaration = type("ConstructorDeclaration", (), {})

    J = _MissingJavalangTree()

from junitforge.models import (
    ClassSymbol,
    EqualityAssertionMode,
    EqualityDescriptor,
    EqualityMember,
    EqualityStrategy,
    FieldSig,
    JavaSourceFile,
    MethodSig,
)

_KIND = {
    J.ClassDeclaration: "class",
    J.InterfaceDeclaration: "interface",
    J.EnumDeclaration: "enum",
    J.AnnotationDeclaration: "annotation",
}

_MODIFIERS = {
    "public", "protected", "private", "static", "final", "abstract", "native",
    "synchronized", "transient", "volatile", "strictfp", "default",
}

_TYPE_DECL_RE = re.compile(
    r"(?P<prefix>(?:@[\w.]+(?:\s*\([^\n{};]*\))?\s*)*)"
    r"(?P<mods>(?:(?:public|protected|private|abstract|final|static|strictfp)\s+)*)"
    r"(?P<kind>class|interface|enum|@interface|record)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
    r"(?P<tail>[^\{;]*)"
    r"[\{;]",
    re.MULTILINE,
)

_ANNOTATION_RE = re.compile(r"@([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][\w$]*$")


def parse_file(path: Path) -> JavaSourceFile:
    """Parse a Java source file into the junitforge symbol model.

    Order of extraction:
    1. JavaParser CLI jar, if present/enabled.
    2. javalang, for legacy/simple Java syntax.
    3. Brace-aware regex fallback, to avoid unnecessary skipped classes.

    The CLI performs AST extraction only.  Classification and test strategy stay
    in Python.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return JavaSourceFile(path=path, parse_ok=False, parse_error=f"read: {exc}")

    cli_error: str | None = None
    if _javaparser_cli_enabled():
        jar = _find_javaparser_cli_jar(path)
        if jar is not None:
            try:
                return _attach_source_context(_parse_with_javaparser_cli(path, source, jar))
            except Exception as exc:  # noqa: BLE001
                cli_error = f"JavaParser CLI failed: {type(exc).__name__}: {str(exc)[:240]}"

    jf = parse_source(path, source)
    if cli_error:
        if jf.parse_error:
            jf.parse_error = f"{cli_error}; {jf.parse_error}"
        else:
            jf.parse_error = cli_error
    return _attach_source_context(jf)


def parse_source(path: Path, source: str) -> JavaSourceFile:
    if javalang is None:
        return _regex_fallback_parse(source, path, "javalang unavailable")

    try:
        tree = javalang.parse.parse(source)
    except (JavaSyntaxError, Exception) as exc:  # noqa: BLE001
        err_summary = f"{type(exc).__name__}: {str(exc)[:160]}"
        return _regex_fallback_parse(source, path, err_summary)

    jf = JavaSourceFile(path=path, source=source)
    jf.package = tree.package.name if tree.package else None
    jf.imports = [_render_javalang_import(imp) for imp in (tree.imports or [])]
    pkg = jf.package or ""
    line_starts = _line_starts(source)

    for type_decl in tree.types or []:
        sym = _extract_type(type_decl, pkg, source, line_starts)
        if sym is not None:
            jf.types.append(sym)
    return _attach_source_context(jf)


def _attach_source_context(jf: JavaSourceFile) -> JavaSourceFile:
    """Attach owner package/import/source metadata to every nested ClassSymbol."""
    def visit(sym: ClassSymbol, enclosing: str | None = None) -> None:
        sym.package_name = jf.package
        sym.imports = list(jf.imports or [])
        sym.source_path = str(jf.path)
        sym.enclosing_fqcn = enclosing
        for field in sym.fields or []:
            if not field.declaring_type or "." not in field.declaring_type:
                field.declaring_type = sym.fqcn
        for nested in sym.nested or []:
            visit(nested, sym.fqcn)

    for symbol in jf.types or []:
        visit(symbol)
    return jf


# ---------------------------------------------------------------------------
# JavaParser CLI extraction
# ---------------------------------------------------------------------------


def _javaparser_cli_enabled() -> bool:
    value = os.getenv("JUNITFORGE_USE_JAVAPARSER_CLI", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _find_javaparser_cli_jar(java_file: Path) -> Path | None:  # noqa: ARG001
    """Locate tools/javaparser-cli/target/javaparser-cli.jar.

    Resolution order:
    - JUNITFORGE_JAVAPARSER_CLI_JAR or JAVAPARSER_CLI_JAR environment variable.
    - Current working directory and its parents.
    - This file's repository root and its parents.
    - Target Java file's parents, useful when running from inside a repo.
    """
    env_value = os.getenv("JUNITFORGE_JAVAPARSER_CLI_JAR") or os.getenv("JAVAPARSER_CLI_JAR")
    candidates: list[Path] = []
    if env_value:
        candidates.append(Path(env_value).expanduser())

    relative = Path("tools") / "javaparser-cli" / "target" / "javaparser-cli.jar"
    roots: list[Path] = []
    for base in (Path.cwd(), Path(__file__).resolve(), java_file.resolve()):
        current = base if base.is_dir() else base.parent
        roots.extend([current, *current.parents])

    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        candidates.append(root / relative)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:  # noqa: BLE001
            resolved = candidate
        if resolved.is_file():
            return resolved
    return None


def _parse_with_javaparser_cli(path: Path, source: str, jar: Path) -> JavaSourceFile:
    result = subprocess.run(
        ["java", "-jar", str(jar), str(path.resolve())],
        capture_output=True,
        text=True,
        timeout=int(os.getenv("JUNITFORGE_JAVAPARSER_TIMEOUT_SECONDS", "30")),
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise RuntimeError(detail[:1000])

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        preview = (result.stdout or "")[:1000]
        raise RuntimeError(f"invalid JSON from JavaParser CLI: {exc}; stdout={preview!r}") from exc

    return _java_source_from_cli_json(path, source, data)


def _java_source_from_cli_json(path: Path, source: str, data: dict[str, Any]) -> JavaSourceFile:
    jf = JavaSourceFile(path=path, source=source)
    jf.package = _none_if_blank(data.get("packageName") or data.get("package"))
    jf.imports = _import_names_from_cli(data.get("imports"), data.get("importNames"))
    jf.parse_ok = bool(data.get("parseOk", True))
    jf.parse_error = _none_if_blank(data.get("parseError"))

    pkg = jf.package or ""
    raw_types = data.get("types") or []
    if isinstance(raw_types, list):
        for raw_type in raw_types:
            if isinstance(raw_type, dict):
                jf.types.append(_class_symbol_from_cli(raw_type, pkg))

    # Some older/alternate extractor builds may expose only the primary type at root.
    if not jf.types and data.get("className"):
        jf.types.append(_class_symbol_from_cli(data, pkg))

    if not jf.types:
        jf.parse_ok = False
        jf.parse_error = jf.parse_error or "JavaParser CLI returned no types"

    for sym in jf.types:
        _synthesize_lombok(sym, source)
    return jf


def _class_symbol_from_cli(data: dict[str, Any], pkg: str) -> ClassSymbol:
    name = str(data.get("name") or data.get("className") or "Unknown")
    fqcn = str(data.get("fqcn") or (f"{pkg}.{name}" if pkg else name))
    kind = str(data.get("kind") or "class")

    sym = ClassSymbol(
        name=name,
        fqcn=fqcn,
        kind=kind,
        modifiers=_str_set(data.get("modifiers")),
        type_parameters=_str_list(data.get("typeParameters") or data.get("type_parameters")),
        extends=_none_if_blank(data.get("extends")),
        implements=_str_list(data.get("implements")),
        extends_types=_str_list(data.get("extendsList") or data.get("extends_list")),
        annotations=_simple_names(_str_list(data.get("annotations"))),
        annotation_exprs=_str_list(data.get("annotationExprs") or data.get("annotation_exprs")),
        fields=[_field_sig_from_cli(f) for f in _dict_list(data.get("fields"))],
        constructors=[_method_sig_from_cli(c, constructor=True, owner=name, owner_kind=kind) for c in _dict_list(data.get("constructors"))],
        methods=[_method_sig_from_cli(m, constructor=False, owner=name, owner_kind=kind) for m in _dict_list(data.get("methods"))],
        nested=[_class_symbol_from_cli(n, pkg) for n in _dict_list(data.get("nested"))],
        enum_constants=_str_list(data.get("enumConstants") or data.get("enum_constants")),
        line=_int_or_none(data.get("line")),
        end_line=_int_or_none(data.get("endLine") or data.get("end_line")),
    )
    if not sym.extends_types and sym.extends:
        sym.extends_types = [sym.extends]

    # JavaParser represents interface methods without an explicit public modifier
    # depending on source shape; normalize so classifier/generator can see them.
    if sym.kind == "interface":
        for method in sym.methods:
            if "private" not in method.modifiers:
                method.modifiers.add("public")

    return sym


def _field_sig_from_cli(data: dict[str, Any]) -> FieldSig:
    return FieldSig(
        name=str(data.get("name") or "field"),
        type=str(data.get("type") or "Object"),
        modifiers=_str_set(data.get("modifiers")),
        annotations=_simple_names(_str_list(data.get("annotations"))),
        annotation_exprs=_str_list(data.get("annotationExprs") or data.get("annotation_exprs")),
        line=_int_or_none(data.get("line")),
        initializer=_none_if_blank(data.get("initializer")),
        declaring_type=_none_if_blank(data.get("declaringType") or data.get("declaring_type")),
    )


def _method_sig_from_cli(data: dict[str, Any], *, constructor: bool, owner: str, owner_kind: str) -> MethodSig:
    name = str(data.get("name") or (owner if constructor else "method"))
    mods = _str_set(data.get("modifiers"))
    if owner_kind == "interface" and "private" not in mods:
        mods.add("public")
    params = _params_from_cli(data.get("params"))
    is_constructor = bool(data.get("isConstructor", constructor))
    return MethodSig(
        name=name,
        modifiers=mods,
        return_type=None if is_constructor else _none_if_blank(data.get("returnType") or data.get("return_type")),
        params=params,
        throws=_str_list(data.get("throws")),
        annotations=_simple_names(_str_list(data.get("annotations"))),
        line=_int_or_none(data.get("line")),
        end_line=_int_or_none(data.get("endLine") or data.get("end_line")),
        is_constructor=is_constructor,
        body_facts=_body_facts_from_cli(data),
        annotation_exprs=_str_list(data.get("annotationExprs") or data.get("annotation_exprs")),
    )


def _body_facts_from_cli(data: dict[str, Any]) -> dict[str, object]:
    value = data.get("bodyFacts") or data.get("executionFacts") or data.get("body_facts")
    return dict(value) if isinstance(value, dict) else {}


def _params_from_cli(value: Any) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    if not isinstance(value, list):
        return params
    for index, item in enumerate(value):
        if isinstance(item, dict):
            ptype = str(item.get("type") or "Object")
            pname = str(item.get("name") or f"arg{index}")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            ptype = str(item[0] or "Object")
            pname = str(item[1] or f"arg{index}")
        else:
            continue
        params.append((ptype, pname))
    return params


def _import_names_from_cli(imports_value: Any, import_names_value: Any) -> list[str]:
    """Return source imports preserving static/wildcard information.

    JavaParser emits structured imports.  Keep static imports as
    ``static com.acme.Constants.FOO`` so prompt/postprocess can distinguish
    normal type imports from constants/static members.
    """
    names: list[str] = []
    if isinstance(imports_value, list):
        for item in imports_value:
            if isinstance(item, dict):
                name = item.get("name")
                if not name:
                    continue
                rendered = str(name)
                if item.get("asterisk") and not rendered.endswith(".*"):
                    rendered += ".*"
                if item.get("static"):
                    rendered = "static " + rendered
                names.append(rendered)
            elif item:
                names.append(str(item))
        return names
    if isinstance(import_names_value, list):
        return [str(x) for x in import_names_value if x]
    return names


def _render_javalang_import(imp: Any) -> str:
    path = str(getattr(imp, "path", "") or "")
    if getattr(imp, "wildcard", False) and not path.endswith(".*"):
        path += ".*"
    return ("static " + path) if getattr(imp, "static", False) else path


def _regex_imports(source: str) -> list[str]:
    imports: list[str] = []
    for m in re.finditer(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;", source, flags=re.MULTILINE):
        imports.append((("static " if m.group(1) else "") + m.group(2)).strip())
    return imports


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (set, tuple, list)):
        return [str(x) for x in value if x is not None]
    return [str(value)]


def _str_set(value: Any) -> set[str]:
    return set(_str_list(value))


def _simple_names(values: list[str]) -> list[str]:
    return [v.split(".")[-1].lstrip("@") for v in values if v]


def _none_if_blank(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# javalang extraction
# ---------------------------------------------------------------------------


def _extract_type(node, pkg: str, source: str, line_starts: list[int]) -> ClassSymbol | None:
    kind = _KIND.get(type(node))
    if kind is None:
        return None

    name = node.name
    fqcn = f"{pkg}.{name}" if pkg else name
    start = node.position.line if node.position else None
    annotations = [_anno_name(a) for a in (getattr(node, "annotations", None) or [])]

    raw_extends = getattr(node, "extends", None)
    extends_nodes = list(raw_extends) if isinstance(raw_extends, list) else ([raw_extends] if raw_extends is not None else [])
    extends_types = [value for value in (type_to_str(item) for item in extends_nodes) if value]
    type_parameters = []
    for parameter in getattr(node, "type_parameters", None) or []:
        name_part = getattr(parameter, "name", None)
        if not name_part:
            continue
        bounds = [value for value in (type_to_str(bound) for bound in (getattr(parameter, "extends", None) or [])) if value]
        type_parameters.append(str(name_part) + ((" extends " + " & ".join(bounds)) if bounds else ""))

    sym = ClassSymbol(
        name=name,
        fqcn=fqcn,
        kind=kind,
        modifiers=set(getattr(node, "modifiers", None) or set()),
        type_parameters=type_parameters,
        extends=extends_types[0] if extends_types else None,
        implements=[n for n in (type_to_str(i) for i in (getattr(node, "implements", None) or [])) if n],
        extends_types=extends_types,
        annotations=annotations,
        annotation_exprs=_annotation_exprs_from_nodes(
            source, line_starts, getattr(node, "annotations", None) or []
        ),
        line=start,
        end_line=_end_line(source, line_starts, start) if start else None,
    )

    is_controller = any(a.split(".")[-1] in {"RestController", "Controller"} for a in annotations)

    for member in getattr(node, "body", None) or []:
        if isinstance(member, J.FieldDeclaration):
            sym.fields.extend(_fields(member, source, line_starts))
        elif isinstance(member, J.MethodDeclaration):
            sym.methods.append(_method(member, source, line_starts, constructor=False, is_controller=is_controller, owner_kind=kind))
        elif isinstance(member, J.ConstructorDeclaration):
            sym.constructors.append(_method(member, source, line_starts, constructor=True, owner_kind=kind))
        elif type(member) in _KIND:
            nested = _extract_type(member, fqcn, source, line_starts)
            if nested is not None:
                sym.nested.append(nested)

    _synthesize_lombok(sym, source)
    return sym


def _method(
    node,
    source: str,
    line_starts: list[int],
    constructor: bool = False,
    is_controller: bool = False,
    owner_kind: str = "class",
) -> MethodSig:
    start = node.position.line if node.position else None
    params: list[tuple[str, str]] = []
    for p in getattr(node, "parameters", None) or []:
        t = type_to_str(p.type)
        if getattr(p, "varargs", False):
            t = (t or "Object") + "..."
        params.append((t or "Object", p.name))

    annotations = [_anno_name(a) for a in (getattr(node, "annotations", None) or [])]
    mapping = {"GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping", "RequestMapping"}
    has_mapping_annotation = any(a.split(".")[-1] in mapping for a in annotations)

    modifiers = set(getattr(node, "modifiers", None) or set())
    if owner_kind == "interface":
        modifiers.add("public")
    if is_controller or has_mapping_annotation:
        modifiers.add("public")

    return MethodSig(
        name=node.name,
        modifiers=modifiers,
        return_type=None if constructor else type_to_str(getattr(node, "return_type", None)),
        params=params,
        throws=[str(t) for t in (getattr(node, "throws", None) or [])],
        annotations=annotations,
        line=start,
        end_line=_end_line(source, line_starts, start) if start else None,
        is_constructor=constructor,
        annotation_exprs=_annotation_exprs_from_nodes(
            source, line_starts, getattr(node, "annotations", None) or []
        ),
    )


def _fields(node, source: str, line_starts: list[int]) -> list[FieldSig]:
    t = type_to_str(node.type) or "Object"
    mods = set(getattr(node, "modifiers", None) or set())
    annos = [_anno_name(a) for a in (getattr(node, "annotations", None) or [])]
    line = node.position.line if node.position else None
    annotation_exprs = _annotation_exprs_from_nodes(
        source, line_starts, getattr(node, "annotations", None) or []
    )
    return [
        FieldSig(
            name=d.name,
            type=t,
            modifiers=set(mods),
            annotations=list(annos),
            annotation_exprs=list(annotation_exprs),
            line=line,
        )
        for d in node.declarators
    ]


def _anno_name(a) -> str:
    name = getattr(a, "name", None)
    return str(name or a).split(".")[-1]


def _annotation_exprs_from_nodes(source: str, line_starts: list[int], annotations: list[Any]) -> list[str]:
    """Recover exact annotation spelling/arguments from the existing AST positions."""
    expressions: list[str] = []
    for annotation in annotations:
        position = getattr(annotation, "position", None)
        if position is None or not (1 <= position.line <= len(line_starts)):
            continue
        start = line_starts[position.line - 1] + max(0, position.column - 1)
        if start >= len(source) or source[start] != "@":
            start = source.find("@", start, min(len(source), start + 160))
        if start < 0 or start >= len(source):
            continue
        cursor = start + 1
        while cursor < len(source) and (source[cursor].isalnum() or source[cursor] in "_.$"):
            cursor += 1
        while cursor < len(source) and source[cursor].isspace() and source[cursor] != "\n":
            cursor += 1
        if cursor < len(source) and source[cursor] == "(":
            depth = 0
            in_string = False
            escaped = False
            while cursor < len(source):
                char = source[cursor]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        break
                cursor += 1
        expression = source[start:cursor].strip()
        if expression:
            expressions.append(expression)
    return expressions


# ---------------------------------------------------------------------------
# Regex fallback
# ---------------------------------------------------------------------------


def _regex_fallback_parse(source: str, path: Path, exception_msg: str) -> JavaSourceFile:
    jf = JavaSourceFile(path=path, source=source, parse_ok=True, parse_error=f"Fallback Mode ({exception_msg})")

    pkg_match = re.search(r"^\s*package\s+([\w.]+)\s*;", source, flags=re.MULTILINE)
    jf.package = pkg_match.group(1) if pkg_match else None
    jf.imports = _regex_imports(source)
    pkg = jf.package or ""

    masked = _mask_comments_and_literals(source)
    type_match = _TYPE_DECL_RE.search(masked)
    if not type_match:
        jf.parse_ok = False
        jf.parse_error = f"Fallback Mode failed: no top-level type ({exception_msg})"
        return jf

    raw_kind = type_match.group("kind")
    kind = "annotation" if raw_kind == "@interface" else raw_kind
    name = type_match.group("name")
    tail = type_match.group("tail") or ""
    line_starts = _line_starts(source)
    start_line = _line_of(type_match.start(), line_starts)
    end_line = _end_line(source, line_starts, start_line)

    annotation_exprs = _annotation_expressions_from_text(type_match.group("prefix") or "")
    annotations = [a.split(".")[-1] for a in _ANNOTATION_RE.findall(type_match.group("prefix") or "")]
    if not annotations:
        annotations = _class_annotations_before(masked, type_match.start())
    modifiers = set((type_match.group("mods") or "").split()) & _MODIFIERS
    if "public" not in modifiers and re.search(rf"\bpublic\s+(?:class|interface|enum|record)\s+{re.escape(name)}\b", masked):
        modifiers.add("public")

    type_parameters: list[str] = []
    type_param_match = re.match(r"\s*<(?P<body>.*?)>\s*(?=(?:extends|implements)\b|\(|$)", tail)
    if type_param_match:
        type_parameters = [value.strip() for value in _split_params(type_param_match.group("body")) if value.strip()]

    extends_types: list[str] = []
    ext_m = re.search(r"\bextends\s+(?P<body>.*?)(?=\bimplements\b|$)", tail)
    if ext_m:
        extends_types = [value.strip() for value in _split_params(ext_m.group("body")) if value.strip()]
    ext = extends_types[0] if extends_types else None

    implements: list[str] = []
    impl_m = re.search(r"\bimplements\s+([^\{]+)", tail)
    if impl_m:
        implements = [value.strip() for value in _split_params(impl_m.group(1)) if value.strip()]

    sym = ClassSymbol(
        name=name,
        fqcn=f"{pkg}.{name}" if pkg else name,
        kind=kind,
        modifiers=modifiers,
        type_parameters=type_parameters,
        extends=ext,
        implements=implements,
        extends_types=extends_types,
        annotations=annotations,
        annotation_exprs=annotation_exprs,
        line=start_line,
        end_line=end_line,
    )

    if kind == "record":
        sym.fields.extend(_record_component_fields(tail, name, start_line))
        for component in sym.fields:
            sym.methods.append(
                MethodSig(
                    name=component.name,
                    modifiers={"public"},
                    return_type=component.type,
                    params=[],
                    annotations=["JavaGenerated"],
                    line=start_line,
                    end_line=start_line,
                )
            )

    body = _extract_body(masked, type_match.end() - 1)
    members = _top_level_members(body, start_offset=type_match.end())
    is_controller = any(a in {"RestController", "Controller"} for a in annotations)

    for member_text, member_start in members:
        stripped = member_text.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if _looks_like_field(stripped):
            sym.fields.extend(_parse_field_member(stripped, member_start, line_starts))
        elif "(" in stripped:
            parsed = _parse_method_member(stripped, member_start, line_starts, owner=name, owner_kind=kind, is_controller=is_controller)
            if parsed is None:
                continue
            if parsed.is_constructor:
                sym.constructors.append(parsed)
            else:
                sym.methods.append(parsed)

    _synthesize_lombok(sym, source)
    jf.types.append(sym)
    return jf


def _mask_comments_and_literals(source: str) -> str:
    out: list[str] = []
    i = 0
    n = len(source)
    in_line = in_block = in_str = in_char = False
    esc = False
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            else:
                out.append(" ")
        elif in_block:
            if c == "*" and nxt == "/":
                out.extend("  ")
                in_block = False
                i += 1
            else:
                out.append("\n" if c == "\n" else " ")
        elif in_str:
            if esc:
                esc = False
                out.append(" ")
            elif c == "\\":
                esc = True
                out.append(" ")
            elif c == '"':
                in_str = False
                out.append('"')
            else:
                out.append("\n" if c == "\n" else " ")
        elif in_char:
            if esc:
                esc = False
                out.append(" ")
            elif c == "\\":
                esc = True
                out.append(" ")
            elif c == "'":
                in_char = False
                out.append("'")
            else:
                out.append("\n" if c == "\n" else " ")
        else:
            if c == "/" and nxt == "/":
                in_line = True
                out.extend("  ")
                i += 1
            elif c == "/" and nxt == "*":
                in_block = True
                out.extend("  ")
                i += 1
            elif c == '"':
                in_str = True
                out.append(c)
            elif c == "'":
                in_char = True
                out.append(c)
            else:
                out.append(c)
        i += 1
    return "".join(out)


def _class_annotations_before(masked: str, type_start: int) -> list[str]:
    prefix = masked[:type_start]
    lines = prefix.splitlines()
    collected: list[str] = []
    for line in reversed(lines[-25:]):
        s = line.strip()
        if not s:
            if collected:
                break
            continue
        if s.startswith("@"):
            collected.extend(reversed([a.split(".")[-1] for a in _ANNOTATION_RE.findall(s)]))
            continue
        if re.fullmatch(r"(?:public|protected|private|abstract|final|static|strictfp)\s*", s):
            continue
        break
    return list(reversed(collected))


def _extract_body(masked: str, open_brace_pos: int) -> str:
    if open_brace_pos < 0 or open_brace_pos >= len(masked) or masked[open_brace_pos] != "{":
        brace = masked.find("{", open_brace_pos)
        if brace < 0:
            return ""
        open_brace_pos = brace
    depth = 0
    for i in range(open_brace_pos, len(masked)):
        c = masked[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return masked[open_brace_pos + 1:i]
    return masked[open_brace_pos + 1:]


def _top_level_members(body: str, start_offset: int) -> list[tuple[str, int]]:
    members: list[tuple[str, int]] = []
    depth = 0
    start = 0
    for i, c in enumerate(body):
        if c in "({[":
            depth += 1
        elif c in ")}]" and depth > 0:
            depth -= 1
        elif c == ";" and depth == 0:
            segment = body[start:i + 1]
            if segment.strip():
                members.append((segment, start_offset + start))
            start = i + 1
        elif c == "{" and depth == 0:
            # defensive; normally caught by c in "({[" above
            depth += 1
        # method/class body: when a top-level method block closes, emit it
        if c == "}" and depth == 0:
            segment = body[start:i + 1]
            if "(" in segment and segment.strip():
                members.append((segment, start_offset + start))
                start = i + 1
    tail = body[start:].strip()
    if tail:
        members.append((tail, start_offset + start))
    return members


def _strip_annotations(text: str) -> str:
    previous = None
    out = text
    # Remove simple one-level annotation parameter lists. Enterprise annotations can
    # be complex; fallback only needs enough to expose fields/method headers.
    while previous != out:
        previous = out
        out = re.sub(r"@[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?(?:\s*\([^()]*\))?", "", out)
    return out


def _annotation_expressions_from_text(text: str) -> list[str]:
    """Extract balanced Java annotation expressions from an existing source slice."""
    expressions: list[str] = []
    cursor = 0
    while True:
        match = re.search(r"@[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", text[cursor:])
        if match is None:
            break
        start = cursor + match.start()
        end = cursor + match.end()
        probe = end
        while probe < len(text) and text[probe].isspace() and text[probe] != "\n":
            probe += 1
        if probe < len(text) and text[probe] == "(":
            depth = 0
            in_string = False
            escaped = False
            while probe < len(text):
                char = text[probe]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        probe += 1
                        break
                probe += 1
            end = probe
        expressions.append(text[start:end].strip())
        cursor = max(end, start + 1)
    return expressions


def _record_component_fields(tail: str, owner: str, line: int) -> list[FieldSig]:
    open_index = tail.find("(")
    if open_index < 0:
        return []
    depth = 0
    close_index = -1
    for index in range(open_index, len(tail)):
        char = tail[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_index = index
                break
    if close_index < 0:
        return []
    fields: list[FieldSig] = []
    for raw_component in _split_params(tail[open_index + 1:close_index]):
        exprs = _annotation_expressions_from_text(raw_component)
        annos = [a.split(".")[-1] for a in _ANNOTATION_RE.findall(raw_component)]
        clean = re.sub(r"\bfinal\b", "", _strip_annotations(raw_component)).strip()
        match = re.match(r"(?P<type>.+?)\s+(?P<name>[A-Za-z_$][\w$]*)$", clean)
        if match is None:
            continue
        fields.append(
            FieldSig(
                name=match.group("name"),
                type=" ".join(match.group("type").split()),
                modifiers={"private", "final"},
                annotations=annos,
                annotation_exprs=exprs,
                line=line,
                declaring_type=owner,
            )
        )
    return fields


def _looks_like_field(member: str) -> bool:
    head = _strip_annotations(member).strip()
    if not head.endswith(";"):
        return False
    if "(" in head or ")" in head:
        return False
    if head.startswith(("package ", "import ")):
        return False
    return bool(re.search(r"\b[A-Za-z_$][\w$]*(?:\s*=|\s*[,;])", head))


def _parse_field_member(member: str, member_start: int, line_starts: list[int]) -> list[FieldSig]:
    annos = [a.split(".")[-1] for a in _ANNOTATION_RE.findall(member)]
    annotation_exprs = _annotation_expressions_from_text(member)
    clean = _strip_annotations(member)
    clean = clean.strip().rstrip(";").strip()
    if not clean:
        return []
    tokens = clean.split()
    mods = {t for t in tokens if t in _MODIFIERS}
    rest = " ".join(t for t in tokens if t not in _MODIFIERS)
    if not rest:
        return []
    parts = [p.strip() for p in rest.split(",")]
    first = parts[0]
    first = first.split("=", 1)[0].strip()
    m = re.match(r"(?P<type>.+?)\s+(?P<name>[A-Za-z_$][\w$]*)$", first)
    if not m:
        return []
    field_type = " ".join(m.group("type").split())
    names = [m.group("name")]
    for extra in parts[1:]:
        name = extra.split("=", 1)[0].strip()
        if _IDENTIFIER_RE.match(name):
            names.append(name)
    line = _line_of(member_start, line_starts)
    return [
        FieldSig(
            name=n,
            type=field_type,
            modifiers=set(mods),
            annotations=list(annos),
            annotation_exprs=list(annotation_exprs),
            line=line,
        )
        for n in names
    ]


def _parse_method_member(member: str, member_start: int, line_starts: list[int], *, owner: str, owner_kind: str, is_controller: bool) -> MethodSig | None:
    header = member.split("{", 1)[0].strip().rstrip(";").strip()
    annos = [a.split(".")[-1] for a in _ANNOTATION_RE.findall(header)]
    header = _strip_annotations(header)
    header = " ".join(header.split())
    m = re.match(r"(?P<prefix>.*?)\s*(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<params>[^)]*)\)\s*(?:throws\s+(?P<throws>.*))?$", header)
    if not m:
        return None
    prefix = (m.group("prefix") or "").strip()
    name = m.group("name")
    prefix_tokens = prefix.split()
    mods = {t for t in prefix_tokens if t in _MODIFIERS}
    type_tokens = [t for t in prefix_tokens if t not in _MODIFIERS]
    is_constructor = name == owner and not type_tokens
    return_type = None if is_constructor else (" ".join(type_tokens) if type_tokens else "void")
    if owner_kind == "interface":
        mods.add("public")
    if any(a in {"GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping", "RequestMapping"} for a in annos) or is_controller:
        mods.add("public")
    params = _parse_params(m.group("params") or "")
    throws = [x.strip().split(".")[-1] for x in (m.group("throws") or "").split(",") if x.strip()]
    line = _line_of(member_start, line_starts)
    return MethodSig(
        name=name,
        modifiers=mods,
        return_type=return_type,
        params=params,
        throws=throws,
        annotations=annos,
        line=line,
        end_line=_line_of(member_start + len(member), line_starts),
        is_constructor=is_constructor,
        annotation_exprs=_annotation_expressions_from_text(member.split("{", 1)[0]),
    )


def _parse_params(params_text: str) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    if not params_text.strip():
        return params
    for raw in _split_params(params_text):
        p = _ANNOTATION_RE.sub("", raw)
        p = re.sub(r"\b(final)\b", "", p)
        p = " ".join(p.strip().split())
        if not p:
            continue
        tokens = p.split()
        if len(tokens) < 2:
            continue
        name = tokens[-1]
        ptype = " ".join(tokens[:-1])
        params.append((ptype, name))
    return params


def _split_params(params: str) -> list[str]:
    out: list[str] = []
    start = 0
    angle_depth = 0
    paren_depth = 0
    bracket_depth = 0
    for i, c in enumerate(params):
        if c == "<":
            angle_depth += 1
        elif c == ">" and angle_depth > 0:
            angle_depth -= 1
        elif c == "(":
            paren_depth += 1
        elif c == ")" and paren_depth > 0:
            paren_depth -= 1
        elif c == "[":
            bracket_depth += 1
        elif c == "]" and bracket_depth > 0:
            bracket_depth -= 1
        elif c == "," and angle_depth == 0 and paren_depth == 0 and bracket_depth == 0:
            out.append(params[start:i])
            start = i + 1
    out.append(params[start:])
    return out


# ---------------------------------------------------------------------------
# Lombok synthesis
# ---------------------------------------------------------------------------


def _synthesize_lombok(sym: ClassSymbol, source: str) -> None:
    annos = {a.split(".")[-1] for a in (sym.annotations or [])}
    has_data = "Data" in annos or "@Data" in source
    has_value = "Value" in annos or "@Value" in source
    has_equals_hashcode = "EqualsAndHashCode" in annos or "@EqualsAndHashCode" in source
    has_to_string = "ToString" in annos or "@ToString" in source
    has_getter = has_data or has_value or "Getter" in annos or "@Getter" in source
    has_setter = has_data or "Setter" in annos or "@Setter" in source
    has_builder = "Builder" in annos or "SuperBuilder" in annos or "@Builder" in source or "@SuperBuilder" in source
    has_no_args = "NoArgsConstructor" in annos or "@NoArgsConstructor" in source
    has_all_args = "AllArgsConstructor" in annos or "@AllArgsConstructor" in source

    existing_methods = {m.name for m in sym.methods}
    existing_ctor_arities = {len(c.params) for c in sym.constructors}

    for field in sym.fields or []:
        if "static" in field.modifiers:
            continue
        f_annos = {a.split(".")[-1] for a in (field.annotations or [])}
        ftype = field.type or "Object"
        if has_getter or "Getter" in f_annos:
            getter = _getter_name(field.name, ftype)
            if getter not in existing_methods:
                sym.methods.append(MethodSig(name=getter, modifiers={"public"}, return_type=ftype, params=[], annotations=["LombokGenerated"], line=sym.line, end_line=sym.line))
                existing_methods.add(getter)
        if has_setter or "Setter" in f_annos:
            setter = _setter_name(field.name, ftype)
            if setter not in existing_methods:
                sym.methods.append(MethodSig(name=setter, modifiers={"public"}, return_type="void", params=[(ftype, field.name)], annotations=["LombokGenerated"], line=sym.line, end_line=sym.line))
                existing_methods.add(setter)

    if has_data or has_value or has_equals_hashcode:
        for name, ret, params, mods in (
            ("equals", "boolean", [("Object", "o")], {"public"}),
            ("hashCode", "int", [], {"public"}),
            ("canEqual", "boolean", [("Object", "other")], {"protected"}),
        ):
            if name not in existing_methods:
                sym.methods.append(MethodSig(name=name, modifiers=set(mods), return_type=ret, params=list(params), annotations=["LombokGenerated"], line=sym.line, end_line=sym.line))
                existing_methods.add(name)

    if has_data or has_value or has_to_string:
        if "toString" not in existing_methods:
            sym.methods.append(MethodSig(name="toString", modifiers={"public"}, return_type="String", params=[], annotations=["LombokGenerated"], line=sym.line, end_line=sym.line))
            existing_methods.add("toString")

    if has_builder and "builder" not in existing_methods:
        sym.methods.append(MethodSig(name="builder", modifiers={"public", "static"}, return_type=f"{sym.name}.{sym.name}Builder", params=[], annotations=["LombokGenerated"], line=sym.line, end_line=sym.line))

    if has_no_args and 0 not in existing_ctor_arities:
        sym.constructors.append(MethodSig(name=sym.name, modifiers={"public"}, return_type=None, params=[], annotations=["LombokGenerated"], line=sym.line, end_line=sym.line, is_constructor=True))

    if has_all_args:
        params = [(f.type, f.name) for f in (sym.fields or []) if "static" not in f.modifiers]
        if params and len(params) not in existing_ctor_arities:
            sym.constructors.append(MethodSig(name=sym.name, modifiers={"public"}, return_type=None, params=params, annotations=["LombokGenerated"], line=sym.line, end_line=sym.line, is_constructor=True))


def _getter_name(field_name: str, field_type: str) -> str:
    base = _bean_base(field_name, field_type)
    if _is_primitive_boolean(field_type):
        return f"is{base}"
    return f"get{_java_bean_suffix(field_name)}"


def _setter_name(field_name: str, field_type: str) -> str:  # noqa: ARG001
    return f"set{_bean_base(field_name, field_type)}"


def _bean_base(field_name: str, field_type: str) -> str:
    if _is_primitive_boolean(field_type) and field_name.startswith("is") and len(field_name) > 2 and field_name[2].isupper():
        return field_name[2:]
    return _java_bean_suffix(field_name)


def _is_primitive_boolean(field_type: str | None) -> bool:
    return (field_type or "").strip() == "boolean"


def _java_bean_suffix(field_name: str) -> str:
    if not field_name:
        return ""
    if len(field_name) >= 2 and field_name[0].isupper() and field_name[1].isupper():
        return field_name
    return field_name[0].upper() + field_name[1:]


# ---------------------------------------------------------------------------
# javalang type -> string
# ---------------------------------------------------------------------------


def type_to_str(t) -> str | None:
    if t is None:
        return None
    name = getattr(t, "name", None) or ""
    args = getattr(t, "arguments", None)
    if args:
        rendered = []
        for a in args:
            pt = getattr(a, "pattern_type", None)
            at = getattr(a, "type", None)
            if pt == "?" or (pt is None and at is None):
                rendered.append("?")
            elif pt in ("extends", "super"):
                rendered.append(f"? {pt} {type_to_str(at)}")
            else:
                rendered.append(type_to_str(at) or "?")
        name += "<" + ", ".join(rendered) + ">"
    sub = getattr(t, "sub_type", None)
    if sub is not None:
        sub_str = type_to_str(sub)
        if sub_str:
            name += "." + sub_str
    dims = getattr(t, "dimensions", None) or []
    name += "[]" * len(dims)
    return name or None


def _type_name(t) -> str | None:
    if t is None:
        return None
    name = getattr(t, "name", None)
    if name is None:
        return None
    sub = getattr(t, "sub_type", None)
    if sub is not None and getattr(sub, "name", None):
        return f"{name}.{sub.name}"
    return name


def _first_type_name(ext) -> str | None:
    if ext is None:
        return None
    if isinstance(ext, list):
        return _type_name(ext[0]) if ext else None
    return _type_name(ext)


def _base_type(rendered: str) -> str:
    t = re.sub(r"<.*>", "", rendered).replace("[]", "").replace("...", "").strip()
    return t.split(".")[-1]


# ---------------------------------------------------------------------------
# Line scanner
# ---------------------------------------------------------------------------


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of(offset: int, line_starts: list[int]) -> int:
    return bisect.bisect_right(line_starts, offset)


def _end_line(source: str, line_starts: list[int], start_line: int) -> int:
    """Return the 1-based line of the member's terminating `}` or `;`."""
    n = len(source)
    if not (1 <= start_line <= len(line_starts)):
        return start_line
    i = line_starts[start_line - 1]
    depth = 0
    seen_brace = False
    in_line = in_block = in_str = in_char = False
    esc = False

    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
        elif in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 1
        elif in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif in_char:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == "'":
                in_char = False
        else:
            if c == "/" and nxt == "/":
                in_line = True
                i += 1
            elif c == "/" and nxt == "*":
                in_block = True
                i += 1
            elif c == '"':
                in_str = True
            elif c == "'":
                in_char = True
            elif c == "{":
                depth += 1
                seen_brace = True
            elif c == "}":
                depth -= 1
                if seen_brace and depth == 0:
                    return _line_of(i, line_starts)
            elif c == ";" and not seen_brace and depth == 0:
                return _line_of(i, line_starts)
        i += 1
    return start_line


# ---------------------------------------------------------------------------
# DTO/entity equality contract resolution
# ---------------------------------------------------------------------------


_ENTITY_ANNOTATIONS = {"Entity", "Embeddable", "MappedSuperclass"}
_RELATIONSHIP_ANNOTATIONS = {
    "OneToOne", "OneToMany", "ManyToOne", "ManyToMany", "ElementCollection",
}
_ID_ANNOTATIONS = {"Id", "EmbeddedId"}


def resolve_equality_descriptor(
    symbol: ClassSymbol,
    source: str | None = None,
    lookup: Callable[..., ClassSymbol | None] | None = None,
    *,
    _seen: set[str] | None = None,
) -> EqualityDescriptor:
    """Resolve equality from existing symbols/source, failing closed when unsafe."""
    cached = getattr(symbol, "equality_descriptor", None)
    retry_unresolved_parent = bool(
        cached is not None
        and lookup is not None
        and any(
            "unresolved parent equality" in warning
            for warning in cached.warnings
        )
    )
    if cached is not None and not retry_unresolved_parent:
        return cached
    if retry_unresolved_parent:
        symbol.equality_descriptor = None

    seen = set(_seen or ())
    identity = symbol.fqcn or symbol.name
    if identity in seen:
        return EqualityDescriptor(
            strategy=EqualityStrategy.UNRESOLVED,
            assertion_mode=EqualityAssertionMode.CONSERVATIVE,
            warnings=[f"cyclic equality inheritance involving {symbol.name}"],
        )
    seen.add(identity)

    source = source if source is not None else _source_for_equality_symbol(symbol)
    entity = bool(_simple_annotation_names(symbol.annotations) & _ENTITY_ANNOTATIONS)
    explicit_equals = _explicit_object_methods(symbol, "equals")
    explicit_hash = _explicit_object_methods(symbol, "hashCode")

    if bool(explicit_equals) != bool(explicit_hash):
        missing = "hashCode" if explicit_equals else "equals"
        present = "equals" if explicit_equals else "hashCode"
        descriptor = EqualityDescriptor(
            strategy=EqualityStrategy.UNRESOLVED,
            assertion_mode=EqualityAssertionMode.CONSERVATIVE,
            entity=entity,
            unrelated_type_safe=_manual_unrelated_type_safe(
                _method_source(source, (explicit_equals or explicit_hash)[0])
            ),
            warnings=[f"{present} is overridden but {missing} is not visibly overridden"],
        )
        if entity and _identifier_fields(symbol):
            descriptor.warnings.append("ambiguous null-ID entity equality")
        symbol.equality_descriptor = descriptor
        return descriptor

    if explicit_equals and explicit_hash:
        descriptor = _manual_equality_descriptor(
            symbol, source, explicit_equals[0], explicit_hash[0], lookup, entity
        )
        symbol.equality_descriptor = descriptor
        return descriptor

    lombok_strategy = _lombok_equality_strategy(symbol)
    if lombok_strategy is not None:
        descriptor = _lombok_equality_descriptor(
            symbol, source, lookup, lombok_strategy, entity, seen
        )
        symbol.equality_descriptor = descriptor
        return descriptor

    if symbol.kind == "record":
        members = [
            _equality_member(symbol, field, "record-component", lookup, entity)
            for field in symbol.fields
            if "static" not in field.modifiers
        ]
        descriptor = EqualityDescriptor(
            strategy=EqualityStrategy.RECORD,
            assertion_mode=EqualityAssertionMode.VALUE,
            members=members,
            entity=entity,
            unrelated_type_safe=True,
        )
        _complete_value_descriptor(descriptor, symbol, difference_names={m.name for m in members})
        symbol.equality_descriptor = descriptor
        return descriptor

    parent_type = _normalized_reference_type(symbol.extends or "")
    if parent_type and parent_type not in {"Object", "java.lang.Object"}:
        parent = _lookup_equality_symbol(lookup, parent_type, symbol)
        if parent is None:
            descriptor = EqualityDescriptor(
                strategy=EqualityStrategy.INHERITED,
                assertion_mode=EqualityAssertionMode.CONSERVATIVE,
                parent_type=parent_type,
                entity=entity,
                warnings=[f"unresolved parent equality: {parent_type}"],
            )
        else:
            parent_descriptor = resolve_equality_descriptor(parent, None, lookup, _seen=seen)
            descriptor = _inherited_equality_descriptor(
                symbol, parent, parent_descriptor, entity
            )
        symbol.equality_descriptor = descriptor
        return descriptor

    descriptor = EqualityDescriptor(
        strategy=EqualityStrategy.OBJECT_IDENTITY,
        assertion_mode=EqualityAssertionMode.IDENTITY,
        entity=entity,
        unrelated_type_safe=True,
    )
    symbol.equality_descriptor = descriptor
    return descriptor


def _lombok_equality_strategy(symbol: ClassSymbol) -> EqualityStrategy | None:
    if _has_lombok_annotation(symbol, "EqualsAndHashCode"):
        return EqualityStrategy.LOMBOK_EQUALS_HASHCODE
    if _has_lombok_annotation(symbol, "Data"):
        return EqualityStrategy.LOMBOK_DATA
    if _has_lombok_annotation(symbol, "Value"):
        return EqualityStrategy.LOMBOK_VALUE
    return None


def _lombok_equality_descriptor(
    symbol: ClassSymbol,
    source: str,
    lookup: Callable[..., ClassSymbol | None] | None,
    strategy: EqualityStrategy,
    entity: bool,
    seen: set[str],
) -> EqualityDescriptor:
    annotation = _class_annotation_expression(symbol, "EqualsAndHashCode") or ""
    only_explicit = bool(re.search(
        r"\bonlyExplicitlyIncluded\s*=\s*true\b", annotation, flags=re.IGNORECASE
    ))
    call_super = bool(re.search(
        r"\bcallSuper\s*=\s*true\b", annotation, flags=re.IGNORECASE
    ))
    descriptor = EqualityDescriptor(
        strategy=strategy,
        assertion_mode=EqualityAssertionMode.VALUE,
        only_explicitly_included=only_explicit,
        call_super=call_super,
        parent_type=_normalized_reference_type(symbol.extends or "") or None,
        entity=entity,
        unrelated_type_safe=True,
    )

    selected_fields: list[FieldSig] = []
    for field in symbol.fields:
        if "static" in field.modifiers:
            continue
        included = _has_equality_member_annotation(field, "Include")
        excluded = _has_equality_member_annotation(field, "Exclude")
        if only_explicit:
            if included:
                selected_fields.append(field)
            continue
        if "transient" in field.modifiers or excluded:
            continue
        selected_fields.append(field)

    included_methods = [
        method for method in symbol.methods
        if _has_equality_method_annotation(method, "Include")
    ]
    unresolved_method_members: list[str] = []
    replacements: set[str] = set()
    mapped_methods: list[tuple[MethodSig, FieldSig]] = []
    fields_by_name = {field.name: field for field in symbol.fields}
    for method in included_methods:
        expression = _method_annotation_expression(method, "Include") or ""
        replacement = _annotation_string_attribute(expression, "replaces")
        if replacement:
            replacements.add(replacement)
        mapped_name = _simple_included_method_field(method, source, symbol)
        mapped_field = fields_by_name.get(mapped_name or "")
        if mapped_field is None:
            unresolved_method_members.append(method.name)
            continue
        mapped_methods.append((method, mapped_field))

    if replacements:
        selected_fields = [field for field in selected_fields if field.name not in replacements]

    descriptor.members.extend(
        _equality_member(symbol, field, "field", lookup, entity)
        for field in selected_fields
    )
    existing_keys = {(member.declaring_type, member.name) for member in descriptor.members}
    for method, mapped_field in mapped_methods:
        key = (symbol.fqcn or symbol.name, mapped_field.name)
        if key in existing_keys:
            continue
        member = _equality_member(
            symbol, mapped_field, f"included-method:{method.name}", lookup, entity
        )
        descriptor.members.append(member)
        existing_keys.add(key)

    if only_explicit and not selected_fields and not mapped_methods:
        descriptor.warnings.append(
            "onlyExplicitlyIncluded = true but no included equality member was resolved"
        )
    if unresolved_method_members:
        descriptor.warnings.append(
            "unsupported included equality method(s): "
            + ", ".join(sorted(unresolved_method_members))
        )
        descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE

    if call_super:
        parent_type = _normalized_reference_type(symbol.extends or "")
        parent = _lookup_equality_symbol(lookup, parent_type, symbol) if parent_type else None
        if parent is None:
            descriptor.warnings.append(
                f"unresolved parent equality: {parent_type or 'java.lang.Object'}"
            )
            descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE
        else:
            parent_descriptor = resolve_equality_descriptor(parent, None, lookup, _seen=seen)
            if parent_descriptor.assertion_mode is EqualityAssertionMode.VALUE:
                descriptor.members.extend(_inherited_members(parent_descriptor.members))
            elif parent_descriptor.assertion_mode is EqualityAssertionMode.IDENTITY:
                descriptor.warnings.append(
                    f"callSuper = true reaches identity equality in {parent.name}; "
                    "distinct equal instances are not provable"
                )
                descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE
            else:
                descriptor.warnings.append(f"unresolved parent equality: {parent.name}")
                descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE

    _apply_entity_equality_safety(descriptor, symbol)
    if descriptor.assertion_mode is EqualityAssertionMode.VALUE:
        _complete_value_descriptor(
            descriptor, symbol, difference_names={member.name for member in descriptor.members}
        )
    return descriptor


def _manual_equality_descriptor(
    symbol: ClassSymbol,
    source: str,
    equals_method: MethodSig,
    hash_method: MethodSig,
    lookup: Callable[..., ClassSymbol | None] | None,
    entity: bool,
) -> EqualityDescriptor:
    equals_source = _method_source(source, equals_method)
    hash_source = _method_source(source, hash_method)
    equals_names = _visible_manual_fields(symbol, equals_source)
    hash_names = _visible_manual_fields(symbol, hash_source)
    descriptor = EqualityDescriptor(
        strategy=EqualityStrategy.MANUAL,
        assertion_mode=EqualityAssertionMode.VALUE,
        entity=entity,
        unrelated_type_safe=_manual_unrelated_type_safe(equals_source),
    )
    unsupported = _unsupported_manual_calls(symbol, equals_source, hash_source)
    if unsupported:
        descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE
        descriptor.warnings.append(
            "complex manual equality uses unsupported helper/normalization: "
            + ", ".join(sorted(unsupported))
        )

    union = equals_names | hash_names
    descriptor.members = [
        _equality_member(symbol, field, "manual", lookup, entity)
        for field in symbol.fields
        if field.name in union and "static" not in field.modifiers
    ]
    extra_hash = hash_names - equals_names
    if extra_hash:
        descriptor.warnings.append(
            "hashCode visibly uses member(s) absent from equals: "
            + ", ".join(sorted(extra_hash))
        )
    if not equals_names:
        descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE
        descriptor.warnings.append("manual equals has no safely resolved participating field")

    _apply_entity_equality_safety(descriptor, symbol)
    if descriptor.assertion_mode is EqualityAssertionMode.VALUE:
        _complete_value_descriptor(descriptor, symbol, difference_names=equals_names)
    return descriptor


def _inherited_equality_descriptor(
    child: ClassSymbol,
    parent: ClassSymbol,
    parent_descriptor: EqualityDescriptor,
    entity: bool,
) -> EqualityDescriptor:
    descriptor = EqualityDescriptor(
        strategy=EqualityStrategy.INHERITED,
        assertion_mode=parent_descriptor.assertion_mode,
        members=_inherited_members(parent_descriptor.members),
        parent_type=parent.fqcn or parent.name,
        entity=entity,
        entity_key_kind=parent_descriptor.entity_key_kind,
        unrelated_type_safe=parent_descriptor.unrelated_type_safe,
        warnings=list(parent_descriptor.warnings),
    )
    if descriptor.assertion_mode is EqualityAssertionMode.VALUE:
        _complete_value_descriptor(
            descriptor, child, difference_names={member.name for member in descriptor.members}
        )
    elif descriptor.assertion_mode is EqualityAssertionMode.CONSERVATIVE:
        descriptor.warnings.append(
            f"inherited equality from {parent.name} is unresolved or unsafe"
        )
    return descriptor


def _complete_value_descriptor(
    descriptor: EqualityDescriptor,
    symbol: ClassSymbol,
    *,
    difference_names: set[str],
) -> None:
    if descriptor.assertion_mode is not EqualityAssertionMode.VALUE:
        return
    unresolved_equal_values = [
        member.name
        for member in descriptor.members
        if member.equal_value is None
    ]
    if unresolved_equal_values:
        descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE
        descriptor.warnings.append(
            "no verified minimal equal-pair value for participating member(s): "
            + ", ".join(sorted(unresolved_equal_values))
        )
        return
    for member in descriptor.members:
        if (
            member.name in difference_names
            and member.assignable
            and member.has_verified_difference
        ):
            descriptor.difference_member = member.name
            break
    if not descriptor.members:
        descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE
        descriptor.warnings.append("no equality-participating member was resolved")
    elif descriptor.difference_member is None:
        descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE
        descriptor.warnings.append(
            f"no verified writable equality member on {symbol.name} can create "
            "the required unequal instance"
        )


def _apply_entity_equality_safety(
    descriptor: EqualityDescriptor, symbol: ClassSymbol
) -> None:
    if not descriptor.entity:
        return
    id_fields = _identifier_fields(symbol)
    participating = list(descriptor.members)
    participating_ids = [member for member in participating if member.identifier]
    if participating and len(participating_ids) == len(participating):
        descriptor.entity_key_kind = "identifier"
    elif participating_ids:
        descriptor.entity_key_kind = "identifier-and-business-state"
    elif participating:
        descriptor.entity_key_kind = "business-key"
    else:
        descriptor.entity_key_kind = "unresolved"

    unsafe = [member.name for member in descriptor.members if member.relationship]
    if unsafe:
        descriptor.assertion_mode = EqualityAssertionMode.CONSERVATIVE
        descriptor.warnings.append(
            "relationship-heavy entity equality is unsafe: "
            + ", ".join(sorted(unsafe))
        )
    if descriptor.entity_key_kind == "unresolved" and id_fields:
        descriptor.warnings.append("ambiguous null-ID entity equality")


def _equality_member(
    owner: ClassSymbol,
    field: FieldSig,
    origin: str,
    lookup: Callable[..., ClassSymbol | None] | None,
    entity: bool,
) -> EqualityMember:
    equal_value, different_value, shared_reference, nested_resolved = (
        _equality_witnesses(field.type, lookup, owner)
    )
    annotations = _effective_field_annotations(owner, field)
    mutator = _verified_field_mutator(owner, field)
    assignable = bool(mutator) or _field_constructor_assignable(owner, field)
    identifier = bool(annotations & _ID_ANNOTATIONS)
    generated = "GeneratedValue" in annotations
    relationship = bool(annotations & _RELATIONSHIP_ANNOTATIONS)
    if entity and _is_collection_or_array_type(field.type):
        relationship = True
    return EqualityMember(
        name=field.name,
        type_name=field.type,
        declaring_type=field.declaring_type or owner.fqcn or owner.name,
        origin=origin,
        accessor=_verified_field_accessor(owner, field),
        mutator=mutator,
        assignable=assignable,
        equal_value=equal_value,
        different_value=different_value,
        shared_reference=shared_reference,
        nested_equality_resolved=nested_resolved,
        identifier=identifier,
        generated_identifier=generated,
        relationship=relationship,
    )


def _equality_witnesses(
    type_name: str,
    lookup: Callable[..., ClassSymbol | None] | None,
    owner: ClassSymbol,
) -> tuple[str | None, str | None, bool, bool]:
    normalized = _normalized_reference_type(type_name)
    simple = normalized.rsplit(".", 1)[-1]
    values: dict[str, tuple[str, str]] = {
        "String": ('"A"', '"B"'),
        "CharSequence": ('"A"', '"B"'),
        "boolean": ("false", "true"),
        "Boolean": ("Boolean.FALSE", "Boolean.TRUE"),
        "byte": ("(byte) 1", "(byte) 2"),
        "Byte": ("Byte.valueOf((byte) 1)", "Byte.valueOf((byte) 2)"),
        "short": ("(short) 1", "(short) 2"),
        "Short": ("Short.valueOf((short) 1)", "Short.valueOf((short) 2)"),
        "int": ("1", "2"),
        "Integer": ("Integer.valueOf(1)", "Integer.valueOf(2)"),
        "long": ("1L", "2L"),
        "Long": ("Long.valueOf(1L)", "Long.valueOf(2L)"),
        "float": ("1.0f", "2.0f"),
        "Float": ("Float.valueOf(1.0f)", "Float.valueOf(2.0f)"),
        "double": ("1.0d", "2.0d"),
        "Double": ("Double.valueOf(1.0d)", "Double.valueOf(2.0d)"),
        "char": ("'A'", "'B'"),
        "Character": ("Character.valueOf('A')", "Character.valueOf('B')"),
        "BigDecimal": ('new BigDecimal("10.00")', 'new BigDecimal("11.00")'),
        "BigInteger": ('new BigInteger("10")', 'new BigInteger("11")'),
        "LocalDate": (
            "LocalDate.of(2024, 1, 1)",
            "LocalDate.of(2024, 1, 2)",
        ),
        "LocalDateTime": (
            "LocalDateTime.of(2024, 1, 1, 0, 0)",
            "LocalDateTime.of(2024, 1, 2, 0, 0)",
        ),
        "LocalTime": ("LocalTime.of(10, 0)", "LocalTime.of(11, 0)"),
        "Instant": (
            "Instant.ofEpochSecond(1L)",
            "Instant.ofEpochSecond(2L)",
        ),
        "Date": ("new Date(0L)", "new Date(86400000L)"),
        "UUID": (
            'UUID.fromString("00000000-0000-0000-0000-000000000001")',
            'UUID.fromString("00000000-0000-0000-0000-000000000002")',
        ),
    }
    if simple in values:
        first, second = values[simple]
        return first, second, False, True

    if _is_collection_or_array_type(type_name):
        if "Map" in type_name:
            return "new java.util.LinkedHashMap<>()", "null", True, False
        if "Set" in type_name:
            return "new java.util.LinkedHashSet<>()", "null", True, False
        if "[]" in type_name:
            component = type_name.replace("[]", "").strip()
            return f"new {component}[0]", "null", True, False
        return "new java.util.ArrayList<>()", "null", True, False

    nested = _lookup_equality_symbol(lookup, normalized, owner)
    if nested is not None and nested.kind == "enum" and nested.enum_constants:
        different = (
            f"{nested.name}.{nested.enum_constants[1]}"
            if len(nested.enum_constants) >= 2
            else None
        )
        return (
            f"{nested.name}.{nested.enum_constants[0]}",
            different,
            False,
            True,
        )
    if simple == "Object":
        return "new Object()", "null", True, False
    if nested is not None and _has_verified_no_arg_construction(nested):
        # Outer equality tests deliberately do not depend on the nested type's
        # equality contract. Reuse one instance in the equal pair.
        return f"new {nested.name}()", "null", True, False
    return None, None, True, False


def _verified_field_accessor(owner: ClassSymbol, field: FieldSig) -> str | None:
    candidates = [field.name] if owner.kind == "record" else [
        _getter_name(field.name, field.type),
        f"get{field.name[:1].upper()}{field.name[1:]}",
    ]
    for method in owner.methods:
        if (
            method.name in candidates
            and not method.params
            and "private" not in method.modifiers
        ):
            return method.name
    if "public" in field.modifiers:
        return "<direct-field>"
    return None


def _verified_field_mutator(owner: ClassSymbol, field: FieldSig) -> str | None:
    if "final" in field.modifiers:
        return None
    setter = _setter_name(field.name, field.type)
    for method in owner.methods:
        if method.name not in {setter, field.name} or len(method.params) != 1:
            continue
        if (
            "public" in method.modifiers
            and _same_erased_type(method.params[0][0], field.type)
        ):
            return method.name
    if "public" in field.modifiers and "final" not in field.modifiers:
        return "<direct-field>"
    return None


def _field_constructor_assignable(owner: ClassSymbol, field: FieldSig) -> bool:
    if owner.kind == "record":
        return True
    if _has_lombok_annotation(owner, "Value") and not owner.constructors:
        return True
    if (
        _has_lombok_annotation(owner, "Data")
        and not owner.constructors
        and (
            "final" in field.modifiers
            or "NonNull" in _simple_annotation_names(field.annotations)
        )
    ):
        return True
    if any(
        "private" not in constructor.modifiers
        and any(
            name == field.name and _same_erased_type(parameter_type, field.type)
            for parameter_type, name in constructor.params
        )
        for constructor in owner.constructors
    ):
        return True
    return any(method.name == "builder" and method.is_static for method in owner.methods)


def _has_verified_no_arg_construction(symbol: ClassSymbol) -> bool:
    if symbol.kind != "class" or "abstract" in symbol.modifiers:
        return False
    if any(
        not constructor.params and "private" not in constructor.modifiers
        for constructor in symbol.constructors
    ):
        return True
    if symbol.constructors:
        return False
    if _has_lombok_annotation(symbol, "Value"):
        return False
    return not any(
        "final" in field.modifiers and field.initializer is None
        for field in symbol.fields
    )


def _explicit_object_methods(symbol: ClassSymbol, name: str) -> list[MethodSig]:
    methods = [
        method
        for method in symbol.methods
        if method.name == name
        and "LombokGenerated" not in _simple_annotation_names(method.annotations)
        and "JavaGenerated" not in _simple_annotation_names(method.annotations)
    ]
    if name == "equals":
        return [
            method
            for method in methods
            if len(method.params) == 1
            and _normalized_reference_type(method.params[0][0]).rsplit(".", 1)[-1]
            == "Object"
        ]
    if name == "hashCode":
        return [method for method in methods if not method.params]
    return methods


def _visible_manual_fields(symbol: ClassSymbol, method_source: str) -> set[str]:
    if not method_source:
        return set()
    visible: set[str] = set()
    for field in symbol.fields:
        if "static" in field.modifiers:
            continue
        bean_suffix = field.name[:1].upper() + field.name[1:]
        patterns = (
            rf"\b(?:this\s*\.\s*)?{re.escape(field.name)}\b",
            rf"\b(?:get|is){re.escape(bean_suffix)}\s*\(",
        )
        if any(re.search(pattern, method_source) for pattern in patterns):
            visible.add(field.name)
    return visible


def _unsupported_manual_calls(
    symbol: ClassSymbol, equals_source: str, hash_source: str
) -> set[str]:
    allowed = {
        "equals", "hash", "hashCode", "getClass", "canEqual", "deepEquals",
        "compare", "compareTo",
    }
    for field in symbol.fields:
        suffix = field.name[:1].upper() + field.name[1:]
        allowed.update({f"get{suffix}", f"is{suffix}"})
    ignored = {"if", "for", "while", "switch", "return", "new", "instanceof"}
    calls = set(
        re.findall(
            r"\b([A-Za-z_$][\w$]*)\s*\(",
            equals_source + "\n" + hash_source,
        )
    )
    unsupported = {
        call for call in calls if call not in allowed and call not in ignored
    }
    if re.search(r"\bsuper\s*\.\s*equals\s*\(", equals_source):
        unsupported.add("super.equals")
    return unsupported


def _manual_unrelated_type_safe(equals_source: str) -> bool:
    return bool(
        re.search(r"\binstanceof\b|\.\s*getClass\s*\(", equals_source or "")
    )


def _method_source(source: str, method: MethodSig) -> str:
    if not source:
        return ""
    matches = list(re.finditer(
        rf"\b{re.escape(method.name)}\s*\([^)]*\)[^{{;]*\{{", source
    ))
    if not matches:
        lines = source.splitlines()
        if method.line and 1 <= method.line <= len(lines):
            end = method.end_line or method.line
            return "\n".join(lines[method.line - 1:min(len(lines), end)])
        return ""
    if method.line:
        starts = _line_starts(source)
        target = starts[min(len(starts), max(1, method.line)) - 1]
        match = min(matches, key=lambda candidate: abs(candidate.start() - target))
    else:
        match = matches[0]
    open_brace = source.find("{", match.start())
    depth = 0
    masked = _mask_comments_and_literals(source)
    for index in range(open_brace, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    return ""


def _simple_included_method_field(
    method: MethodSig, source: str, symbol: ClassSymbol
) -> str | None:
    if method.params or not method.return_type or method.return_type == "void":
        return None
    method_source = _method_source(source, method)
    body_match = re.search(r"\{(?P<body>[\s\S]*)\}", method_source)
    if body_match is None:
        return None
    body = re.sub(
        r"//.*?$|/\*[\s\S]*?\*/",
        "",
        body_match.group("body"),
        flags=re.MULTILINE,
    ).strip()
    direct = re.fullmatch(
        r"return\s+(?:this\s*\.\s*)?([A-Za-z_$][\w$]*)\s*;", body
    )
    if direct and any(field.name == direct.group(1) for field in symbol.fields):
        return direct.group(1)
    getter = re.fullmatch(
        r"return\s+(?:this\s*\.\s*)?(?:get|is)([A-Z][\w$]*)\s*\(\s*\)\s*;",
        body,
    )
    if getter:
        candidate = getter.group(1)[:1].lower() + getter.group(1)[1:]
        if any(field.name == candidate for field in symbol.fields):
            return candidate
    return None


def _class_annotation_expression(
    symbol: ClassSymbol, simple_name: str
) -> str | None:
    for expression in symbol.annotation_exprs or []:
        if re.match(
            rf"@(?:lombok\.)?{re.escape(simple_name)}\b", expression.strip()
        ):
            return expression
    return None


def _method_annotation_expression(
    method: MethodSig, member_name: str
) -> str | None:
    for expression in method.annotation_exprs or []:
        if re.search(
            rf"(?:EqualsAndHashCode\s*\.\s*)?{re.escape(member_name)}\b",
            expression,
        ):
            return expression
    return None


def _has_lombok_annotation(symbol: ClassSymbol, simple_name: str) -> bool:
    if _class_annotation_expression(symbol, simple_name) is not None:
        return True
    if simple_name not in _simple_annotation_names(symbol.annotations):
        return False
    imports = {
        value.removeprefix("static ") for value in (symbol.imports or [])
    }
    if f"lombok.{simple_name}" in imports or "lombok.*" in imports:
        return True
    conflicting = any(
        value.endswith(f".{simple_name}") and not value.startswith("lombok.")
        for value in imports
    )
    return not conflicting


def _has_equality_member_annotation(
    field: FieldSig, member_name: str
) -> bool:
    expressions = field.annotation_exprs or []
    if any(
        re.search(
            rf"EqualsAndHashCode\s*\.\s*{re.escape(member_name)}\b",
            value,
        )
        for value in expressions
    ):
        return True
    if any(
        re.search(
            rf"ToString\s*\.\s*{re.escape(member_name)}\b", value
        )
        for value in expressions
    ):
        return False
    return member_name in _simple_annotation_names(field.annotations)


def _has_equality_method_annotation(
    method: MethodSig, member_name: str
) -> bool:
    expressions = method.annotation_exprs or []
    if any(
        re.search(
            rf"EqualsAndHashCode\s*\.\s*{re.escape(member_name)}\b",
            value,
        )
        for value in expressions
    ):
        return True
    if any(
        re.search(
            rf"ToString\s*\.\s*{re.escape(member_name)}\b", value
        )
        for value in expressions
    ):
        return False
    return member_name in _simple_annotation_names(method.annotations)


def _annotation_string_attribute(expression: str, name: str) -> str | None:
    match = re.search(
        rf'\b{re.escape(name)}\s*=\s*"([^"]+)"', expression or ""
    )
    return match.group(1) if match else None


def _identifier_fields(symbol: ClassSymbol) -> list[FieldSig]:
    return [
        field
        for field in symbol.fields
        if _effective_field_annotations(symbol, field) & _ID_ANNOTATIONS
    ]


def _effective_field_annotations(
    owner: ClassSymbol, field: FieldSig
) -> set[str]:
    annotations = _simple_annotation_names(field.annotations)
    suffix = field.name[:1].upper() + field.name[1:]
    accessor_names = {
        field.name,
        _getter_name(field.name, field.type),
        f"get{suffix}",
        f"is{suffix}",
    }
    for method in owner.methods:
        if method.name in accessor_names and not method.params:
            annotations.update(_simple_annotation_names(method.annotations))
    return annotations


def _inherited_members(
    members: list[EqualityMember],
) -> list[EqualityMember]:
    return [
        EqualityMember(
            name=member.name,
            type_name=member.type_name,
            declaring_type=member.declaring_type,
            origin=f"inherited:{member.origin}",
            accessor=member.accessor,
            mutator=member.mutator,
            assignable=bool(member.mutator),
            equal_value=member.equal_value,
            different_value=member.different_value,
            shared_reference=member.shared_reference,
            nested_equality_resolved=member.nested_equality_resolved,
            identifier=member.identifier,
            generated_identifier=member.generated_identifier,
            relationship=member.relationship,
        )
        for member in members
    ]


def _lookup_equality_symbol(
    lookup: Callable[..., ClassSymbol | None] | None,
    type_name: str,
    owner: ClassSymbol,
) -> ClassSymbol | None:
    if lookup is None or not type_name:
        return None
    try:
        return lookup(type_name, owner)
    except TypeError:
        try:
            return lookup(type_name)
        except (KeyError, TypeError):
            return None
    except KeyError:
        return None


def _source_for_equality_symbol(symbol: ClassSymbol) -> str:
    path_value = getattr(symbol, "source_path", None)
    if not path_value:
        return ""
    try:
        return Path(path_value).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _normalized_reference_type(type_name: str) -> str:
    value = (type_name or "").strip()
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"<[^<>]*>", "", value)
    return value.replace("[]", "").replace("...", "").strip()


def _same_erased_type(left: str, right: str) -> bool:
    return (
        _normalized_reference_type(left).rsplit(".", 1)[-1]
        == _normalized_reference_type(right).rsplit(".", 1)[-1]
    )


def _is_collection_or_array_type(type_name: str) -> bool:
    raw = type_name or ""
    simple = _normalized_reference_type(raw).rsplit(".", 1)[-1]
    return "[]" in raw or simple in {
        "List", "Set", "Map", "Collection", "Iterable", "Queue", "Deque",
        "ArrayList", "LinkedList", "HashSet", "LinkedHashSet", "HashMap",
        "LinkedHashMap",
    }


def _simple_annotation_names(values: list[str] | None) -> set[str]:
    return {
        str(value).split(".")[-1].lstrip("@") for value in (values or [])
    }
