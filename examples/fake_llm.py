"""An offline stand-in for a chat model: scripted decisions, realistic names, usage and TTFT.

`FakeLLM.complete` records a Traceloom generation the same way the provider wrappers do, so the
demo agent's trace looks like one made with a real model.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any

import traceloom_sdk

PLANNER_MODEL = "claude-haiku-4-5-20251001"
ANSWER_MODEL = "claude-sonnet-5-5"


@dataclass
class Decision:
    """What the model returned: a tool call or a final answer."""

    tool: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    text: str = ""


# (tool, input) for each planner turn, then the final answer is written from the observations.
SCRIPT = [
    ("search", {"query": "refund policy for damaged items"}),
    ("calculator", {"expression": "49.99 * 2"}),
    ("lookup_order", {"order_id": "A-1042"}),
]


def _tokens(value: Any) -> int:
    return max(1, len(str(value)) // 4)


class FakeLLM:
    def __init__(self, sleep: float = 0.005) -> None:
        self._sleep = sleep
        self._turn = 0

    def complete(self, question: str, history: list[dict[str, Any]]) -> Decision:
        final = self._turn >= len(SCRIPT)
        model = ANSWER_MODEL if final else PLANNER_MODEL
        params = {"temperature": 0.0, "max_tokens": 512}
        messages = [{"role": "user", "content": question}, *history]
        with traceloom_sdk.start_generation(
            "planner" if not final else "answer",
            model=model,
            input=messages,
            model_parameters=params,
        ) as gen:
            started = dt.datetime.now(dt.UTC)
            time.sleep(self._sleep)  # time to first token
            first_token = dt.datetime.now(dt.UTC)
            time.sleep(self._sleep)
            if final:
                decision = Decision(text=self._answer(history))
                output: Any = decision.text
            else:
                tool, tool_input = SCRIPT[self._turn]
                decision = Decision(tool=tool, tool_input=tool_input)
                output = {"tool_call": {"name": tool, "input": tool_input}}
            self._turn += 1
            gen.update(
                output=output,
                completion_start_time=first_token if first_token > started else started,
                usage_details={
                    "input": _tokens(messages) + 60,
                    "output": _tokens(output) + 8,
                    "total": _tokens(messages) + _tokens(output) + 68,
                },
            )
        return decision

    @staticmethod
    def _answer(history: list[dict[str, Any]]) -> str:
        failed = any("error" in str(m.get("content", "")).lower() for m in history)
        base = "Damaged items can be refunded within 30 days; two units at 49.99 come to 99.98."
        if failed:
            base += (
                " I could not look up order A-1042 right now, so I have escalated it to a human."
            )
        return base
