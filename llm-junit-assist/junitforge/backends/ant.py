"""Ant / Eclipse build backend (no Maven).

Drives compilation with javac and test-execution + coverage with the JUnit
Platform Console Launcher + the JaCoCo java agent / CLI. Classpath is harvested
from the repo's jars (ThirdParty/, lib/, Eclipse .classpath references). Exposes
the same surface the engine expects from the Maven CompileGate / CoverageRunner,
so the rest of junitforge is unchanged.

The whole repo is treated as one module for simplicity.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from junitforge.compile_gate import normalize_release, parse_javac_errors
from junitforge.coverage_run import CoverageRunResult
from junitforge.models import CompileError, ModuleInfo
from junitforge.toolchain import Toolchain
from junitforge.vendor.javac import CompileResult, JavacRunner, join_classpath
from junitforge.vendor.logging_utils import get

log = get(__name__)

_IGNORE = {".git", "target", "build", "bin", "out", "node_modules", ".junitforge"}


def is_ant_repo(repo_root: Path) -> bool:
    return (repo_root / "build.xml").exists() and not (repo_root / "pom.xml").exists()


def discover_ant_modules(repo_root: Path) -> list[ModuleInfo]:
    return [ModuleInfo(name=repo_root.name, root=repo_root, pom=repo_root / "build.xml")]


def ant_source_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(repo_root.rglob("*.java")):
        parts = set(p.parts)
        if parts & _IGNORE:
            continue
        if p.name in ("package-info.java", "module-info.java"):
            continue
        # skip anything that already looks like a test
        if p.name.endswith("Test.java") or "test" in [s.lower() for s in p.parts]:
            continue
        out.append(p)
    return out


def detect_release(repo_root: Path) -> str | None:
    """Pick a javac --release from an Eclipse JRE_CONTAINER (JavaSE-17) if present."""
    for cp in repo_root.rglob(".classpath"):
        try:
            text = cp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r"JavaSE-(\d+)", text)
        if m:
            return normalize_release(m.group(1))
    return None


def ant_test_path(source_path: Path, module: ModuleInfo, symbol) -> Path:
    """Generated tests live under .junitforge/gen-tests mirroring the package."""
    pkg_parts = symbol.fqcn.rsplit(".", 1)[0].split(".") if "." in symbol.fqcn else []
    return module.root.joinpath(".junitforge", "gen-tests", *pkg_parts) / f"{symbol.name}Test.java"


def _is_valid_jar(p: Path) -> bool:
    try:
        if p.stat().st_size < 4:
            return False
        with p.open("rb") as fh:
            return fh.read(2) == b"PK"   # zip magic
    except OSError:
        return False


def harvest_jars(repo_root: Path) -> list[str]:
    jars: list[str] = []
    for j in sorted(repo_root.rglob("*.jar")):
        if set(j.parts) & {".git", ".junitforge"}:
            continue
        if not _is_valid_jar(j):
            log.warning("skipping invalid/empty jar: %s", j.name)
            continue
        jars.append(str(j))
    return jars


# ---------------------------------------------------------------------------
# Compile gate
# ---------------------------------------------------------------------------


@dataclass
class AntCompileGate:
    repo_root: Path
    cache_dir: Path
    toolchain: Toolchain
    java_release: str | None = None
    timeout_sec: int = 180

    _base_cp: list[str] = field(default_factory=list, init=False)
    _ready: bool = field(default=False, init=False)
    _setup_note: str = field(default="", init=False)

    @property
    def main_classes(self) -> Path:
        return self.cache_dir / "main-classes"

    @property
    def test_classes(self) -> Path:
        return self.cache_dir / "test-classes"

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def setup_note(self) -> str:
        return self._setup_note

    def __post_init__(self) -> None:
        self.java_release = normalize_release(self.java_release)

    def setup(self, modules: list[ModuleInfo]) -> bool:
        if shutil.which("javac") is None:
            self._setup_note = "javac not on PATH"
            return False
        if not self.toolchain.ok:
            self._setup_note = self.toolchain.note or "toolchain jars unavailable"
            return False
        self.main_classes.mkdir(parents=True, exist_ok=True)
        self.test_classes.mkdir(parents=True, exist_ok=True)
        jars = harvest_jars(self.repo_root)
        self._base_cp = jars + self.toolchain.test_classpath()

        sources = ant_source_files(self.repo_root)
        if not sources:
            self._setup_note = "no source files found"
            return False
        runner = JavacRunner(classpath=join_classpath(jars), output_dir=self.main_classes,
                             release=self.java_release, timeout_sec=self.timeout_sec)
        result = runner.compile_many(sources)
        # Even a partial main compile is usable (some .class files produced).
        produced = any(self.main_classes.rglob("*.class"))
        self._ready = produced
        if not produced:
            self._setup_note = "main sources did not compile: " + "; ".join(result.errors[:2])
        return self._ready

    def classpath_for(self, _module_name: str) -> str:
        return join_classpath([str(self.main_classes), str(self.test_classes), *self._base_cp])

    def check(self, test_file: Path, module: ModuleInfo,
              scratch_dir: Path | None = None) -> tuple[CompileResult, list[CompileError]]:
        if not self._ready:
            return (CompileResult(ok=False, errors=["ant compile gate not ready: " + self._setup_note]),
                    [CompileError(file=None, line=None, col=None,
                                  message="ant compile gate unavailable: " + self._setup_note)])
        runner = JavacRunner(classpath=self.classpath_for(module.name),
                             output_dir=self.test_classes, release=self.java_release,
                             timeout_sec=self.timeout_sec)
        result = runner.compile_one(test_file)
        errors = parse_javac_errors(result.stderr) if not result.ok else []
        own = [e for e in errors if e.file and e.file.name == test_file.name]
        final = own or errors
        if not result.ok and not final:
            msg = (result.stderr or "compile failed").strip().splitlines()
            final = [CompileError(file=None, line=None, col=None,
                                  message=(msg[0] if msg else "compile failed")[:200])]
        return result, final


# ---------------------------------------------------------------------------
# Coverage runner (JUnit console launcher + JaCoCo agent/cli)
# ---------------------------------------------------------------------------


@dataclass
class AntCoverageRunner:
    repo_root: Path
    cache_dir: Path
    toolchain: Toolchain
    gate: AntCompileGate
    timeout_sec: int = 300

    def available(self) -> bool:
        return shutil.which("java") is not None and self.toolchain.ok

    def warm_cache(self) -> None:
        return None

    def xml_path(self, _module: ModuleInfo) -> Path:
        return self.cache_dir / "jacoco" / "jacoco.xml"

    def exec_path(self, _module: ModuleInfo) -> Path:
        return self.cache_dir / "jacoco" / "jacoco.exec"

    def measure(self, module: ModuleInfo, test_classes: list[str] | None = None) -> CoverageRunResult:
        if not self.available():
            return CoverageRunResult(False, False, "java/toolchain unavailable")
        if not test_classes:
            return CoverageRunResult(False, False, "no test class specified")
        java = shutil.which("java") or "java"
        exec_p = self.exec_path(module)
        xml_p = self.xml_path(module)
        exec_p.parent.mkdir(parents=True, exist_ok=True)
        for stale in (exec_p, xml_p):
            try:
                stale.unlink()
            except OSError:
                pass

        run_cp = join_classpath([str(self.gate.main_classes), str(self.gate.test_classes),
                                 *harvest_jars(self.repo_root), *self.toolchain.test_classpath()])
        select = []
        for fqcn in test_classes:
            select += ["-c", fqcn]
        run_cmd = [
            java, f"-javaagent:{self.toolchain.jacoco_agent}=destfile={exec_p}",
            "-cp", run_cp,
            "org.junit.platform.console.ConsoleLauncher", "execute",
            *select, "--disable-banner", "--details=none",
        ]
        try:
            proc = subprocess.run(run_cmd, capture_output=True, text=True,
                                  timeout=self.timeout_sec, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return CoverageRunResult(False, False, f"junit run failed: {exc}")
        blob = (proc.stdout or "") + (proc.stderr or "")
        if not exec_p.exists() or exec_p.stat().st_size == 0:
            return CoverageRunResult(False, False, "jacoco.exec missing/empty - coverage not trustworthy",
                                     log_tail=blob[-2000:])

        # jacoco report: needs classfiles + sourcefiles roots
        src_roots = _source_roots(self.repo_root)
        rep_cmd = [java, "-jar", str(self.toolchain.jacoco_cli), "report", str(exec_p),
                   "--classfiles", str(self.gate.main_classes), "--xml", str(xml_p)]
        for sr in src_roots:
            rep_cmd += ["--sourcefiles", sr]
        try:
            rproc = subprocess.run(rep_cmd, capture_output=True, text=True,
                                   timeout=self.timeout_sec, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return CoverageRunResult(False, False, f"jacoco report failed: {exc}", log_tail=blob[-1000:])
        if not xml_p.exists():
            return CoverageRunResult(False, False, "jacoco.xml not generated",
                                     log_tail=((rproc.stderr or "") + blob)[-2000:])
        return CoverageRunResult(True, True, "ok", xml_path=xml_p, log_tail=blob[-1500:])


def _source_roots(repo_root: Path) -> list[str]:
    roots: set[str] = set()
    for f in ant_source_files(repo_root):
        # walk up to the dir that starts the package path; heuristic: a 'src' dir.
        parts = f.parts
        if "src" in parts:
            i = len(parts) - 1 - parts[::-1].index("src")
            roots.add(str(Path(*parts[: i + 1])))
        else:
            roots.add(str(f.parent))
    return sorted(roots)
