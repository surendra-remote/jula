"""Parse jacoco.xml into deterministic ClassCoverage records."""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from junitforge.models import ClassCoverage, CoverageReport, LineCoverage, MethodCoverage


def _counter(el, ctype: str) -> tuple[int, int]:
    for c in el.findall("counter"):
        if c.get("type") == ctype:
            return int(c.get("missed", 0)), int(c.get("covered", 0))
    return 0, 0


def parse_jacoco_xml(
    xml_path: Path,
    module_name: str = "",
    module_root: Path | None = None,
) -> list[ClassCoverage]:
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=True,
    )

    try:
        tree = etree.parse(str(xml_path), parser)
    except Exception:
        return []

    root = tree.getroot()
    if root is None:
        return []

    out: list[ClassCoverage] = []

    for pkg in root.findall("package"):
        pkg_name = pkg.get("name", "")

        source_lines: dict[str, dict[int, LineCoverage]] = {}

        for sf in pkg.findall("sourcefile"):
            sf_name = sf.get("name", "")
            lines: dict[int, LineCoverage] = {}

            for ln in sf.findall("line"):
                nr = int(ln.get("nr", 0))
                if nr <= 0:
                    continue

                lines[nr] = LineCoverage(
                    nr=nr,
                    mi=int(ln.get("mi", 0)),
                    ci=int(ln.get("ci", 0)),
                    mb=int(ln.get("mb", 0)),
                    cb=int(ln.get("cb", 0)),
                )

            source_lines[sf_name] = lines

        for cls in pkg.findall("class"):
            binary = cls.get("name", "")
            sourcefilename = cls.get("sourcefilename", "")
            fqcn = binary.replace("/", ".")

            methods: list[MethodCoverage] = []

            for m in cls.findall("method"):
                mi, ci = _counter(m, "INSTRUCTION")
                mb, cb = _counter(m, "BRANCH")
                ml, cl = _counter(m, "LINE")

                methods.append(
                    MethodCoverage(
                        name=m.get("name", ""),
                        desc=m.get("desc", ""),
                        first_line=int(m.get("line", 0)) or None,
                        missed_instr=mi,
                        covered_instr=ci,
                        missed_branch=mb,
                        covered_branch=cb,
                        missed_line=ml,
                        covered_line=cl,
                    )
                )

            lines = source_lines.get(sourcefilename, {})

            executable_lines = {
                nr: lc for nr, lc in lines.items()
                if (lc.mi + lc.ci + lc.mb + lc.cb) > 0
            }

            missed_line = sum(1 for lc in executable_lines.values() if lc.ci == 0 and lc.mi > 0)
            covered_line = sum(1 for lc in executable_lines.values() if lc.ci > 0)

            missed_branch = sum(lc.mb for lc in executable_lines.values())
            covered_branch = sum(lc.cb for lc in executable_lines.values())

            missed_instr, covered_instr = _counter(cls, "INSTRUCTION")

            cc = ClassCoverage(
                fqcn=fqcn,
                binary_name=binary,
                module=module_name,
                methods=methods,
                lines=dict(executable_lines),
                missed_instr=missed_instr,
                covered_instr=covered_instr,
                missed_branch=missed_branch,
                covered_branch=covered_branch,
                missed_line=missed_line,
                covered_line=covered_line,
            )

            if module_root is not None and sourcefilename:
                cc.source_file = _resolve_source(module_root, pkg_name, sourcefilename)

            out.append(cc)

    return out


def _resolve_source(module_root: Path, pkg_name: str, sourcefilename: str) -> Path | None:
    candidate = module_root / "src" / "main" / "java" / pkg_name / sourcefilename
    if candidate.exists():
        return candidate

    for p in module_root.rglob(sourcefilename):
        if "src/main/java" in str(p).replace("\\", "/"):
            return p

    return None


def parse_modules(module_xmls: list[tuple[str, Path, Path]]) -> CoverageReport:
    report = CoverageReport()

    for name, root, xml in module_xmls:
        if not xml.exists():
            continue

        for cc in parse_jacoco_xml(xml, name, root):
            report.classes[cc.fqcn] = cc

    return report