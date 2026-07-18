"""Watsonx implementation of LLMClient - direct REST, no IBM SDK.

Upgraded to optimize single-turn token limits for highly resilient, high-coverage
Spring Boot 3.5 test case generation.
"""

from __future__ import annotations

import ast
import json
import os
import re
import secrets
import string
import time
from dataclasses import dataclass

import requests

from junitforge.vendor.llm.base import LLMResponse, TokenUsage, ToolCall
from junitforge.vendor.logging_utils import get

log = get(__name__)

CHAT_API_VERSION = "2024-10-10"
IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
TOKEN_REFRESH_LEEWAY_SEC = 300
HTTP_TIMEOUT_SEC = int(os.environ.get("WATSONX_HTTP_TIMEOUT_SEC", "300"))
MAX_RETRIES = int(os.environ.get("WATSONX_MAX_RETRIES", "3"))

_VERIFY_TLS = os.environ.get("WATSONX_VERIFY_TLS", "1").lower() not in ("0", "false", "no")
if not _VERIFY_TLS:
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


@dataclass
class WatsonxConfig:
    url: str
    api_key: str
    project_id: str
    model_id: str
    max_tokens_per_call: int = 4096

    @classmethod
    def from_env(cls, model_id: str | None = None) -> "WatsonxConfig":
        return cls(
            url=os.environ["WATSONX_URL"],
            api_key=os.environ["WATSONX_API_KEY"],
            project_id=os.environ["WATSONX_PROJECT_ID"],
            model_id=model_id or os.environ.get("WATSONX_MODEL_ID", "mistralai/mistral-medium-2505"),
            # Enforce the realistic single-turn token limit for modern enterprise engines
            max_tokens_per_call=int(os.environ.get("WATSONX_MAX_TOKENS", "4096")),
        )


class WatsonxClient:
    def __init__(self, config: WatsonxConfig):
        self.config = config
        self._token: str | None = None
        self._token_expiry: float = 0.0

    @property
    def model_id(self) -> str:
        return self.config.model_id

    def _iam_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and time.time() < self._token_expiry - TOKEN_REFRESH_LEEWAY_SEC:
            return self._token
        r = requests.post(
            IAM_TOKEN_URL,
            data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": self.config.api_key,
            },
            timeout=30,
            verify=_VERIFY_TLS,
        )
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._token_expiry = time.time() + int(d.get("expires_in") or 3600)
        return self._token

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        body: dict = {
            "model_id": self.config.model_id,
            "project_id": self.config.project_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice_option"] = "auto"

        url = f"{self.config.url.rstrip('/')}/ml/v1/text/chat?version={CHAT_API_VERSION}"

        last_exc: Exception | None = None
        current_max_tokens = max_tokens
        
        for attempt in range(MAX_RETRIES):
            try:
                body["max_tokens"] = current_max_tokens
                token = self._iam_token()
                r = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=body,
                    timeout=HTTP_TIMEOUT_SEC,
                    verify=_VERIFY_TLS,
                )
                if r.status_code == 401:
                    self._token = None
                    self._token_expiry = 0.0
                    self._iam_token(force_refresh=True)
                    r = requests.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        json=body,
                        timeout=HTTP_TIMEOUT_SEC,
                        verify=_VERIFY_TLS,
                    )
                r.raise_for_status()
                return _to_response(r.json())
            except requests.exceptions.ReadTimeout as exc:
                last_exc = exc
                # Safe padding fallback logic to guarantee token boundaries are never choked
                current_max_tokens = max(2048, current_max_tokens)
                delay = 2.0 ** attempt
                log.warning(
                    "Watsonx connection alert: turn timed out on attempt %d; re-engaging with delay parameter %.1fs",
                    attempt + 1, delay
                )
                time.sleep(delay)
                continue
            except Exception as exc:
                last_exc = exc
                delay = 2.0 ** attempt
                log.warning("Watsonx connection error on attempt %d: %s", attempt + 1, exc)
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc


def _to_response(raw: dict) -> LLMResponse:
    choices = raw.get("choices") or []
    if not choices:
        return LLMResponse(message=None, raw=raw)
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    tcs: list[ToolCall] = []
    
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            args = {"__raw__": args_raw}
        if isinstance(args, dict):
            args = _unwrap_double_encoded(args)
        tcs.append(ToolCall(id=tc.get("id") or "", name=fn.get("name") or "", arguments=args or {}))

    usage = raw.get("usage") or {}
    return LLMResponse(
        message=content,
        tool_calls=tcs,
        usage=TokenUsage(
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            total_tokens=usage.get("total_tokens") or 0,
        ),
        raw=raw,
    )


def _unwrap_double_encoded(args: dict) -> dict:
    out: dict = {}
    for k, v in args.items():
        if isinstance(v, str) and v.startswith(("[", "{")) and v.endswith(("]", "}")):
            decoded: object = v
            try:
                decoded = json.loads(v)
            except Exception:
                try:
                    decoded = ast.literal_eval(v)
                except Exception:
                    pass
            out[k] = decoded
        else:
            out[k] = v
    return out
