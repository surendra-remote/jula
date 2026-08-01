"""Maven + Spring Boot 3 Build Backend.

Drives compilation and isolated test-execution using standard Maven lifecycle 
commands (mvn test). Automatically resolves complex Spring Boot starter trees 
and relies natively on the project's pom.xml context.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from junitforge.coverage_run import CoverageRunResult
from junitforge.models import CompileError, ModuleInfo
from junitforge.vendor.javac import CompileResult
from junitforge.vendor.logging_utils import get

log = get(__name__)

def is_maven_repo(repo_root: Path) -> bool:
    """Detects if this is a standard Maven project."""
    return (repo_root / "pom.xml").exists()

def discover_maven_modules(repo_root: Path) -> list[ModuleInfo]:
    """Identifies module information using root pom.xml."""
    return [ModuleInfo(name=repo_root.name, root=repo_root, pom=repo_root / "pom.xml")]

def maven_source_files(repo_root: Path) -> list[Path]:
    """Finds target production Java classes under src/main/java."""
    src_dir = repo_root / "src" / "main" / "java"
    if not src_dir.exists():
        return []
    
    out: list[Path] = []
    for p in sorted(src_dir.rglob("*.java")):
        if p.name in ("package-info.java", "module-info.java"):
            continue
        out.append(p)
    return out

def maven_test_path(source_path: Path, module: ModuleInfo, symbol) -> Path:
    """Places generated tests safely into target test directories matching Maven convention."""
    pkg_parts = symbol.fqcn.rsplit(".", 1)[0].split(".") if "." in symbol.fqcn else []
    return module.root.joinpath("src", "test", "java", *pkg_parts) / f"{symbol.name}Test.java"


# ---------------------------------------------------------------------------
# Maven Compiler Gate
# ---------------------------------------------------------------------------

@dataclass
class MavenCompileGate:
    repo_root: Path
    cache_dir: Path
    timeout_sec: int = 120
    _ready: bool = False
    _setup_note: str = ""

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def setup_note(self) -> str:
        return self._setup_note

    def setup(self, modules: list[ModuleInfo]) -> bool:
        """Verifies Maven installation and runs test-compile to ensure layout stability."""
        mvn_exec = shutil.which("mvn") or shutil.which("mvn.cmd")
        if mvn_exec is None:
            self._setup_note = "Maven CLI ('mvn') not found on PATH. Ensure Maven is configured."
            return False
        
        # Verify the production build compiles cleanly
        print("[*] Executing upfront Maven validation compile step...")
        cmd = [mvn_exec, "test-compile", "-DskipTests=true"]
        res = subprocess.run(cmd, cwd=str(self.repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if res.returncode == 0:
            self._ready = True
        else:
            self._setup_note = f"Maven compilation failed upfront: {res.stderr[:200]}"
            self._ready = False
        return self._ready

    def check(self, test_file: Path, module: ModuleInfo) -> tuple[CompileResult, list[CompileError]]:
        """Validates if the AI's generated code compiles successfully within the Spring Boot framework."""
        mvn_exec = shutil.which("mvn") or shutil.which("mvn.cmd")
        if not mvn_exec:
            return CompileResult(ok=False), [CompileError(None, None, None, "Maven command lost")]

        cmd = [mvn_exec, "test-compile", "-DskipTests=true"]
        res = subprocess.run(cmd, cwd=str(self.repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if res.returncode == 0:
            return CompileResult(ok=True), []
        
        # Return generic error wrapper indicating the AI code broke compilation bounds
        return CompileResult(ok=False, errors=[res.stderr]), [CompileError(file=test_file, line=None, col=None, message="AI Generated code failed Maven test-compile step")]


# ---------------------------------------------------------------------------
# Maven Coverage Runner (JaCoCo Core Integration)
# ---------------------------------------------------------------------------

@dataclass
class MavenCoverageRunner:
    repo_root: Path
    cache_dir: Path
    timeout_sec: int = 180

    def xml_path(self, module: ModuleInfo) -> Path:
        """Standard location of JaCoCo XML report inside target/ folder."""
        return module.root / "target" / "site" / "jacoco" / "jacoco.xml"

    def measure(self, module: ModuleInfo, test_class_name: str) -> CoverageRunResult:
        """Executes the specific target test suite via surefire and pulls structural branch data."""
        mvn_exec = shutil.which("mvn") or shutil.which("mvn.cmd")
        if not mvn_exec:
            return CoverageRunResult(False, False, "Maven missing on runtime path")

        # Force execution of ONLY this specific generated test suite using the -Dtest flag
        # This keeps loop iterations fast and prevents running your entire project suite over and over
        cmd = [mvn_exec, "clean", "test", f"-Dtest={test_class_name}"]
        
        try:
            res = subprocess.run(cmd, cwd=str(self.repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=self.timeout_sec)
        except subprocess.TimeoutExpired:
            return CoverageRunResult(False, True, f"Maven test execution timed out after {self.timeout_sec} seconds.")

        xml_p = self.xml_path(module)
        if not xml_p.exists():
            return CoverageRunResult(False, False, f"Tests executed but target JaCoCo XML report was missing at: {xml_p}. Make sure jacoco-maven-plugin is active in your pom.xml.")

        return CoverageRunResult(success=True, timeout=False, note=f"Successfully analyzed coverage metrics.")