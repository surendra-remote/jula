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

_MVN_ERR = re.compile(r"^\[ERROR\]\s+(?P<path>[^\s\[][^\[\n]*\.java):\[(?P<line>\d+),(?P<col>\d+)\]\s+(?P<msg>.*)$")
_MVN_CONT = re.compile(r"^\[ERROR\]\s+(?:\s+)?(symbol|location|required|found|reason):\s+(.*)$")
_JAVAC_ERR = re.compile(r"^(?P<path>[^\n:]+\.java):(?P<line>\d+):\s+error:\s+(?P<msg>.*)$")
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
    return _parse(stderr, _JAVAC_ERR, _JAVAC_CONT)


def parse_maven_compiler_errors(log_text: str) -> list[CompileError]:
    return _parse(log_text, _MVN_ERR, _MVN_CONT)


def _parse(text: str, err_re: re.Pattern, cont_re: re.Pattern) -> list[CompileError]:
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
        return res, errors

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
