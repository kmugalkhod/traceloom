from traceloom_sdk import score, start_span


def scores(recorder):
    return [e["body"] for e in recorder.of_type("score-create")]


def test_score_sends_config_session_and_dataset_run(client, recorder):
    client.score(
        "quality", 0.8, trace_id="t1", config_id="cfg1", session_id="s1", dataset_run_id="run1"
    )
    client.flush()
    (body,) = scores(recorder)
    assert (body["configId"], body["sessionId"], body["datasetRunId"]) == ("cfg1", "s1", "run1")
    assert body["traceId"] == "t1" and body["value"] == 0.8


def test_new_params_are_omitted_when_unset(client, recorder):
    client.score("quality", 1, trace_id="t1")
    client.flush()
    (body,) = scores(recorder)
    assert not {"configId", "sessionId", "datasetRunId"} & body.keys()


def test_session_score_needs_no_trace(client, recorder):
    client.score("csat", "good", session_id="s9", config_id="cfg2")
    client.flush()
    (body,) = scores(recorder)
    assert body["sessionId"] == "s9" and "traceId" not in body


def test_module_score_and_observation_score_pass_the_params(client, recorder):
    with start_span("work") as span:
        span.score("a", 1, config_id="c1", dataset_run_id="r1")
        score("b", True, config_id="c2")
    client.flush()
    by_name = {b["name"]: b for b in scores(recorder)}
    assert by_name["a"]["configId"] == "c1" and by_name["a"]["datasetRunId"] == "r1"
    assert by_name["b"]["configId"] == "c2" and by_name["b"]["dataType"] == "BOOLEAN"
