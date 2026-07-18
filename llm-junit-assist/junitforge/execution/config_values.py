"""Read application YAML/properties for deterministic @Value test injection."""
from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any

from junitforge.vendor.logging_utils import get

log = get(__name__)


def _flatten(value: Any, prefix: str = "", out: dict[str, str] | None = None) -> dict[str, str]:
    out = out or {}
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            _flatten(child, name, out)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _flatten(child, f"{prefix}[{index}]", out)
    elif value is not None and prefix:
        if isinstance(value, bool):
            out[prefix] = "true" if value else "false"
        else:
            out[prefix] = str(value)
    return out


def load_application_values(repo_root: Path) -> dict[str, str]:
    started = perf_counter()
    candidates = sorted({
        *repo_root.glob("**/src/main/resources/application.yml"),
        *repo_root.glob("**/src/main/resources/application.yaml"),
        *repo_root.glob("**/src/test/resources/application.yml"),
        *repo_root.glob("**/src/test/resources/application.yaml"),
    })
    values: dict[str, str] = {}
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if yaml is not None:
                for document in yaml.safe_load_all(text):
                    if isinstance(document, dict):
                        # Later files override earlier files, matching test-resource precedence.
                        values.update(_flatten(document))
            else:
                # Conservative indentation-based fallback for scalar mappings.
                stack: list[tuple[int, str]] = []
                for raw in text.splitlines():
                    if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
                        continue
                    indent = len(raw) - len(raw.lstrip(" "))
                    key, val = raw.strip().split(":", 1)
                    while stack and stack[-1][0] >= indent:
                        stack.pop()
                    full = ".".join([part for _, part in stack] + [key.strip()])
                    if val.strip():
                        values[full] = val.strip().strip('"\'')
                    else:
                        stack.append((indent, key.strip()))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read configuration file %s: %s", path, exc)
    log.info(
        "[TIMING] application configuration scan: %.3fs | files=%d | properties=%d",
        perf_counter() - started, len(candidates), len(values),
    )
    return values
