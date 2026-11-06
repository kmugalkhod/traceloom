"""LangChain callback handler: maps runs to Traceloom observations.

    from traceloom_sdk.langchain import CallbackHandler
    chain.invoke(inputs, config={"callbacks": [CallbackHandler()]})

chain -> chain, LLM and chat model -> generation, tool -> tool, retriever -> retriever. Runs are
linked by `parent_run_id`; a run with no parent nests under the current `@observe` observation,
or starts a new trace. The module imports without langchain installed.
"""

from __future__ import annotations

import datetime as _dt
import functools
import logging
from typing import Any, Callable, Dict, List, Optional

from traceloom_sdk import context, objects
from traceloom_sdk.client import Traceloom, get_client

try:  # pragma: no cover - exercised only where langchain is installed
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover
    BaseCallbackHandler = object  # type: ignore[assignment,misc]

log = logging.getLogger("traceloom")

_ROLES = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool",
          "function": "function", "chat": "user", "generic": "user"}  # fmt: skip


def _guard(fn: Callable) -> Callable:
    """Handler methods never raise into LangChain (and so never into user code)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception("Traceloom LangChain handler: %s failed", fn.__name__)
            return None

    return wrapper


def _name(serialized: Optional[Dict[str, Any]], kwargs: Dict[str, Any], default: str) -> str:
    if kwargs.get("name"):
        return str(kwargs["name"])
    serialized = serialized or {}
    if serialized.get("name"):
        return str(serialized["name"])
    ident = serialized.get("id")
    if isinstance(ident, (list, tuple)) and ident:
        return str(ident[-1])
    return default


def _message(message: Any) -> Any:
    if isinstance(message, dict):
        return message
    kind = getattr(message, "type", None)
    content = getattr(message, "content", message)
    if kind is None:
        return content
    out: Dict[str, Any] = {"role": _ROLES.get(kind, kind), "content": content}
    for extra in ("tool_calls", "name", "tool_call_id"):
        value = getattr(message, extra, None)
        if value:
            out[extra] = value
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "type") and hasattr(value, "content"):
        return _message(value)
    for attr in ("model_dump", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return method()
            except Exception:
                break
    return value


def _usage(response: Any, generation: Any) -> Dict[str, int]:
    raw = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
    meta = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
    pairs = {
        "input": raw.get("prompt_tokens", meta.get("input_tokens")),
        "output": raw.get("completion_tokens", meta.get("output_tokens")),
        "total": raw.get("total_tokens", meta.get("total_tokens")),
    }
    return {k: int(v) for k, v in pairs.items() if isinstance(v, (int, float)) and v}


class CallbackHandler(BaseCallbackHandler):  # type: ignore[misc,valid-type]
    raise_error = False
    run_inline = True

    def __init__(
        self,
        client: Optional[Traceloom] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._trace_fields = {
            k: v
            for k, v in {"user_id": user_id, "session_id": session_id, "tags": tags}.items()
            if v
        }
        self._runs: Dict[Any, objects.Observation] = {}
        self._ttft: set = set()

    # -- plumbing --------------------------------------------------------------------------

    def _begin(
        self, run_id: Any, parent_run_id: Any, as_type: str, name: str, **fields: Any
    ) -> objects.Observation:
        client = self._client or get_client()
        parent = self._runs.get(parent_run_id) if parent_run_id is not None else None
        if parent is not None:
            obs = parent.start_observation(name, as_type, **fields)
        else:
            ambient = context.current_observation()
            if ambient is not None:
                obs = ambient.start_observation(name, as_type, **fields)
            else:
                obs = objects.start(client, as_type, name, fields)
                if self._trace_fields:
                    obs.update_trace(**self._trace_fields)
        self._runs[run_id] = obs
        return obs

    def _end(self, run_id: Any, **fields: Any) -> None:
        obs = self._runs.pop(run_id, None)
        self._ttft.discard(run_id)
        if obs is not None:
            obs.end(**fields)

    def _fail(self, run_id: Any, error: BaseException) -> None:
        obs = self._runs.get(run_id)
        if obs is not None:
            objects.mark_error(obs, error)
        self._end(run_id)

    # -- chains ----------------------------------------------------------------------------

    @_guard
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        name = _name(serialized, kwargs, "chain")
        self._begin(run_id, parent_run_id, "chain", name, input=_plain(inputs),
                    metadata=kwargs.get("metadata") or None)  # fmt: skip

    @_guard
    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._end(run_id, output=_plain(outputs))

    @_guard
    def on_chain_error(self, error, *, run_id, **kwargs):
        self._fail(run_id, error)

    # -- LLMs and chat models --------------------------------------------------------------

    def _start_generation(self, serialized, input, run_id, parent_run_id, kwargs) -> None:
        params = dict(kwargs.get("invocation_params") or {})
        meta = kwargs.get("metadata") or {}
        model = (
            params.pop("model", None) or params.pop("model_name", None) or meta.get("ls_model_name")
        )
        params.pop("_type", None)
        self._begin(
            run_id, parent_run_id, "generation", _name(serialized, kwargs, "llm"),
            input=input, model=model, model_parameters=_plain(params) or None,
        )  # fmt: skip

    @_guard
    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        text = prompts[0] if isinstance(prompts, list) and len(prompts) == 1 else prompts
        self._start_generation(serialized, text, run_id, parent_run_id, kwargs)

    @_guard
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        batch = [[_message(m) for m in group] for group in messages]
        self._start_generation(
            serialized, batch[0] if len(batch) == 1 else batch, run_id, parent_run_id, kwargs
        )

    @_guard
    def on_llm_new_token(self, token, *, run_id, **kwargs):
        obs = self._runs.get(run_id)
        if obs is not None and run_id not in self._ttft:
            self._ttft.add(run_id)
            obs.update(completion_start_time=_dt.datetime.now(_dt.timezone.utc))

    @_guard
    def on_llm_end(self, response, *, run_id, **kwargs):
        generations = getattr(response, "generations", None) or [[]]
        first = generations[0][0] if generations and generations[0] else None
        message = getattr(first, "message", None)
        output = _message(message) if message is not None else getattr(first, "text", None)
        fields: Dict[str, Any] = {"output": output, "usage_details": _usage(response, first)}
        model = (getattr(response, "llm_output", None) or {}).get("model_name")
        if model:
            fields["model"] = model
        self._end(run_id, **fields)

    @_guard
    def on_llm_error(self, error, *, run_id, **kwargs):
        self._fail(run_id, error)

    # -- tools and retrievers --------------------------------------------------------------

    @_guard
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        value = kwargs.get("inputs") or input_str
        self._begin(run_id, parent_run_id, "tool", _name(serialized, kwargs, "tool"),
                    input=_plain(value))  # fmt: skip

    @_guard
    def on_tool_end(self, output, *, run_id, **kwargs):
        self._end(run_id, output=getattr(output, "content", None) or _plain(output))

    @_guard
    def on_tool_error(self, error, *, run_id, **kwargs):
        self._fail(run_id, error)

    @_guard
    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, **kwargs):
        self._begin(run_id, parent_run_id, "retriever", _name(serialized, kwargs, "retriever"),
                    input=query)  # fmt: skip

    @_guard
    def on_retriever_end(self, documents, *, run_id, **kwargs):
        docs = [
            {
                "page_content": getattr(d, "page_content", str(d)),
                "metadata": getattr(d, "metadata", {}),
            }
            for d in documents or []
        ]
        self._end(run_id, output=docs)

    @_guard
    def on_retriever_error(self, error, *, run_id, **kwargs):
        self._fail(run_id, error)
