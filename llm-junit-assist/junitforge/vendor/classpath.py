"""Classpath resolution via `mvn dependency:build-classpath`, cached to cp.txt.

Upgraded to default to 'test' scope to ensure JUnit 5 and Spring Boot 3.5 testing
starter trees are fully included across all compilation and validation gates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ClasspathResolverError(RuntimeError):
    pass


class ClasspathResolver(Protocol):
    name: str

    def resolve(self, *, module_root: Path, cache_dir: Path) -> str:
        ...


@dataclass
class MavenClasspathResolver:
    name: str = "maven"
    timeout_sec: int = 180
    mvn_path: str | None = None
    # CRITICAL FIX: Changed default scope from "compile" to "test"
    # This guarantees that JUnit 5, Mockito, and Spring Boot testing dependencies are always available
    include_scope: str = "test"   # "compile" | "test" | "runtime"
    offline: bool = False
    settings_file: Path | None = None
    local_repo: Path | None = None

    def __post_init__(self) -> None:
        if self.mvn_path is None:
            self.mvn_path = shutil.which("mvn") or shutil.which("mvn.cmd") or "mvn"

    def resolve(self, *, module_root: Path, cache_dir: Path) -> str:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cp_file = cache_dir / f"cp_{self.include_scope}_{module_root.name}.txt"
        pom = module_root / "pom.xml"
        if not pom.exists():
            raise ClasspathResolverError(f"no pom.xml in module root directory: {module_root}")

        # Invalidate the cache when the pom changed after the cp file was written.
        if cp_file.exists():
            try:
                if cp_file.stat().st_mtime >= pom.stat().st_mtime:
                    text = cp_file.read_text(encoding="utf-8").strip()
                    if text:
                        return text
            except OSError:
                pass

        # Build clean Maven pipeline command payload targeting resolved test scopes
        cmd = [str(self.mvn_path), "-q", "-B", "-ntp", "-f", str(pom),
               "dependency:build-classpath",
               f"-Dmdep.outputFile={cp_file}",
               f"-DincludeScope={self.include_scope}"]
               
        if self.offline:
            cmd.append("-o")
        if self.settings_file:
            cmd.extend(["-s", str(self.settings_file)])
        if self.local_repo:
            cmd.append(f"-Dmaven.repo.local={self.local_repo}")
            
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout_sec, check=False)
        except FileNotFoundError as exc:
            raise ClasspathResolverError(f"mvn executable command not found on system PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClasspathResolverError(
                f"mvn dependency:build-classpath timed out after execution ceiling of {exc.timeout}s") from exc

        if proc.returncode != 0 or not cp_file.exists():
            raise ClasspathResolverError(
                f"mvn dependency:build-classpath tracking failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '')[:500]}")
                
        return cp_file.read_text(encoding="utf-8").strip()


@dataclass
class FileClasspathResolver:
    name: str = "file"
    cp_file: Path | None = None

    def resolve(self, *, module_root: Path, cache_dir: Path) -> str:
        if self.cp_file is None or not Path(self.cp_file).exists():
            raise ClasspathResolverError(f"Pre-configured cp file path not found: {self.cp_file}")
        text = Path(self.cp_file).read_text(encoding="utf-8").strip()
        if "\n" in text:
            parts = [p.strip() for p in text.splitlines() if p.strip()]
            return os.pathsep.join(parts)
        return text
