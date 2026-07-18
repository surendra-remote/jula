"""Toolchain jars for the non-Maven (Ant/javac) backend.

For Ant/Eclipse repos there is no Maven to run tests + coverage, so we drive
JUnit 5 via the JUnit Platform Console Launcher and JaCoCo via its java agent +
CLI. The needed jars are located in the local Maven cache when present, else
downloaded once into ~/.junitforge/toolchain.
"""

from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from junitforge.vendor.logging_utils import get

log = get(__name__)

CENTRAL = "https://repo1.maven.org/maven2"
JUNIT_CONSOLE_VER = "1.11.4"
JACOCO_VER = "0.8.13"

# key -> (maven path, filename)
_ARTIFACTS = {
    "junit_console": (f"org/junit/platform/junit-platform-console-standalone/{JUNIT_CONSOLE_VER}",
                      f"junit-platform-console-standalone-{JUNIT_CONSOLE_VER}.jar"),
    "jacoco_agent": (f"org/jacoco/org.jacoco.agent/{JACOCO_VER}",
                     f"org.jacoco.agent-{JACOCO_VER}-runtime.jar"),
    "jacoco_cli": (f"org/jacoco/org.jacoco.cli/{JACOCO_VER}",
                   f"org.jacoco.cli-{JACOCO_VER}-nodeps.jar"),
}


@dataclass
class Toolchain:
    cache_dir: Path
    junit_console: Path | None = None
    jacoco_agent: Path | None = None
    jacoco_cli: Path | None = None
    mockito_jars: list[Path] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.junit_console and self.jacoco_agent and self.jacoco_cli)

    def test_classpath(self) -> list[str]:
        cp = [str(self.junit_console)] if self.junit_console else []
        cp += [str(j) for j in self.mockito_jars]
        return cp


def ensure_toolchain(cache_dir: Path | None = None, m2: Path | None = None,
                     allow_download: bool = True) -> Toolchain:
    cache_dir = cache_dir or (Path.home() / ".junitforge" / "toolchain")
    cache_dir.mkdir(parents=True, exist_ok=True)
    m2 = m2 or (Path.home() / ".m2" / "repository")
    tc = Toolchain(cache_dir=cache_dir)

    for key, (mvn_path, fname) in _ARTIFACTS.items():
        local = _locate(key, fname, mvn_path, cache_dir, m2, allow_download)
        setattr(tc, key, local)

    tc.mockito_jars = _find_mockito(m2)
    if not tc.ok:
        tc.note = "missing toolchain jars (no network / not in ~/.m2)"
    return tc


def _locate(key: str, fname: str, mvn_path: str, cache_dir: Path, m2: Path,
            allow_download: bool) -> Path | None:
    cached = cache_dir / fname
    if cached.exists() and cached.stat().st_size > 0:
        return cached
    in_m2 = m2 / mvn_path / fname
    if in_m2.exists() and in_m2.stat().st_size > 0:
        return in_m2
    if not allow_download:
        return None
    url = f"{CENTRAL}/{mvn_path}/{fname}"
    try:
        log.info("downloading toolchain jar: %s", fname)
        with urllib.request.urlopen(url, timeout=120) as r, cached.open("wb") as out:
            out.write(r.read())
        return cached if cached.stat().st_size > 0 else None
    except Exception as exc:  # noqa: BLE001
        log.warning("toolchain download failed for %s: %s", fname, exc)
        return None


def _find_mockito(m2: Path) -> list[Path]:
    """Best-effort mockito + deps from the local Maven cache (for tests with mocks)."""
    out: list[Path] = []
    patterns = ["org/mockito/mockito-core", "org/mockito/mockito-junit-jupiter",
                "net/bytebuddy/byte-buddy", "net/bytebuddy/byte-buddy-agent",
                "org/objenesis/objenesis"]
    for pat in patterns:
        base = m2 / pat
        if not base.exists():
            continue
        jars = sorted(base.rglob("*.jar"))
        jars = [j for j in jars if "sources" not in j.name and "javadoc" not in j.name]
        if jars:
            out.append(jars[-1])  # latest version
    return out


def _sep() -> str:
    return os.pathsep
