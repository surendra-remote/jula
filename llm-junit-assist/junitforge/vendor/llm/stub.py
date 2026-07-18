"""Scripted stub LLM client for tests.

Upgraded to align with the production single-turn token limits of the 
integrated enterprise generation engines.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable

from junitforge.vendor.llm.base import LLMResponse


class StubLLMClient:
    """A high-fidelity Mock client simulating REST exchanges for isolated pipeline testing."""
    
    def __init__(self, responses: Iterable[LLMResponse]):
        self._q: deque[LLMResponse] = deque(responses)
        self.calls: list[dict] = []
        self.model_id = "stub-mock-engine"

    def chat(self, messages, tools=None, temperature=0.0, max_tokens=4096) -> LLMResponse:
        """Captures local transaction context parameters and pops the pre-loaded stub response."""
        self.calls.append({"messages": messages, "tools": tools})
        if not self._q:
            raise RuntimeError("StubLLMClient execution queue has been completely exhausted.")
        return self._q.popleft()
