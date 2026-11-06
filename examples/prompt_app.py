"""Fetch a managed prompt, compile it and run it through a fake model, with prompt linking.

Fetches `support` from its `production` label (cached for TRACELOOM_PROMPT_CACHE_TTL seconds),
compiles it, and records one generation linked to that prompt version. If the server cannot be
reached, the built-in fallback prompt is used instead and the generation is not linked.

    TRACELOOM_PUBLIC_KEY=... TRACELOOM_SECRET_KEY=... python examples/prompt_app.py --name Ann
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import traceloom_sdk

try:  # `python examples/prompt_app.py` or imported as a package
    from examples.fake_llm import ANSWER_MODEL
except ImportError:
    from fake_llm import ANSWER_MODEL

FALLBACK = [
    {"role": "system", "content": "You are a support assistant helping {{name}}."},
    {"role": "user", "content": "{{question}}"},
]


def run(name: str, question: str, prompt_name: str = "support") -> dict[str, Any]:
    prompt = traceloom_sdk.get_prompt(prompt_name, fallback=FALLBACK, type="chat")
    messages = prompt.compile(name=name, question=question)
    with traceloom_sdk.start_generation(
        "support-answer",
        model=ANSWER_MODEL,
        input=messages,
        model_parameters={"temperature": 0.0},
        prompt=prompt,  # a fallback prompt is silently not linked
    ) as gen:
        time.sleep(0.01)  # the fake model "thinks"
        answer = f"Hello {name}! Here is help with: {question}"
        gen.update(
            output=answer,
            usage_details={
                "input": sum(len(str(m["content"])) // 4 + 1 for m in messages),
                "output": len(answer) // 4 + 1,
            },
        )
    return {
        "version": prompt.version,
        "is_fallback": prompt.is_fallback,
        "messages": messages,
        "answer": answer,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="Ann")
    parser.add_argument("--question", default="How do I get a refund?")
    parser.add_argument("--prompt", default="support")
    args = parser.parse_args(argv)
    result = run(args.name, args.question, args.prompt)
    source = "fallback" if result["is_fallback"] else f"{args.prompt} v{result['version']}"
    print(f"prompt: {source}")
    for message in result["messages"]:
        print(f"  [{message['role']}] {message['content']}")
    print(f"answer: {result['answer']}")
    traceloom_sdk.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
