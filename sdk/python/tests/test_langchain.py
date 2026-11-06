import builtins
import importlib
import sys
import uuid
from types import SimpleNamespace

import pytest

from traceloom_sdk import observe
from traceloom_sdk.langchain import CallbackHandler


def rid():
    return uuid.uuid4()


def creates(recorder):
    out = {}
    for e in recorder.events:
        if e["type"].endswith("-create") and e["type"] != "trace-create":
            out[e["body"]["name"]] = e["body"]
    return out


def updates(recorder):
    return {e["body"]["id"]: e["body"] for e in recorder.events if e["type"].endswith("-update")}


class Msg(SimpleNamespace):
    """A BaseMessage look-alike."""


def llm_result(text="answer", usage=None, model="gpt-x", message=True):
    gen = SimpleNamespace(text=text)
    if message:
        gen.message = Msg(type="ai", content=text, usage_metadata=usage or {})
    return SimpleNamespace(generations=[[gen]], llm_output={"model_name": model})


def test_run_tree_maps_types_and_links_parents(client, recorder):
    h = CallbackHandler()
    chain, retr, llm, tool = rid(), rid(), rid(), rid()
    h.on_chain_start({"name": "rag"}, {"q": "why"}, run_id=chain, parent_run_id=None)
    h.on_retriever_start({"id": ["x", "Retriever"]}, "why", run_id=retr, parent_run_id=chain)
    h.on_retriever_end([SimpleNamespace(page_content="doc", metadata={"s": 1})], run_id=retr)
    h.on_chat_model_start(
        {"name": "ChatX"},
        [[Msg(type="system", content="be nice"), Msg(type="human", content="why")]],
        run_id=llm,
        parent_run_id=chain,
        invocation_params={"model_name": "gpt-x", "temperature": 0, "_type": "chat"},
    )
    h.on_llm_new_token("a", run_id=llm)
    h.on_llm_new_token("b", run_id=llm)
    h.on_llm_end(
        llm_result(usage={"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}), run_id=llm
    )
    h.on_tool_start({"name": "calc"}, "1+1", run_id=tool, parent_run_id=chain)
    h.on_tool_end(Msg(content="2"), run_id=tool)
    h.on_chain_end({"answer": "because"}, run_id=chain)
    client.flush()

    made = creates(recorder)
    assert made["rag"]["type"] == "chain" and "parentObservationId" not in made["rag"]
    assert made["Retriever"]["type"] == "retriever" and made["ChatX"]["type"] == "generation"
    assert made["calc"]["type"] == "tool"
    for name in ("Retriever", "ChatX", "calc"):
        assert made[name]["parentObservationId"] == made["rag"]["id"]
        assert made[name]["traceId"] == made["rag"]["traceId"]
    gen = made["ChatX"]
    assert gen["model"] == "gpt-x" and gen["modelParameters"] == {"temperature": 0}
    assert gen["input"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "why"},
    ]
    ups = updates(recorder)
    llm_up = ups[gen["id"]]
    assert llm_up["output"] == {"role": "assistant", "content": "answer"}
    assert llm_up["usageDetails"] == {"input": 9, "output": 4, "total": 13}
    assert "completionStartTime" in llm_up and llm_up["model"] == "gpt-x"
    assert ups[made["Retriever"]["id"]]["output"] == [{"page_content": "doc", "metadata": {"s": 1}}]
    assert ups[made["calc"]["id"]]["output"] == "2"
    assert ups[made["rag"]["id"]]["output"] == {"answer": "because"}
    assert h._runs == {}  # nothing leaks


def test_completion_llm_token_usage_from_llm_output(client, recorder):
    h = CallbackHandler()
    llm = rid()
    h.on_llm_start(
        {"name": "OpenAI"}, ["prompt text"], run_id=llm, invocation_params={"model": "m"}
    )
    result = llm_result("done", message=False)
    result.llm_output["token_usage"] = {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    h.on_llm_end(result, run_id=llm)
    client.flush()
    made = creates(recorder)["OpenAI"]
    assert made["input"] == "prompt text" and made["model"] == "m"
    up = updates(recorder)[made["id"]]
    assert up["output"] == "done" and up["usageDetails"] == {"input": 3, "output": 2, "total": 5}


def test_errors_set_level_error(client, recorder):
    h = CallbackHandler()
    chain, tool = rid(), rid()
    h.on_chain_start({"name": "c"}, {}, run_id=chain)
    h.on_tool_start({"name": "t"}, "x", run_id=tool, parent_run_id=chain)
    h.on_tool_error(ValueError("tool broke"), run_id=tool)
    h.on_chain_error(RuntimeError("chain broke"), run_id=chain)
    client.flush()
    made, ups = creates(recorder), updates(recorder)
    assert ups[made["t"]["id"]]["level"] == "ERROR"
    assert ups[made["t"]["id"]]["statusMessage"] == "ValueError: tool broke"
    assert ups[made["c"]["id"]]["level"] == "ERROR"


def test_nests_under_the_current_observe_span_and_applies_trace_fields(client, recorder):
    h = CallbackHandler(user_id="u1", session_id="s1", tags=["t"])

    @observe
    def outer():
        run = rid()
        h.on_chain_start({"name": "inner"}, {}, run_id=run)
        h.on_chain_end({}, run_id=run)

    outer()
    standalone = rid()
    h.on_chain_start({"name": "solo"}, {}, run_id=standalone)
    h.on_chain_end({}, run_id=standalone)
    client.flush()
    made = creates(recorder)
    assert made["inner"]["parentObservationId"] == made["outer"]["id"]
    assert made["inner"]["traceId"] == made["outer"]["traceId"]
    solo_trace = made["solo"]["traceId"]
    tagged = [e["body"] for e in recorder.of_type("trace-create") if e["body"].get("userId")]
    assert [t["id"] for t in tagged] == [solo_trace] and tagged[0]["tags"] == ["t"]


def test_handler_errors_never_reach_user_code(client, sdk_logs):
    h = CallbackHandler()
    h.on_chain_start(None, None, run_id="r1", parent_run_id="never-started")  # fine
    h.on_chat_model_start({}, "not-a-list-of-lists", run_id="r2")  # malformed input
    h.on_llm_end(object(), run_id="r3")  # unknown run
    h.on_tool_end(None, run_id="missing")
    h.on_chain_error(RuntimeError("x"), run_id="missing")
    assert h.raise_error is False


def test_real_langchain_runnables_produce_a_nested_trace(client, recorder):
    core = pytest.importorskip("langchain_core")
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.runnables import RunnableLambda

    chain = RunnableLambda(lambda x: f"question: {x}") | FakeListChatModel(
        responses=["hello there"]
    )
    result = chain.invoke("hi", config={"callbacks": [CallbackHandler()]})
    assert result.content == "hello there"
    client.flush()
    types = [
        e["body"]["type"]
        for e in recorder.events
        if e["type"] in ("observation-create", "generation-create", "span-create")
    ]
    assert "generation" in types and "chain" in types
    assert core is not None


def test_module_imports_without_langchain(monkeypatch):
    real_import = builtins.__import__

    def deny(name, *args, **kwargs):
        if name.startswith("langchain"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)
    monkeypatch.delitem(sys.modules, "traceloom_sdk.langchain", raising=False)
    module = importlib.import_module("traceloom_sdk.langchain")
    assert module.BaseCallbackHandler is object
    handler = module.CallbackHandler()
    handler.on_chain_start({"name": "x"}, {}, run_id="a")
    handler.on_chain_end({}, run_id="a")
