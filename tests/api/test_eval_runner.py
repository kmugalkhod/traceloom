from __future__ import annotations

import pytest

from tests.api.eval_helpers import SECRET, capture_eval_jobs, good, setup
from traceloom.db.models import ScoreConfig
from traceloom.evals import runner
from traceloom.llm.providers import DoneDelta, ErrorDelta, TextDelta


@pytest.fixture
def env(client, container, db, world, make_key, make_eval_template, make_eval_rule,
        make_eval_execution, fake_provider, monkeypatch):  # fmt: skip
    class Env:
        pass

    e = Env()
    e.__dict__.update(
        setup(client, container, db, world, make_key, make_eval_template, make_eval_rule,
              make_eval_execution).__dict__,
    )  # fmt: skip
    e.sent = capture_eval_jobs(container, monkeypatch)
    e.fake, e.container, e.db, e.world = fake_provider, container, db, world

    def run_job():
        runner.handle(container, {"execution_id": e.execution.id})
        container.queue.drain()
        db.expire_all()
        return e.execution

    e.run = run_job
    return e


def scores(env):
    return env.container.trace_store.get_trace(env.world.project.id, "t1").scores


def test_success_writes_one_eval_score_and_a_judge_trace(env, settings):
    env.fake.script(*good())
    ex = env.run()
    assert (ex.status, ex.attempts, ex.error) == ("succeeded", 1, None)
    [score] = scores(env)
    assert (score.name, score.value, score.source, score.comment) == (
        "relevance", 0.8, "EVAL", "grounded in the answer",
    )  # fmt: skip
    assert score.execution_id == ex.id and score.id == ex.score_id
    judge = env.container.trace_store.get_trace(env.world.project.id, ex.judge_trace_id)
    assert judge.trace.environment == settings.eval_environment
    assert judge.trace.name.startswith("eval:")
    assert ex.cost and ex.cost > 0 and judge.trace.total_cost == pytest.approx(ex.cost)
    call = env.fake.calls[0]
    assert call["response_schema"]["properties"]["score"]["type"] == "number"
    assert "2+2" not in str(call["messages"][0])  # the instructions never carry the object


def test_invalid_json_then_valid_output_succeeds_on_attempt_2(env):
    env.fake.script(TextDelta("I think it is fine."), DoneDelta("stop"))
    env.fake.script(*good())
    ex = env.run()
    assert (ex.status, ex.attempts) == ("pending", 1)
    [(message, delay)] = env.sent
    assert message == {"type": "eval_job", "execution_id": ex.id} and delay > 0
    ex = env.run()
    assert (ex.status, ex.attempts, ex.error) == ("succeeded", 2, None)
    assert len(scores(env)) == 1


def test_a_fenced_reply_with_text_around_it_is_parsed(env):
    env.fake.script(*good('Sure!\n```json\n{"score": "0.5", "reasoning": "ok"}\n```'))
    assert env.run().status == "succeeded"
    assert scores(env)[0].value == 0.5


def test_a_failing_provider_ends_failed_after_3_attempts_with_a_scrubbed_error(env):
    for _ in range(3):
        env.fake.script(ErrorDelta(f"401 bad key {SECRET} at Bearer abcdefghijkl"))
    for attempt in (1, 2, 3):
        ex = env.run()
        assert ex.attempts == attempt
    assert ex.status == "failed" and len(env.sent) == 2
    assert SECRET not in ex.error and "abcdefghijkl" not in ex.error
    assert "[redacted]" in ex.error
    assert scores(env) == []


def test_a_provider_exception_is_retried_and_scrubbed(env):
    env.fake.script(RuntimeError(f"boom {SECRET}"))
    ex = env.run()
    assert ex.status == "pending" and "boom" in ex.error and SECRET not in ex.error


def test_out_of_range_output_is_invalid(env):
    env.fake.script(*good('{"score": 7, "reasoning": "x"}'))
    ex = env.run()
    assert ex.status == "pending" and "outside" in ex.error


def test_backoff_grows_with_jitter():
    first, third = runner.backoff_seconds(1), runner.backoff_seconds(3)
    assert 2 <= first <= 3 and 8 <= third <= 12
    assert runner.backoff_seconds(30) <= runner.BACKOFF_MAX_SECONDS * 1.5


def test_a_missing_variable_fails_without_retries(env):
    env.template.latest.prompt = "Rate {{answer}}"
    env.db.commit()
    ex = env.run()
    assert (ex.status, ex.attempts) == ("failed", 1) and "answer" in ex.error
    assert env.sent == [] and env.fake.calls == []


def test_an_archived_config_fails_without_retries(env):
    from traceloom.db.base import utcnow

    config = ScoreConfig(project_id=env.world.project.id, name="relevance", data_type="NUMERIC",
                         archived_at=utcnow())  # fmt: skip
    env.db.add(config)
    env.db.flush()
    env.rule.config_id = config.id
    env.db.commit()
    ex = env.run()
    assert (ex.status, ex.attempts) == ("failed", 1) and "archived" in ex.error
    assert env.sent == [] and env.fake.calls == []


def test_a_missing_connection_fails_without_retries(env):
    env.db.delete(env.connection)
    env.db.commit()
    ex = env.run()
    assert (ex.status, ex.attempts) == ("failed", 1) and "connection" in ex.error
    assert env.sent == []


def test_a_job_whose_filter_no_longer_matches_is_skipped(env):
    env.rule.filter = {"name": "something-else"}
    env.db.commit()
    ex = env.run()
    assert ex.status == "skipped" and "filter" in ex.error
    assert env.fake.calls == [] and scores(env) == []


def test_redelivery_overwrites_the_same_score(env):
    env.fake.script(*good())
    ex = env.run()
    first = ex.score_id
    env.fake.script(*good('{"score": 0.3, "reasoning": "second"}'))
    ex.status = "pending"  # the worker died after writing the score, so the job runs again
    env.db.commit()
    ex = env.run()
    [score] = scores(env)
    assert score.id == first == ex.score_id and (score.value, score.comment) == (0.3, "second")


def test_a_message_for_a_finished_job_does_nothing(env):
    env.fake.script(*good())
    env.run()
    env.fake.script(*good())
    ex = env.run()
    assert ex.attempts == 1 and len(env.fake.calls) == 1


def test_manual_retry_resets_a_failed_execution(env, container):
    env.fake.script(ErrorDelta("down"))
    env.run()
    env.execution.attempts = 3
    env.execution.status = "failed"
    env.db.commit()
    assert runner.retry_execution(container, "someone-else", env.execution.id) is False
    assert runner.retry_execution(container, env.world.project.id, env.execution.id) is True
    env.db.expire_all()
    assert (env.execution.status, env.execution.attempts, env.execution.error) == (
        "pending", 0, None,
    )  # fmt: skip
    assert env.sent[-1][0]["execution_id"] == env.execution.id
    assert runner.retry_execution(container, env.world.project.id, env.execution.id) is False


def test_the_handler_is_registered_on_startup(client, container):
    assert container.queue._registry.has("eval_job")  # noqa: SLF001
