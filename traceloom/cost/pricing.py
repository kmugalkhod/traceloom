"""Cost = sum(usage[key] x price[key]), with input-size tiers and a derived total (FR-C3/C4)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from traceloom.cost.registry import ModelDef, ModelRegistry
from traceloom.cost.tokenizers import count_tokens
from traceloom.ports import ObservationRow

Counter = Callable[[str | None, str], int | None]
INPUT_KEYS = ("input", "cache_read", "cache_write")


def effective_prices(definition: ModelDef, usage: Mapping[str, int]) -> dict[str, float]:
    """Base prices, overridden by the highest tier whose threshold the input tokens exceed."""
    prices = dict(definition.prices)
    input_tokens = sum(usage.get(k, 0) for k in INPUT_KEYS)
    for tier in definition.tiers:  # ascending thresholds
        if input_tokens > tier.threshold:
            prices = {**definition.prices, **tier.prices}
    return prices


def compute_cost(definition: ModelDef, usage: Mapping[str, int]) -> dict[str, float]:
    """Per-key costs plus `total`; empty when no usage key has a price."""
    prices = effective_prices(definition, usage)
    cost = {k: usage[k] * prices[k] for k in usage if k != "total" and k in prices}
    if cost:
        cost["total"] = sum(cost.values())
    return cost


def _text(value: Any) -> str:
    """Prompt/completion text for token counting; chat messages contribute their content."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(m, dict) and "content" in m for m in value):
        return "\n".join(_text(m["content"]) for m in value)
    return json.dumps(value, ensure_ascii=False, default=str)


def apply_cost(
    obs: ObservationRow,
    project_id: str,
    registry: ModelRegistry,
    counter: Counter = count_tokens,
) -> None:
    """Fill usage (tokenizer) and cost (calculated) on an observation, in place.

    Provided usage and cost are never touched. Derived values are recomputed on every call, so
    running it again gives the same result.
    """
    if obs.cost_source == "provided":
        if obs.cost_details and "total" not in obs.cost_details:
            obs.cost_details = {**obs.cost_details, "total": sum(obs.cost_details.values())}
        return
    definition = registry.find(project_id, obs.model, obs.start_time)
    if obs.usage_source != "provided":
        obs.usage_details, obs.usage_source = {}, "none"
        if definition and definition.tokenizer:
            counts = {
                "input": counter(definition.tokenizer, _text(obs.input)),
                "output": counter(definition.tokenizer, _text(obs.output)),
            }
            if all(v is not None for v in counts.values()) and any(counts.values()):
                obs.usage_details = {**counts, "total": counts["input"] + counts["output"]}
                obs.usage_source = "tokenizer"
    cost = compute_cost(definition, obs.usage_details) if definition else {}
    obs.cost_details, obs.cost_source = (cost, "calculated") if cost else ({}, "none")
