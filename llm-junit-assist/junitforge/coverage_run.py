"""Run JaCoCo coverage without editing the target pom.

For speed, this runner can be disabled from loop.py using cfg.run_coverage=False.
When enabled, it runs Maven + JaCoCo and returns the jacoco.xml path.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from junitforge.models import ModuleInfo
from junitforge.vendor.logging_utils import get

log = get(__name__)

JACOCO_VERSION = "0.8.12"
SUREFIRE_VERSION = "3.5.2"


@dataclass
class CoverageRunResult:
    ok: bool
    measured: bool
    note: str
    xml_path: Path | None = None
    log_tail: str = ""


@dataclass
class CoverageRunner:
    repo_root: Path
    jacoco_version: str = JACOCO_VERSION
    mvn_path: str | None = None
    settings_file: Path | None = None
    local_repo: Path | None = None
    offline: bool = False
    timeout_sec: int = 240
    surefire_version: str = SUREFIRE_VERSION
    mvn_args: list[str] = field(default_factory=list)
    run_from_root: bool = True

    def __post_init__(self) -> None:
        if self.mvn_path is None:
            self.mvn_path = shutil.which("mvn") or shutil.which("mvn.cmd") or "mvn"

    def xml_path(self, module: ModuleInfo) -> Path:
        return module.root / "target" / "site" / "jacoco" / "jacoco.xml"

    def exec_path(self, module: ModuleInfo) -> Path:
        return module.root / "target" / "jacoco.exec"

    def available(self) -> bool:
        return shutil.which(self.mvn_path or "mvn") is not None

    def warm_cache(self) -> None:
        cmd = [
            str(self.mvn_path),
            "-B",
            "-ntp",
            f"org.jacoco:jacoco-maven-plugin:{self.jacoco_version}:help",
        ]
        if self.settings_file:
            cmd += ["-s", str(self.settings_file)]
        if self.local_repo:
            cmd += [f"-Dmaven.repo.local={self.local_repo}"]

        try:
            subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except Exception:
            pass

    def measure(self, module: ModuleInfo, test_classes: list[str] | None = None) -> CoverageRunResult:
        if not self.available():
            return CoverageRunResult(False, False, "mvn command not found")

        gid = f"org.jacoco:jacoco-maven-plugin:{self.jacoco_version}"
        sf = f"org.apache.maven.plugins:maven-surefire-plugin:{self.surefire_version}:test"

        cwd = self.repo_root if self.run_from_root else module.root
        pom = self.repo_root / "pom.xml" if self.run_from_root else module.pom

        base = [
            str(self.mvn_path),
            "-B",
            "-ntp",
            "-f",
            str(pom),
            f"{gid}:prepare-agent",
            "test-compile",
            sf,
            f"{gid}:report",
            "-DfailIfNoTests=false",
            "-Dsurefire.failIfNoSpecifiedTests=false",
            "-Dmaven.test.failure.ignore=true",
            "-Dcheckstyle.skip=true",
            "-Djavadoc.skip=true",
            "-Dpmd.skip=true",
            "-Dspotbugs.skip=true",
            "-DskipITs=true",
        ]

        base.extend(self.mvn_args)

        if test_classes:
            base.append("-Dtest=" + ",".join(test_classes))

        if self.settings_file:
            base += ["-s", str(self.settings_file)]

        if self.local_repo:
            base += [f"-Dmaven.repo.local={self.local_repo}"]

        for stale in (
            self.exec_path(module),
            self.xml_path(module),
            self.repo_root / "target" / "jacoco.exec",
            self.repo_root / "target" / "site" / "jacoco" / "jacoco.xml",
        ):
            try:
                stale.unlink()
            except OSError:
                pass

        last_blob = ""
        proc = None

        for offline in ([True, False] if self.offline else [False]):
            cmd = base + (["-o"] if offline else [])
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return CoverageRunResult(
                    False,
                    False,
                    f"Maven coverage timed out after {self.timeout_sec}s",
                )
            except FileNotFoundError as exc:
                return CoverageRunResult(False, False, f"mvn execution failed: {exc}")

            last_blob = (proc.stdout or "") + (proc.stderr or "")

            if offline and _looks_offline_miss(last_blob):
                continue
            break

        if proc is None:
            return CoverageRunResult(False, False, "Maven coverage did not execute")

        if "COMPILATION ERROR" in last_blob:
            _print_compiler_errors(last_blob)

        exec_p = self.exec_path(module)
        xml_p = self.xml_path(module)

        root_exec = self.repo_root / "target" / "jacoco.exec"
        root_xml = self.repo_root / "target" / "site" / "jacoco" / "jacoco.xml"

        if not exec_p.exists() or exec_p.stat().st_size == 0:
            if root_exec.exists() and root_exec.stat().st_size > 0:
                exec_p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(root_exec, exec_p)
            else:
                return CoverageRunResult(
                    False,
                    False,
                    "jacoco.exec missing or blank",
                    log_tail=_tail(last_blob),
                )

        if not xml_p.exists():
            if root_xml.exists():
                xml_p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(root_xml, xml_p)
            else:
                return CoverageRunResult(
                    False,
                    False,
                    "jacoco.xml not found",
                    log_tail=_tail(last_blob),
                )

        return CoverageRunResult(
            ok=True,
            measured=True,
            note="coverage measured",
            xml_path=xml_p,
            log_tail=_tail(last_blob),
        )


def _print_compiler_errors(blob: str) -> None:
    print("\n" + "!" * 80)
    print("MAVEN COMPILER ERROR")
    print("!" * 80)

    for line in blob.splitlines():
        if "[ERROR]" in line and any(
            x in line
            for x in (
                ".java:",
                "cannot find symbol",
                "symbol:",
                "constructor",
                "incompatible types",
                "package",
            )
        ):
            print(line)

    print("!" * 80 + "\n")


def _looks_offline_miss(blob: str) -> bool:
    needles = (
        "is missing in the local repository",
        "Cannot access central",
        "in offline mode",
        "Could not resolve",
        "Failure to find",
        "Unable to find",
    )
    return any(n in blob for n in needles)


def _tail(blob: str, n: int = 4000) -> str:
    return blob[-n:] if len(blob) > n else blob