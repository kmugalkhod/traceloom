"""A support agent traced with Traceloom.

A planner model picks tools (search, calculator, lookup_order) and then writes a final answer.
`lookup_order` always fails, so the trace has one tool error. Runs offline against a built-in
fake model, or against Anthropic when ANTHROPIC_API_KEY is set.

    python examples/agent.py [--question "..."]
"""

from __future__ import annotations

import argparse
import ast
import json
import operator
import os
import sys
from typing import Any

import traceloom_sdk
from traceloom_sdk import observe

try:  # `python examples/agent.py` or imported as a package
    from examples.fake_llm import Decision, FakeLLM
except ImportError:
    from fake_llm import Decision, FakeLLM

QUESTION = (
    "A customer got a damaged item: what is the refund policy, "
    "what do two units cost, and where is order A-1042?"
)
MAX_STEPS = 5

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


@observe(as_type="tool", name="search")
def search(query: str) -> str:
    return "Policy: damaged items are refundable within 30 days with a photo."


@observe(as_type="tool", name="calculator")
def calculator(expression: str) -> str:
    def evaluate(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](evaluate(node.left), evaluate(node.right))
        raise ValueError("unsupported expression")

    return str(round(evaluate(ast.parse(expression, mode="eval")), 2))


@observe(as_type="tool", name="lookup_order")
def lookup_order(order_id: str) -> str:
    raise ConnectionError(f"orders service unavailable while looking up {order_id}")


TOOLS = {"search": search, "calculator": calculator, "lookup_order": lookup_order}


@observe(as_type="chain", name="step")
def step(llm: Any, question: str, history: list[dict[str, Any]]) -> bool:
    """One planner turn. Returns True when the agent has its final answer.

    History is in the OpenAI chat shape: the assistant's tool call carries an id and the tool
    result names it, so a recorded generation can be replayed against a real provider."""
    decision = llm.complete(question, history)
    if decision.tool is None:
        history.append({"role": "assistant", "content": decision.text})
        return True
    call_id = decision.call_id or f"call_{sum(m['role'] == 'tool' for m in history) + 1}"
    call = {"name": decision.tool, "arguments": json.dumps(decision.tool_input)}
    history.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "type": "function", "function": call}],
        }
    )
    try:
        result = TOOLS[decision.tool](**decision.tool_input)
    except Exception as exc:  # the failed tool span is already marked ERROR; tell the model
        result = f"error: {exc}"
    history.append({"role": "tool", "tool_call_id": call_id, "content": str(result)})
    return False


def _transcript_line(message: dict[str, Any]) -> str:
    calls = message.get("tool_calls") or ()
    if calls:
        fn = calls[0]["function"]
        return f"assistant: calling {fn['name']}({fn['arguments']})"
    return f"{message['role']}: {message['content']}"


class AnthropicLLM:
    """Plans with a real model; it replies with JSON naming a tool call or the final answer."""

    SYSTEM = (
        "You are a support agent. Tools: search(query), calculator(expression), "
        "lookup_order(order_id). Reply with only JSON: "
        '{"tool": "<name>", "input": {...}} to call a tool, or {"answer": "<text>"} when done. '
        "If a tool reports an error, do not retry it; answer with what you have."
    )

    def __init__(self) -> None:
        import anthropic
        from traceloom_sdk.anthropic import wrap

        self.client = wrap(anthropic.Anthropic())  # every call becomes a generation
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    def complete(self, question: str, history: list[dict[str, Any]]) -> Decision:
        transcript = "\n".join(_transcript_line(m) for m in history)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=self.SYSTEM,
            messages=[{"role": "user", "content": f"Question: {question}\n{transcript}"}],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        try:
            data = json.loads(text[text.index("{") : text.rindex("}") + 1])
        except ValueError:
            return Decision(text=text)
        if data.get("tool") in TOOLS:
            return Decision(tool=data["tool"], tool_input=data.get("input") or {})
        return Decision(text=str(data.get("answer", text)))


def make_llm() -> Any:
    return AnthropicLLM() if os.environ.get("ANTHROPIC_API_KEY") else FakeLLM()


def run(question: str = QUESTION, llm: Any = None) -> dict[str, Any]:
    """Run the agent once. The whole run is one trace rooted at the agent span."""
    llm = llm or make_llm()
    with traceloom_sdk.start_observation(
        "support-agent", as_type="agent", input={"question": question}
    ) as agent:
        traceloom_sdk.update_current_trace(
            user_id="demo-user", session_id="demo-session", tags=["demo", "support"]
        )
        history: list[dict[str, Any]] = []
        for _ in range(MAX_STEPS):
            if step(llm, question, history):
                break
        answer = history[-1]["content"]
        agent.update(output=answer)
    return {"answer": answer, "trace_id": agent.trace_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", default=QUESTION)
    args = parser.parse_args(argv)
    client = traceloom_sdk.get_client()
    result = run(args.question)
    delivered = client.flush(timeout=10)
    print(result["answer"])
    if not client.enabled:
        print("Tracing is off: set TRACELOOM_PUBLIC_KEY and TRACELOOM_SECRET_KEY.", file=sys.stderr)
    elif not delivered:
        print("Could not deliver the trace to TRACELOOM_HOST.", file=sys.stderr)
    else:
        print(f"trace: {result['trace_id']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
