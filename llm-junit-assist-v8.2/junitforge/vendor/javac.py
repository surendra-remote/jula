"""javac runner + error parser. Single-file compile with cached classpath.

Upgraded with mandatory '-parameters' flags to fully support Spring Boot 3.5+
parameter reflection mappings during micro-compilation steps.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


ProcessRunner = Callable[[list[str], int], subprocess.CompletedProcess]


def _default_runner(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


@dataclass
class CompileResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    errors: list[str] = field(default_factory=list)   # parsed error message lines
    cmd: list[str] = field(default_factory=list)


_ERROR_LINE_RE = re.compile(r"^(?P<path>.*\.java):(?P<line>\d+):\s+error:\s+(?P<message>.*)$")
_CONTINUATION_RE = re.compile(r"^\s+(symbol|location|required|found|reason):\s+(.*)$")


def parse_errors(stderr: str) -> list[str]:
    """Return distinct error messages from a javac stderr blob."""
    out: list[str] = []
    current: list[str] | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            out.append(" ".join(current))
            current = None

    for raw in stderr.splitlines():
        m = _ERROR_LINE_RE.match(raw)
        if m:
            _flush()
            current = [m.group("message").strip()]
            continue
        c = _CONTINUATION_RE.match(raw)
        if c and current is not None:
            current.append(f"{c.group(1)}: {c.group(2).strip()}")
            continue
        _flush()
    _flush()

    seen: set[str] = set()
    deduped: list[str] = []
    for msg in out:
        if msg not in seen:
            seen.add(msg)
            deduped.append(msg)
    return deduped


@dataclass
class JavacRunner:
    """Compile one or more .java files with the given classpath.

    `release` pins the bytecode/source level via javac --release N.
    """
    classpath: str
    output_dir: Path
    runner: ProcessRunner = _default_runner
    javac_path: str | None = None
    extra_flags: tuple[str, ...] = ()
    timeout_sec: int = 60
    release: str | None = None

    def __post_init__(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.javac_path is None:
            self.javac_path = shutil.which("javac") or "javac"

    def compile_one(self, source_file: Path) -> CompileResult:
        return self.compile_many([source_file])

    def compile_many(self, source_files: list[Path]) -> CompileResult:
        # CRITICAL FIX: Injected "-parameters" and "-encoding UTF-8" flags by default
        # This preserves Java 17 parameter reflection metadata required by Spring Boot 3.5 components
        cmd = [
            self.javac_path, 
            "-cp", self.classpath, 
            "-d", str(self.output_dir),
            "-parameters",
            "-encoding", "UTF-8"
        ]
        
        if self.release:
            cmd.extend(["--release", str(self.release)])
            
        cmd.extend(self.extra_flags)
        cmd.extend(str(s) for s in source_files)
        
        try:
            proc = self.runner(cmd, self.timeout_sec)
        except FileNotFoundError as exc:
            return CompileResult(
                ok=False, stderr=f"javac executable command not found: {exc}",
                return_code=-1, cmd=cmd, errors=["javac executable not on PATH"],
            )
        except subprocess.TimeoutExpired as exc:
            return CompileResult(
                ok=False, stderr=f"Compilation timed out after {self.timeout_sec}s",
                return_code=-2, cmd=cmd, errors=[f"javac timed out after {exc.timeout}s"],
            )

        ok = proc.returncode == 0
        errors = parse_errors(proc.stderr or "") if not ok else []
        return CompileResult(
            ok=ok, stdout=proc.stdout or "", stderr=proc.stderr or "",
            return_code=proc.returncode, errors=errors, cmd=cmd,
        )


def join_classpath(paths: list[Path | str]) -> str:
    import os
    return os.pathsep.join(str(p) for p in paths if p)


def shell_quote(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)
