"""Run JaCoCo coverage without editing the target pom.

For speed, this runner can be disabled from loop.py using cfg.run_coverage=False.
When enabled, it runs Maven + JaCoCo and returns the jacoco.xml path.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from junitforge.compile_gate import parse_maven_compiler_errors
from junitforge.models import (
    ClassExecutionResult,
    ModuleInfo,
    TestMethodResult,
    TestMethodStatus,
)
from junitforge.vendor.logging_utils import get

log = get(__name__)

JACOCO_VERSION = "0.8.13"
SUREFIRE_VERSION = "3.5.9"


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

    def execute_class(
        self,
        module: ModuleInfo,
        *,
        test_class_name: str,
        test_file: Path,
        cut_class_name: str,
        expected_test_methods: tuple[str, ...] | list[str],
    ) -> ClassExecutionResult:
        """Compile through Maven and execute every method in one Surefire class run."""
        if not self.available():
            return ClassExecutionResult(
                compiled=False,
                executed=False,
                ok=False,
                note="mvn command not found",
            )

        report_dirs = {
            module.root / "target" / "surefire-reports",
            self.repo_root / "target" / "surefire-reports",
        }
        for report_dir in report_dirs:
            if not report_dir.exists():
                continue
            for report in report_dir.glob("*.xml"):
                if test_class_name in report.name:
                    try:
                        report.unlink()
                    except OSError:
                        pass

        cwd = self.repo_root if self.run_from_root else module.root
        pom = self.repo_root / "pom.xml" if self.run_from_root else module.pom
        surefire_goal = (
            f"org.apache.maven.plugins:maven-surefire-plugin:"
            f"{self.surefire_version}:test"
        )
        cmd = [
            str(self.mvn_path),
            "-B",
            "-ntp",
            "-f",
            str(pom),
            surefire_goal,
            f"-Dtest={test_class_name}",
            "-DfailIfNoTests=false",
            "-Dsurefire.failIfNoSpecifiedTests=false",
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
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return ClassExecutionResult(
                    compiled=True,
                    executed=False,
                    ok=False,
                    note=f"Maven complete-class execution timed out after {self.timeout_sec}s",
                )
            except FileNotFoundError as exc:
                return ClassExecutionResult(
                    compiled=False,
                    executed=False,
                    ok=False,
                    note=f"mvn execution failed: {exc}",
                )
            last_blob = (proc.stdout or "") + (proc.stderr or "")
            if offline and _looks_offline_miss(last_blob):
                continue
            break

        if proc is None:
            return ClassExecutionResult(False, False, False, note="Maven did not execute")

        compiler_errors = parse_maven_compiler_errors(last_blob)
        if compiler_errors:
            return ClassExecutionResult(
                compiled=False,
                executed=False,
                ok=False,
                compiler_errors=compiler_errors,
                note="Maven test compilation failed before Surefire execution",
                raw_log=_tail(last_blob, 40000),
            )

        result = parse_surefire_reports(
            report_dirs=report_dirs,
            test_class_name=test_class_name,
            test_file=test_file,
            cut_class_name=cut_class_name,
            expected_test_methods=tuple(expected_test_methods),
            raw_log=last_blob,
        )
        if proc.returncode != 0 and result.ok:
            result.ok = False
            result.note = "Surefire tests passed but Maven returned a non-zero build result"
        return result

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


_MOCKITO_MARKERS = (
    "org.mockito.",
    "UnnecessaryStubbingException",
    "PotentialStubbingProblem",
    "WantedButNotInvoked",
    "ArgumentsAreDifferent",
    "TooManyActualInvocations",
    "NeverWantedButInvoked",
    "InvalidUseOfMatchersException",
    "MisplacedArgumentMatcherException",
)


def _source_line(path: Path, line_number: int | None) -> str | None:
    if line_number is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip()
    return None


def _line_from_location(location: str | None, file_name: str) -> int | None:
    if not location:
        return None
    match = re.search(rf"{re.escape(file_name)}:(\d+)", location)
    return int(match.group(1)) if match else None


def _split_invocation_arguments(invocation: str | None) -> list[str]:
    if not invocation or "(" not in invocation or ")" not in invocation:
        return []
    body = invocation[invocation.find("(") + 1:invocation.rfind(")")]
    args: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(body):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{<":
            depth += 1
        elif char in ")]}>" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            args.append(body[start:index].strip())
            start = index + 1
    tail = body[start:].strip()
    if tail:
        args.append(tail)
    return args


def _extract_expected_actual(message: str) -> tuple[str | None, str | None]:
    if re.search(r"expected:\s*not\s*<null>", message, flags=re.IGNORECASE):
        return "non-null", "null"
    patterns = (
        r"expected:\s*<(?P<expected>[\s\S]*?)>\s*but was:\s*<(?P<actual>[\s\S]*?)>",
        r"expected:\s*(?P<expected>[^\n]+?)\s*but was:\s*(?P<actual>[^\n]+)",
        r"Expected\s*:\s*(?P<expected>[^\n]+).*?Actual\s*:\s*(?P<actual>[^\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return match.group("expected").strip(), match.group("actual").strip()
    return None, None


def _extract_null_path(message: str) -> str | None:
    patterns = (
        r'because\s+"(?P<path>[^"]+)"\s+is null',
        r'because the return value of\s+"(?P<path>[^"]+)"\s+is null',
        r"Cannot read field \"[^\"]+\" because \"(?P<path>[^\"]+)\" is null",
    )
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            return match.group("path")
    return None


def _extract_mockito_evidence(
    diagnostic: str,
    *,
    test_file: Path,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "mockito_subtype": None,
        "stub_declaration": None,
        "stub_source_location": None,
        "actual_invocation": None,
        "actual_invocation_source_location": None,
        "expected_arguments": [],
        "actual_arguments": [],
        "unused_stub_locations": [],
    }
    subtype = next((marker for marker in _MOCKITO_MARKERS[1:] if marker in diagnostic), None)
    evidence["mockito_subtype"] = subtype or "MockitoFailure"

    actual = re.search(
        r"this invocation of ['`]?[\w$]+['`]? method:\s*\n\s*(?P<call>[^\n;]+;?)\s*\n\s*->\s*(?P<loc>at\s+[^\n]+)",
        diagnostic,
        flags=re.IGNORECASE,
    )
    if actual:
        invocation = actual.group("call").strip().rstrip(";")
        evidence["actual_invocation"] = invocation
        evidence["actual_invocation_source_location"] = actual.group("loc").strip()
        evidence["actual_arguments"] = _split_invocation_arguments(invocation)

    stubbing_section = re.search(
        r"stubbing\(s\).*?:\s*(?P<body>[\s\S]*?)(?:\n\s*(?:Typically|For more information|$))",
        diagnostic,
        flags=re.IGNORECASE,
    )
    if stubbing_section:
        call_match = re.search(r"(?:^|\n)\s*\d+\.\s*(?P<call>[^\n;]+;?)", stubbing_section.group("body"))
        location_match = re.search(r"->\s*(?P<loc>at\s+[^\n]+)", stubbing_section.group("body"))
        if call_match:
            stub_call = call_match.group("call").strip().rstrip(";")
            evidence["expected_arguments"] = _split_invocation_arguments(stub_call)
        if location_match:
            evidence["stub_source_location"] = location_match.group("loc").strip()

    locations = re.findall(
        rf"->\s*(at\s+[^\n]*{re.escape(test_file.name)}:\d+\)?)",
        diagnostic,
    )
    if "UnnecessaryStubbingException" in diagnostic:
        evidence["unused_stub_locations"] = [location.strip() for location in locations]
    if evidence["stub_source_location"] is None and locations:
        evidence["stub_source_location"] = locations[0].strip()
    # Prefer the exact generated source declaration over Mockito's abbreviated
    # invocation rendering. Keep the diagnostic's argument list because it is
    # often more explicit than matcher-heavy source.
    line = _line_from_location(str(evidence["stub_source_location"] or ""), test_file.name)
    source_declaration = _source_line(test_file, line)
    if source_declaration:
        evidence["stub_declaration"] = source_declaration
        if not evidence["expected_arguments"]:
            evidence["expected_arguments"] = _split_invocation_arguments(source_declaration)
    return evidence


def _test_result_from_xml(
    testcase: ET.Element,
    *,
    test_file: Path,
    test_class_name: str,
    cut_class_name: str,
) -> TestMethodResult:
    name = testcase.attrib.get("name", "<unknown>")
    class_name = testcase.attrib.get("classname", "")
    try:
        duration = float(testcase.attrib.get("time", "0") or 0)
    except ValueError:
        duration = None
    skipped = testcase.find("skipped")
    failure = testcase.find("failure")
    error = testcase.find("error")
    if skipped is not None:
        return TestMethodResult(
            method_name=name,
            class_name=class_name,
            duration_seconds=duration,
            status=TestMethodStatus.SKIPPED,
            message=skipped.attrib.get("message") or (skipped.text or "").strip() or "test skipped",
            raw_diagnostic=(skipped.text or "").strip(),
        )
    node = failure if failure is not None else error
    if node is None:
        return TestMethodResult(
            method_name=name,
            class_name=class_name,
            duration_seconds=duration,
            status=TestMethodStatus.PASSED,
        )

    exception_type = node.attrib.get("type") or None
    attribute_message = node.attrib.get("message") or ""
    body = (node.text or "").strip()
    diagnostic = "\n".join(part for part in (attribute_message.strip(), body) if part).strip()
    is_mockito = (
        (exception_type or "").startswith("org.mockito.")
        or "org.mockito.exceptions." in diagnostic
        or any(marker in diagnostic for marker in _MOCKITO_MARKERS[1:])
    )
    if is_mockito:
        status = TestMethodStatus.MOCKITO_FAILURE
    elif (
        "Assertion" in (exception_type or "")
        or "opentest4j" in (exception_type or "").lower()
        or re.search(r"expected:|but was:|expected non-null", diagnostic, flags=re.IGNORECASE)
    ):
        status = TestMethodStatus.ASSERTION_FAILURE
    else:
        status = TestMethodStatus.RUNTIME_ERROR

    causes = [
        " ".join(match).strip()
        for match in re.findall(r"Caused by:\s*([\w.$]+)(?::\s*([^\n]+))?", diagnostic)
    ]
    root_cause = causes[-1] if causes else (
        f"{exception_type}: {attribute_message}".strip(": ") if exception_type else attribute_message or None
    )
    stack_frames = [
        line.strip()
        for line in diagnostic.splitlines()
        if line.strip().startswith("at ") or line.strip().startswith("\tat ")
    ]
    filtered = [
        frame for frame in stack_frames
        if test_class_name in frame or cut_class_name in frame
    ]
    generated_frame = next((frame for frame in filtered if test_class_name in frame), None)
    cut_frame = next((frame for frame in filtered if cut_class_name in frame), None)
    source_line = _line_from_location(generated_frame, test_file.name)
    expected, actual = _extract_expected_actual(diagnostic)
    mockito = (
        _extract_mockito_evidence(
            "\n".join(part for part in (exception_type or "", diagnostic) if part),
            test_file=test_file,
        )
        if status is TestMethodStatus.MOCKITO_FAILURE else {}
    )
    if source_line is None and mockito.get("stub_source_location"):
        source_line = _line_from_location(str(mockito["stub_source_location"]), test_file.name)

    return TestMethodResult(
        method_name=name,
        class_name=class_name,
        duration_seconds=duration,
        status=status,
        exception_type=exception_type,
        message=attribute_message or (diagnostic.splitlines()[0] if diagnostic else None),
        expected=expected,
        actual=actual,
        source_line=source_line,
        root_cause=root_cause,
        cause_chain=causes,
        filtered_stack_trace=filtered,
        generated_test_frame=generated_frame,
        cut_frame=cut_frame,
        null_path=_extract_null_path(diagnostic),
        mockito_subtype=mockito.get("mockito_subtype"),
        stub_declaration=mockito.get("stub_declaration"),
        stub_source_location=mockito.get("stub_source_location"),
        actual_invocation=mockito.get("actual_invocation"),
        actual_invocation_source_location=mockito.get("actual_invocation_source_location"),
        expected_arguments=list(mockito.get("expected_arguments") or []),
        actual_arguments=list(mockito.get("actual_arguments") or []),
        unused_stub_locations=list(mockito.get("unused_stub_locations") or []),
        raw_diagnostic=diagnostic,
    )


def parse_surefire_reports(
    *,
    report_dirs: set[Path] | tuple[Path, ...] | list[Path],
    test_class_name: str,
    test_file: Path,
    cut_class_name: str,
    expected_test_methods: tuple[str, ...],
    raw_log: str = "",
) -> ClassExecutionResult:
    """Parse every method from the just-completed class-level Surefire run."""
    parsed: dict[str, TestMethodResult] = {}
    for report_dir in report_dirs:
        if not report_dir.exists():
            continue
        for report in sorted(report_dir.glob("*.xml")):
            try:
                root = ET.parse(report).getroot()
            except (ET.ParseError, OSError):
                continue
            suite_name = root.attrib.get("name", "")
            testcases = root.findall(".//testcase")
            if test_class_name not in suite_name and not any(
                test_class_name in testcase.attrib.get("classname", "")
                for testcase in testcases
            ):
                continue
            for testcase in testcases:
                if test_class_name not in testcase.attrib.get("classname", suite_name):
                    continue
                result = _test_result_from_xml(
                    testcase,
                    test_file=test_file,
                    test_class_name=test_class_name,
                    cut_class_name=cut_class_name,
                )
                # JUnit ConsoleLauncher writes parameterless method names as
                # ``method()``; Surefire normally writes ``method``. Canonicalize
                # only this exact form so both complete-class backends produce
                # the same per-method result without guessing display names.
                if (
                    result.method_name.endswith("()")
                    and result.method_name[:-2] in expected_test_methods
                ):
                    result.method_name = result.method_name[:-2]
                parsed[result.method_name] = result

    ordered: list[TestMethodResult] = []
    for expected in expected_test_methods:
        result = parsed.pop(expected, None)
        if result is None:
            result = TestMethodResult(
                method_name=expected,
                class_name=test_class_name,
                status=TestMethodStatus.MISSING,
                message="Surefire report did not contain this generated test method",
            )
        ordered.append(result)
    ordered.extend(parsed[name] for name in sorted(parsed))
    executed = bool(expected_test_methods) and any(
        result.status is not TestMethodStatus.MISSING for result in ordered
    )
    ok = (
        executed
        and len(ordered) == len(expected_test_methods)
        and all(result.status is TestMethodStatus.PASSED for result in ordered)
    )
    note = "all complete-class tests passed" if ok else (
        "Surefire did not produce a result for every generated test"
        if any(result.status is TestMethodStatus.MISSING for result in ordered)
        else "complete-class execution contains failures, errors, Mockito failures, or skipped tests"
    )
    return ClassExecutionResult(
        compiled=True,
        executed=executed,
        ok=ok,
        test_methods=ordered,
        note=note,
        raw_log=_tail(raw_log, 40000),
    )
