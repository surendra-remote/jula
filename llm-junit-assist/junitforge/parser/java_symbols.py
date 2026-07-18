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
from typing import Any

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

from junitforge.models import ClassSymbol, FieldSig, JavaSourceFile, MethodSig

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
                return _parse_with_javaparser_cli(path, source, jar)
            except Exception as exc:  # noqa: BLE001
                cli_error = f"JavaParser CLI failed: {type(exc).__name__}: {str(exc)[:240]}"

    jf = parse_source(path, source)
    if cli_error:
        if jf.parse_error:
            jf.parse_error = f"{cli_error}; {jf.parse_error}"
        else:
            jf.parse_error = cli_error
    return jf


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
        extends=_none_if_blank(data.get("extends")),
        implements=_str_list(data.get("implements")),
        annotations=_simple_names(_str_list(data.get("annotations"))),
        fields=[_field_sig_from_cli(f) for f in _dict_list(data.get("fields"))],
        constructors=[_method_sig_from_cli(c, constructor=True, owner=name, owner_kind=kind) for c in _dict_list(data.get("constructors"))],
        methods=[_method_sig_from_cli(m, constructor=False, owner=name, owner_kind=kind) for m in _dict_list(data.get("methods"))],
        nested=[_class_symbol_from_cli(n, pkg) for n in _dict_list(data.get("nested"))],
        enum_constants=_str_list(data.get("enumConstants") or data.get("enum_constants")),
        line=_int_or_none(data.get("line")),
        end_line=_int_or_none(data.get("endLine") or data.get("end_line")),
    )

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

    sym = ClassSymbol(
        name=name,
        fqcn=fqcn,
        kind=kind,
        modifiers=set(getattr(node, "modifiers", None) or set()),
        extends=_first_type_name(getattr(node, "extends", None)),
        implements=[n for n in (_type_name(i) for i in (getattr(node, "implements", None) or [])) if n],
        annotations=annotations,
        line=start,
        end_line=_end_line(source, line_starts, start) if start else None,
    )

    is_controller = any(a.split(".")[-1] in {"RestController", "Controller"} for a in annotations)

    for member in getattr(node, "body", None) or []:
        if isinstance(member, J.FieldDeclaration):
            sym.fields.extend(_fields(member))
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
    )


def _fields(node) -> list[FieldSig]:
    t = type_to_str(node.type) or "Object"
    mods = set(getattr(node, "modifiers", None) or set())
    annos = [_anno_name(a) for a in (getattr(node, "annotations", None) or [])]
    line = node.position.line if node.position else None
    return [FieldSig(name=d.name, type=t, modifiers=set(mods), annotations=list(annos), annotation_exprs=[], line=line) for d in node.declarators]


def _anno_name(a) -> str:
    name = getattr(a, "name", None)
    return str(name or a).split(".")[-1]


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
    kind = "annotation" if raw_kind == "@interface" else ("class" if raw_kind == "record" else raw_kind)
    name = type_match.group("name")
    tail = type_match.group("tail") or ""
    line_starts = _line_starts(source)
    start_line = _line_of(type_match.start(), line_starts)
    end_line = _end_line(source, line_starts, start_line)

    annotations = [a.split(".")[-1] for a in _ANNOTATION_RE.findall(type_match.group("prefix") or "")]
    if not annotations:
        annotations = _class_annotations_before(masked, type_match.start())
    modifiers = set((type_match.group("mods") or "").split()) & _MODIFIERS
    if "public" not in modifiers and re.search(rf"\bpublic\s+(?:class|interface|enum|record)\s+{re.escape(name)}\b", masked):
        modifiers.add("public")

    ext = None
    ext_m = re.search(r"\bextends\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)", tail)
    if ext_m:
        ext = ext_m.group(1).split(".")[-1]

    implements: list[str] = []
    impl_m = re.search(r"\bimplements\s+([^\{]+)", tail)
    if impl_m:
        implements = [_base_type(x.strip()) for x in impl_m.group(1).split(",") if x.strip()]

    sym = ClassSymbol(
        name=name,
        fqcn=f"{pkg}.{name}" if pkg else name,
        kind=kind,
        modifiers=modifiers,
        extends=ext,
        implements=implements,
        annotations=annotations,
        line=start_line,
        end_line=end_line,
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
    return [FieldSig(name=n, type=field_type, modifiers=set(mods), annotations=list(annos), annotation_exprs=[], line=line) for n in names]


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
    return MethodSig(name=name, modifiers=mods, return_type=return_type, params=params, throws=throws, annotations=annos, line=line, end_line=_line_of(member_start + len(member), line_starts), is_constructor=is_constructor)


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
    depth = 0
    for i, c in enumerate(params):
        if c == "<":
            depth += 1
        elif c == ">" and depth > 0:
            depth -= 1
        elif c == "," and depth == 0:
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
