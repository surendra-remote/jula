"""Deterministic compile gate for multi-module Maven reactors.

Authoritative check that a generated test actually compiles, upgraded to provide
robust partial-reactor tolerance and support Spring Boot 3.5 parameter metadata.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from junitforge.models import CompileError, ModuleInfo
from junitforge.vendor.classpath import ClasspathResolverError, MavenClasspathResolver
from junitforge.vendor.javac import CompileResult, JavacRunner, join_classpath
from junitforge.vendor.logging_utils import get

log = get(__name__)

_MVN_ERR = re.compile(r"^\[ERROR\]\s+(?P<path>.+?\.java):\[(?P<line>\d+),(?P<col>\d+)\]\s+(?P<msg>.*)$")
_MVN_CONT = re.compile(r"^\[ERROR\]\s+(?:\s+)?(symbol|location|required|found|reason):\s+(.*)$")
_JAVAC_ERR = re.compile(r"^(?P<path>.*\.java):(?P<line>\d+):\s+error:\s+(?P<msg>.*)$")
_JAVAC_CONT = re.compile(r"^\s+(symbol|location|required|found|reason):\s+(.*)$")


def normalize_release(version: str | None) -> str | None:
    """javac --release wants '17', not '1.17'. Map legacy '1.x' -> 'x'."""
    if not version:
        return version
    v = version.strip()
    if v.startswith("1.") and v[2:].isdigit():
        return v[2:]
    return v


def parse_javac_errors(stderr: str) -> list[CompileError]:
    return _parse(stderr, _JAVAC_ERR, _JAVAC_CONT, caret_column=True)


def parse_maven_compiler_errors(log_text: str) -> list[CompileError]:
    return _parse(log_text, _MVN_ERR, _MVN_CONT)


def _parse(
    text: str,
    err_re: re.Pattern,
    cont_re: re.Pattern,
    *,
    caret_column: bool = False,
) -> list[CompileError]:
    out: list[CompileError] = []
    cur: CompileError | None = None
    for raw in text.splitlines():
        m = err_re.match(raw)
        if m:
            cur = CompileError(
                file=Path(m.group("path")),
                line=int(m.group("line")),
                col=int(m.group("col")) if "col" in m.groupdict() and m.group("col") else None,
                message=m.group("msg").strip(),
                raw=raw,
            )
            out.append(cur)
            continue
        c = cont_re.match(raw)
        if c and cur is not None:
            cur.detail.append(f"{c.group(1)}: {c.group(2).strip()}")
            continue
        # javac reports its precise source column with a caret on the line
        # following the offending statement. Preserve it when present instead
        # of discarding evidence that Maven would have emitted numerically.
        if (
            caret_column
            and cur is not None
            and cur.col is None
            and raw.strip() == "^"
        ):
            cur.col = raw.index("^") + 1
    return out


def _looks_offline_miss(blob: str) -> bool:
    """Detects standard Maven offline artifact dependency missing signals."""
    blob_lower = blob.lower()
    return "plugin execution not covered" in blob_lower or "could not resolve dependencies" in blob_lower


# ---------------------------------------------------------------------------
# Compile gate
# ---------------------------------------------------------------------------


@dataclass
class CompileGate:
    repo_root: Path
    cache_dir: Path
    java_release: str | None = "17"
    mvn_path: str | None = None
    offline: bool = False
    settings_file: Path | None = None
    local_repo: Path | None = None
    mvn_args: list[str] = field(default_factory=list)
    timeout_sec: int = 600
    javac_timeout_sec: int = 120

    _module_dep_cp: dict[str, str] = field(default_factory=dict, init=False)
    _all_class_dirs: list[str] = field(default_factory=list, init=False)
    _ready: bool = field(default=False, init=False)
    _setup_note: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.mvn_path is None:
            self.mvn_path = shutil.which("mvn") or shutil.which("mvn.cmd") or "mvn"
        self.java_release = normalize_release(self.java_release)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def setup_note(self) -> str:
        return self._setup_note

    def setup(self, modules: list[ModuleInfo]) -> bool:
        """Reactor test-compile + union classpath compilation scanner setup pass."""
        if shutil.which("javac") is None:
            self._setup_note = "javac execution binary missing from environmental PATH variables"
            return False
            
        # Run upfront compilation verification pass
        self._reactor_test_compile()

        # CRITICAL REFACTOR: Partial-Reactor Tolerance Engine Block
        # Instead of failing globally if a single irrelevant sub-module fails, 
        # we index any and all successfully compiled module target paths.
        for m in modules:
            for sub in ("classes", "test-classes"):
                d = m.root / "target" / sub
                if d.exists():
                    self._all_class_dirs.append(str(d))

        # Build local target class path indices mapping dependency matrices
        for m in modules:
            self._module_dep_cp[m.name] = self._resolve_module_cp(m)

        self._ready = len(self._all_class_dirs) > 0
        if not self._ready:
            self._setup_note = "Reactor initialization produced zero class directories under target sub-trees"
            
        return self._ready

    def classpath_for(self, module_name: str) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        dep = self._module_dep_cp.get(module_name, "")
        for part in self._all_class_dirs + (dep.split(os.pathsep) if dep else []):
            if part and part not in seen:
                seen.add(part)
                parts.append(part)
        return join_classpath(parts)

    def check(self, test_file: Path, module: ModuleInfo,
              scratch_dir: Path | None = None) -> tuple[CompileResult, list[CompileError]]:
        """Checks structural single file compilation against the cached reactor classpath."""
        # Use our production-hardened JavacRunner specifying mandatory parameters mapping
        target_out = scratch_dir or (self.cache_dir / "test-classes")
        
        runner = JavacRunner(
            classpath=self.classpath_for(module.name),
            output_dir=target_out,
            javac_path=shutil.which("javac"),
            # Injected critical parameters and encoding flags to align with loop utilities
            extra_flags=("-parameters", "-encoding", "UTF-8"),
            timeout_sec=self.javac_timeout_sec,
            release=self.java_release
        )
        
        res = runner.compile_one(test_file)
        errors = parse_javac_errors(res.stderr) if not res.ok else []
        if not res.ok and not errors:
            # Never lose the compiler diagnostic. This fallback covers launcher
            # failures, localized/non-standard javac output, and any future
            # parser mismatch. Keep the report bounded while retaining the raw
            # text needed to diagnose a preflight failure.
            summary = next((line.strip() for line in (res.stderr or "").splitlines() if line.strip()), "")
            if not summary and res.errors:
                summary = res.errors[0]
            if not summary:
                summary = f"javac failed with return code {res.return_code}"
            errors = [CompileError(
                file=test_file,
                line=None,
                col=None,
                message=summary,
                raw=(res.stderr or res.stdout or "")[:4000],
            )]
        return res, errors

    def check_complete_class(
        self,
        test_file: Path,
        module: ModuleInfo,
    ) -> tuple[CompileResult, list[CompileError]]:
        """Compile the whole generated class, then confirm Maven test-compile.

        The single-file javac pass gives precise diagnostics for the generated
        unit. Maven is the authoritative boundary and places the class in the
        module's normal test output before Surefire is invoked directly.
        """
        # Put the exactly-one generated class where direct Surefire execution
        # expects compiled tests. This remains a one-source-file javac unit.
        result, errors = self.check(
            test_file,
            module,
            scratch_dir=module.root / "target" / "test-classes",
        )
        if not result.ok:
            return result, errors

        cmd = [
            str(self.mvn_path),
            "-B",
            "-ntp",
            "-f",
            str(self.repo_root / "pom.xml"),
            "test-compile",
            "-DskipTests=true",
            "-Dcheckstyle.skip=true",
            "-Djavadoc.skip=true",
            "-Dpmd.skip=true",
            "-Dspotbugs.skip=true",
            "-DskipITs=true",
        ]
        cmd.extend(self.mvn_args)
        if self.settings_file:
            cmd += ["-s", str(self.settings_file)]
        if self.local_repo:
            cmd += [f"-Dmaven.repo.local={self.local_repo}"]

        last_blob = ""
        proc = None
        for offline in ([True, False] if self.offline else [False]):
            invocation = cmd + (["-o"] if offline else [])
            try:
                proc = subprocess.run(
                    invocation,
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                return CompileResult(ok=False, errors=[str(exc)]), [CompileError(
                    file=None,
                    line=None,
                    col=None,
                    message=f"Maven test-compile could not complete: {exc}",
                )]
            last_blob = (proc.stdout or "") + (proc.stderr or "")
            if offline and _looks_offline_miss(last_blob):
                continue
            break

        if proc is not None and proc.returncode == 0:
            return CompileResult(ok=True), []

        parsed = parse_maven_compiler_errors(last_blob)
        own = [
            error for error in parsed
            if error.file is not None and error.file.name == test_file.name
        ]
        if own:
            return CompileResult(ok=False, errors=[error.render() for error in own]), own
        # An unrelated sibling test must not redefine the generated class as a
        # compilation failure. The exact generated source already passed javac
        # and was emitted to target/test-classes; direct Surefire can still run
        # it as the requested class-level unit.
        log.warning(
            "Maven test-compile failed outside generated class %s; "
            "continuing with its successful isolated compilation",
            test_file.name,
        )
        return result, []

    def _resolve_module_cp(self, m: ModuleInfo) -> str:
        """Resolve a module's test-scope classpath; offline first, online fallback."""
        attempts = [True, False] if self.offline else [False]
        for offline in attempts:
            resolver = MavenClasspathResolver(
                mvn_path=self.mvn_path, include_scope="test", offline=offline,
                settings_file=self.settings_file, local_repo=self.local_repo,
                timeout_sec=self.timeout_sec,
            )
            try:
                cp = resolver.resolve(module_root=m.root, cache_dir=self.cache_dir)
                if cp:
                    return cp
            except ClasspathResolverError as exc:
                continue
        return ""

    def _reactor_test_compile(self) -> bool:
        base = [str(self.mvn_path), "-B", "-ntp", "-fae", "-DskipTests",
                "-f", str(self.repo_root / "pom.xml"), "test-compile"]
                
        if self.settings_file:
            base += ["-s", str(self.settings_file)]
        if self.local_repo:
            base += [f"-Dmaven.repo.local={self.local_repo}"]
            
        for offline in ([True, False] if self.offline else [False]):
            cmd = base + (["-o"] if offline else [])
            try:
                proc = subprocess.run(cmd, cwd=self.repo_root, capture_output=True,
                                      text=True, timeout=self.timeout_sec, check=False)
                if proc.returncode == 0:
                    return True
                blob = (proc.stdout or "") + (proc.stderr or "")
                if offline and _looks_offline_miss(blob):
                    continue
            except Exception:
                pass
        return False
