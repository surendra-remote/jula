"""LLM client protocol + shared types. Vendor-neutral shape.

Upgraded to define distinct operational parameters matching the single-turn 
output limits of modern enterprise generation engines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    """Represents an isolated tool execution call payload emitted by the model."""
    id: str
    name: str
    arguments: dict


@dataclass
class TokenUsage:
    """Telemetry POJO tracking input, output, and aggregate processing data."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """The master return payload wrapper encapsulating model responses and usage metrics."""
    message: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict | None = None  # vendor-specific raw payload map for system debugging


class LLMClient(Protocol):
    """Unified communications interface definition mapping REST requests to active clients."""
    
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
        # Operational boundaries adjusted to respect single-turn generation limits
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Executes a chat completion call against the integrated LLM provider backend."""
        ...
