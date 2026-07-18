"""Always-visible timing and heartbeat diagnostics for JUnitForge.

Every event is printed immediately to stderr and, when configured, appended as
JSONL under ``<repo>/.junitforge/timing-events.jsonl``.  The heartbeat context
manager keeps printing while a long blocking phase is active, so a run never
becomes silent without identifying the current phase.
"""
from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator

_LOCK = threading.Lock()
_OUTPUT: Path | None = None
_VERSION = "V15.3-BOUNDED-EXECUTION-CONTEXT"


def configure_timing(repo_root: Path | None = None) -> None:
    global _OUTPUT
    if repo_root is not None:
        _OUTPUT = Path(repo_root) / ".junitforge" / "timing-events.jsonl"
        _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        _OUTPUT.touch(exist_ok=True)
    event("version", version=_VERSION, timing_file=str(_OUTPUT) if _OUTPUT else "disabled")


def now() -> float:
    return perf_counter()


def event(step: str, *, elapsed: float | None = None, **fields: Any) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    parts = [f"[{timestamp}] [V15.3-TIMING]", step]
    if elapsed is not None:
        parts.append(f"elapsed={elapsed:.3f}s")
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    line = " | ".join(parts)

    with _LOCK:
        print(line, file=sys.stderr, flush=True)
        if _OUTPUT is not None:
            payload = {
                "timestamp": timestamp,
                "version": _VERSION,
                "step": step,
                "elapsed_seconds": elapsed,
                **fields,
            }
            try:
                with _OUTPUT.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
            except OSError as exc:
                print(
                    f"[{timestamp}] [V15.3-TIMING] timing-file-write-error | error={exc}",
                    file=sys.stderr,
                    flush=True,
                )


@contextmanager
def heartbeat(
    step: str,
    *,
    interval_seconds: float = 15.0,
    **fields: Any,
) -> Iterator[None]:
    """Emit periodic progress while the enclosed operation is still active."""
    started = perf_counter()
    stop = threading.Event()

    def run() -> None:
        count = 0
        while not stop.wait(max(1.0, interval_seconds)):
            count += 1
            event(
                f"{step}.heartbeat",
                elapsed=perf_counter() - started,
                heartbeat=count,
                **fields,
            )

    thread = threading.Thread(
        target=run,
        name=f"junitforge-heartbeat-{step}",
        daemon=True,
    )
    event(f"{step}.start", **fields)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
        event(f"{step}.end", elapsed=perf_counter() - started, **fields)
