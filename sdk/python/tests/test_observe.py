import asyncio

import pytest

from traceloom_sdk import observe, update_current_trace


def by_name(recorder, event_type):
    return {
        e["body"]["name"]: e["body"] for e in recorder.of_type(event_type) if "name" in e["body"]
    }


def updates(recorder):
    return {e["body"]["id"]: e["body"] for e in recorder.events if e["type"].endswith("-update")}


def test_sync_nesting_input_and_output(client, recorder):
    @observe
    def child(x, y=2):
        return x + y

    @observe(name="parent-op", as_type="agent")
    def parent(n):
        return child(n) * 2

    assert parent(3) == 10
    client.flush()
    creates = by_name(recorder, "observation-create") | by_name(recorder, "span-create")
    root, kid = creates["parent-op"], creates["child"]
    assert root["type"] == "agent" and "parentObservationId" not in root
    assert kid["parentObservationId"] == root["id"] and kid["traceId"] == root["traceId"]
    assert root["input"] == {"n": 3} and kid["input"] == {"x": 3, "y": 2}
    ups = updates(recorder)
    assert ups[root["id"]]["output"] == 10 and ups[kid["id"]]["output"] == 5
    trace = [e["body"] for e in recorder.of_type("trace-create")]
    assert any(t.get("name") == "parent-op" for t in trace) and any(
        t.get("output") == 10 for t in trace
    )
    assert len({t["id"] for t in trace}) == 1  # one trace for the whole call tree


def test_capture_flags_and_method_self_is_dropped(client, recorder):
    class Svc:
        @observe(capture_input=False, capture_output=False)
        def hidden(self, secret):
            return "s3cret"

        @observe
        def shown(self, a):
            return a

    Svc().hidden("pw")
    Svc().shown(1)
    client.flush()
    creates = by_name(recorder, "span-create")
    assert "input" not in creates["hidden"] and creates["shown"]["input"] == {"a": 1}
    ups = updates(recorder)
    assert "output" not in ups[creates["hidden"]["id"]]


def test_exception_sets_error_and_reraises(client, recorder):
    @observe
    def outer():
        inner()

    @observe
    def inner():
        raise ValueError("bad thing")

    with pytest.raises(ValueError, match="bad thing"):
        outer()
    client.flush()
    creates = by_name(recorder, "span-create")
    ups = updates(recorder)
    for name in ("outer", "inner"):
        assert ups[creates[name]["id"]]["level"] == "ERROR"
    assert ups[creates["inner"]["id"]]["statusMessage"] == "ValueError: bad thing"


def test_async_function_nesting_and_error(client, recorder):
    @observe
    async def leaf(x):
        await asyncio.sleep(0)
        if x < 0:
            raise RuntimeError("neg")
        return x

    @observe
    async def branch():
        a, b = await asyncio.gather(leaf(1), leaf(2))
        return a + b

    assert asyncio.run(branch()) == 3
    with pytest.raises(RuntimeError):
        asyncio.run(leaf(-1))
    client.flush()
    spans = [e["body"] for e in recorder.of_type("span-create")]
    branch_id = next(s["id"] for s in spans if s["name"] == "branch")
    leaves = [s for s in spans if s["name"] == "leaf" and s["input"]["x"] > 0]
    assert len(leaves) == 2 and all(s["parentObservationId"] == branch_id for s in leaves)
    bad = next(s for s in spans if s["name"] == "leaf" and s["input"]["x"] < 0)
    assert updates(recorder)[bad["id"]]["level"] == "ERROR"


def test_generator_collects_output_and_nests_children(client, recorder):
    @observe
    def step(i):
        return i * 10

    @observe(as_type="chain")
    def stream():
        for i in range(3):
            yield step(i)
        return "finished"

    gen = stream()
    first = next(gen)
    assert first == 0
    # between yields the consumer is outside the observation
    assert client.get_current_trace_id() is None
    assert list(gen) == [10, 20]
    client.flush()
    chain = by_name(recorder, "observation-create")["stream"]
    steps = [e["body"] for e in recorder.of_type("span-create") if e["body"]["name"] == "step"]
    assert len(steps) == 3 and all(s["parentObservationId"] == chain["id"] for s in steps)
    up = updates(recorder)[chain["id"]]
    assert up["output"] == [0, 10, 20] and "level" not in up


def test_generator_send_return_and_error(client, recorder):
    @observe
    def echo():
        got = yield "ready"
        yield got
        return 99

    g = echo()
    assert next(g) == "ready"
    assert g.send("hello") == "hello"
    with pytest.raises(StopIteration) as stop:
        next(g)
    assert stop.value.value == 99

    @observe
    def broken():
        yield 1
        raise KeyError("k")

    with pytest.raises(KeyError):
        list(broken())
    client.flush()
    creates = by_name(recorder, "span-create")
    assert updates(recorder)[creates["broken"]["id"]]["level"] == "ERROR"
    assert updates(recorder)[creates["echo"]["id"]]["output"] == ["ready", "hello"]


def test_generator_closed_early_still_ends_the_span(client, recorder):
    @observe
    def numbers():
        yield from range(10)

    g = numbers()
    next(g)
    g.close()
    client.flush()
    span = by_name(recorder, "span-create")["numbers"]
    assert updates(recorder)[span["id"]]["output"] == [0]


def test_async_generator_collects_nests_and_errors(client, recorder):
    @observe
    async def fetch(i):
        return i

    @observe(as_type="chain")
    async def agen():
        for i in range(2):
            yield await fetch(i)

    @observe
    async def bad():
        yield 1
        raise ValueError("late")

    async def main():
        out = [x async for x in agen()]
        with pytest.raises(ValueError):
            async for _ in bad():
                pass
        return out

    assert asyncio.run(main()) == [0, 1]
    client.flush()
    chain = by_name(recorder, "observation-create")["agen"]
    fetches = [e["body"] for e in recorder.of_type("span-create") if e["body"]["name"] == "fetch"]
    assert len(fetches) == 2 and all(f["parentObservationId"] == chain["id"] for f in fetches)
    assert updates(recorder)[chain["id"]]["output"] == [0, 1]
    bad_span = by_name(recorder, "span-create")["bad"]
    assert updates(recorder)[bad_span["id"]]["level"] == "ERROR"


def test_update_current_trace_inside_observe(client, recorder):
    @observe
    def handler():
        update_current_trace(user_id="u-9", tags=["t"])

    handler()
    client.flush()
    assert any(e["body"].get("userId") == "u-9" for e in recorder.of_type("trace-create"))


def test_sdk_failures_never_break_the_decorated_function(make_client, monkeypatch, sdk_logs):
    c = make_client()
    from traceloom_sdk import objects, set_default_client

    set_default_client(c)
    monkeypatch.setattr(c, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sdk bug")))

    @observe
    def work():
        return "fine"

    assert work() == "fine"
    monkeypatch.setattr(objects, "start", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert work() == "fine"


def test_disabled_client_still_runs_the_function(sdk_logs):
    @observe
    def work(a):
        return a

    assert work(4) == 4  # default client is created from the (empty) environment: disabled
