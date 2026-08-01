"""Reliable JSON extraction from LLM output (fixes issue 1).

Strict-JSON instruction + a balanced-brace extractor (ported from v1's good
`_extract_first_json_object`) + lightweight schema validation + bounded re-ask
with the precise validation error. No pydantic dependency - keeps junitforge's
footprint small and fully standalone.
"""

from __future__ import annotations

import json
from typing import Callable

from junitforge.vendor.llm.base import LLMClient


class JsonValidationError(ValueError):
    pass


def extract_json_object(text: str) -> dict:
    cleaned = (text or "").strip()
    if not cleaned:
        raise JsonValidationError("empty response")
    # Strip a single fenced block if present.
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    candidate = _first_balanced_object(cleaned)
    if candidate is None:
        raise JsonValidationError("no JSON object found in response")
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise JsonValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise JsonValidationError("JSON payload is not an object")
    return obj


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def generate_json(
    llm: LLMClient,
    messages: list[dict],
    *,
    validate: Callable[[dict], dict] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1500,
    max_reasks: int = 1,
) -> dict:
    """Call chat, extract+validate a JSON object, re-ask once on failure."""
    convo = list(messages)
    last_err = ""
    for attempt in range(max_reasks + 1):
        resp = llm.chat(convo, temperature=temperature, max_tokens=max_tokens)
        text = resp.message or ""
        try:
            obj = extract_json_object(text)
            return validate(obj) if validate else obj
        except (JsonValidationError, ValueError) as exc:
            last_err = str(exc)
            if attempt >= max_reasks:
                break
            convo = list(messages) + [{
                "role": "user",
                "content": (
                    "Your previous reply could not be parsed: "
                    f"{last_err}. Return EXACTLY one valid JSON object and nothing else - "
                    "no markdown fences, no prose."
                ),
            }]
    raise JsonValidationError(f"unable to obtain valid JSON after re-ask: {last_err}")
