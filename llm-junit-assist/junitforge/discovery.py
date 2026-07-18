"""Repository discovery: modules, in-scope source classes, test paths, and a
fully qualified repo-wide symbol index for accurate collaborator resolution.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from junitforge.models import ClassSymbol, JavaSourceFile, ModuleInfo
from junitforge.parser.java_symbols import parse_file

_IGNORE = {".git", "target", "build", "node_modules", ".idea", ".mvn"}


def _strip_ns(text: str) -> str:
    return re.sub(r'\sxmlns="[^"]+"', "", text, count=1)


def _artifact_id(pom: Path) -> str:
    try:
        root = ET.fromstring(_strip_ns(pom.read_text(encoding="utf-8", errors="replace")))
        a = root.findtext("artifactId")
        if a:
            return a.strip()
    except Exception:
        pass
    return pom.parent.name


def discover_modules(repo_root: Path) -> list[ModuleInfo]:
    """Every directory with a pom.xml AND a src/main/java tree is recognized as a valid module."""
    modules: list[ModuleInfo] = []
    for pom in sorted(repo_root.rglob("pom.xml")):
        if any(part in _IGNORE for part in pom.parts):
            continue
        root = pom.parent
        if not (root / "src" / "main" / "java").exists():
            continue
        modules.append(ModuleInfo(name=_artifact_id(pom), root=root, pom=pom))
    return modules


def module_for_source(modules: list[ModuleInfo], src: Path) -> ModuleInfo | None:
    best: tuple[int, ModuleInfo] | None = None
    src = src.resolve()
    for m in modules:
        try:
            src.relative_to(m.root.resolve())
        except ValueError:
            continue
        depth = len(m.root.resolve().parts)
        if best is None or depth > best[0]:
            best = (depth, m)
    return best[1] if best else None


def iter_source_files(module: ModuleInfo) -> list[Path]:
    base = module.root / "src" / "main" / "java"
    if not base.exists():
        return []
    out: list[Path] = []
    for p in sorted(base.rglob("*.java")):
        if p.name in ("package-info.java", "module-info.java"):
            continue
        out.append(p)
    return out


def is_test_target(jf: JavaSourceFile) -> tuple[bool, str]:
    """Decide whether a parsed source file is worth generating a unit test for."""
    t = jf.primary_type
    if t is None:
        return False, "no type / parse failed"
    if t.kind in ("interface", "annotation"):
        return False, f"{t.kind} (no implementation to unit-test)"
    if "abstract" in t.modifiers:
        return False, "abstract class"
        
    # Application bootstrap classes (just main()) are skipped
    if any(a.split(".")[-1] == "SpringBootApplication" for a in t.annotations) and len(t.methods) <= 1:
        return False, "Spring Boot application bootstrap layer skipped"
        
    public_methods = [m for m in t.methods if m.is_public]
    if not public_methods and not t.constructors:
        return False, "no public methods detected for test capture"
    return True, "ok"


def test_path_for(source_path: Path, module: ModuleInfo, symbol: ClassSymbol) -> Path:
    """Places generated tests inside standard Maven test package directory frameworks."""
    pkg_parts = symbol.fqcn.rsplit(".", 1)[0].split(".") if "." in symbol.fqcn else []
    test_root = module.root / "src" / "test" / "java"
    return test_root.joinpath(*pkg_parts) / f"{symbol.name}Test.java"


def test_package_for(symbol: ClassSymbol) -> str:
    return symbol.fqcn.rsplit(".", 1)[0] if "." in symbol.fqcn else ""


def build_symbol_index(modules: list[ModuleInfo]) -> dict[str, ClassSymbol]:
    """Builds an enterprise-grade symbol indexing structure map.
    
    CRITICAL FIX: Map BOTH fully qualified class names (FQCN) and unique simple names 
    to ensure overlapping object structures across sibling packages never corrupt 
    the collaborator method signature generation templates.
    """
    index: dict[str, ClassSymbol] = {}
    for m in modules:
        for p in iter_source_files(m):
            jf = parse_file(p)
            for t in jf.types:
                # 1. Primary Mapping: Absolute Fully Qualified Class Name (Guarantees Uniqueness)
                index[t.fqcn] = t
                # 2. Secondary Mapping: Simple Class Name Fallback (If not already occupied by another subpackage)
                index.setdefault(t.name, t)
    return index
