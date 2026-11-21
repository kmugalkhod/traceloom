"""The judge worker: the `eval_job` handler (FR-J5, J6, J7; ADR-0014).

One message wakes one `eval_executions` row. The row is the source of truth, so the handler is
safe under at-least-once delivery: it claims the row (pending -> running) with a conditional
UPDATE and a message for a row that is not pending does nothing. A claimed job

1. re-checks the rule's filter on the latest data (no longer matching: `skipped`),
2. resolves the template variables and compiles the prompt,
3. reserves its estimated cost against the daily cap (over the cap: `capped`, not retried),
4. calls the model with the score schema as structured output, recorded as a judge trace in the
   judge environment,
5. parses and validates the reply and writes one EVAL score with a deterministic id, so a second
   delivery overwrites the same score.

Provider errors and invalid output are retried with exponential backoff and jitter up to
`eval_max_attempts`. A missing variable, an archived score config or a missing connection fails at
once. Error text is scrubbed of the connection's secrets and of anything key-shaped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from traceloom.container import Container
from traceloom.db.base import utcnow
from traceloom.db.models import LLMConnection
from traceloom.db.models_eval import EvalExecution, EvalRule, EvalTemplate, EvalTemplateVersion
from traceloom.evals import rule_service, spend
from traceloom.evals.mapping import MappingError, ObjectContext, resolve_variables
from traceloom.evals.rules import OBSERVATION, TRACE, _filter_matches, score_conditions, truthy
from traceloom.ingest.accept import IngestLimits, accept_batch
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy, Secrets
from traceloom.llm.run import Run, RunSpec
from traceloom.ports import LEVELS, ObservationRow, ScoreRow, TraceRow
from traceloom.prompts.variables import compile_text
from traceloom.queue_registry import EVAL_JOB
from traceloom.scores.configs import config_cache, score_check_for

log = logging.getLogger("traceloom.evals")

BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 300.0
ERROR_MAX = 1000
RETRYABLE_STATUSES = ("failed", "capped")

_SYSTEM = (
    "You are an evaluator. Judge the content you are given and reply with only a JSON object of "
    'the form {"score": <value>, "reasoning": "<one or two sentences>"}. %s'
)


class Permanent(Exception):
    """The job cannot succeed however often it runs; the message is safe to show."""


class Retryable(Exception):
    """A provider or output problem that another attempt may fix."""


class Skipped(Exception):
    """The job no longer applies."""


@dataclass(frozen=True)
class Judgement:
    value: float | str | bool
    data_type: str
    reasoning: str


def score_id_for(execution_id: str) -> str:
    """Deterministic, so re-delivery overwrites the same score."""
    return hashlib.sha256(f"eval-score:{execution_id}".encode()).hexdigest()[:32]


# --- Schema and output -------------------------------------------------------------------------


def response_schema(output_schema: Mapping[str, Any]) -> dict[str, Any]:
    kind = output_schema["type"]
    if kind == "numeric":
        score: dict[str, Any] = {
            "type": "number",
            "minimum": output_schema["min"],
            "maximum": output_schema["max"],
        }
    elif kind == "boolean":
        score = {"type": "boolean"}
    else:
        score = {"type": "string", "enum": list(output_schema["categories"])}
    return {
        "type": "object",
        "properties": {"score": score, "reasoning": {"type": "string"}},
        "required": ["score", "reasoning"],
        "additionalProperties": False,
    }


def _describe(output_schema: Mapping[str, Any]) -> str:
    kind = output_schema["type"]
    if kind == "numeric":
        return f'"score" is a number from {output_schema["min"]} to {output_schema["max"]}.'
    if kind == "boolean":
        return '"score" is true or false.'
    return '"score" is exactly one of: ' + ", ".join(output_schema["categories"]) + "."


def extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in a reply, tolerating code fences and text around it."""
    text = re.sub(r"```(?:json)?", "", text or "")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            found, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(found, dict):
            return found
    raise Retryable("the judge did not return a JSON object")


def validate_output(output_schema: Mapping[str, Any], reply: str) -> Judgement:
    payload = extract_json(reply)
    if "score" not in payload:
        raise Retryable('the judge output has no "score"')
    raw, kind = payload["score"], output_schema["type"]
    reasoning = payload.get("reasoning")
    reasoning = reasoning.strip() if isinstance(reasoning, str) else ""
    if kind == "numeric":
        if isinstance(raw, str):
            try:
                raw = float(raw)
            except ValueError:
                raise Retryable("the judge score is not a number") from None
        if isinstance(raw, bool) or not isinstance(raw, int | float) or raw != raw:
            raise Retryable("the judge score is not a number")
        if not output_schema["min"] <= raw <= output_schema["max"]:
            raise Retryable(
                f"the judge score {raw:g} is outside {output_schema['min']}..{output_schema['max']}"
            )
        return Judgement(float(raw), "NUMERIC", reasoning)
    if kind == "boolean":
        words = {"true": True, "false": False, "yes": True, "no": False, "1": True, "0": False}
        value = raw if isinstance(raw, bool) else words.get(str(raw).strip().lower())
        if value is None:
            raise Retryable("the judge score is not true or false")
        return Judgement(value, "BOOLEAN", reasoning)
    labels = {c.lower(): c for c in output_schema["categories"]}
    label = labels.get(str(raw).strip().lower()) if isinstance(raw, str) else None
    if label is None:
        raise Retryable("the judge score is not one of the categories")
    return Judgement(label, "CATEGORICAL", reasoning)


# --- Filter re-check and context ---------------------------------------------------------------


def still_matches(
    rule: EvalRule,
    row: TraceRow | ObservationRow,
    trace: TraceRow | None = None,
    scores: Sequence[ScoreRow] = (),
) -> bool:
    """The rule's filter on the object as it is now (`trace`: an observation's parent; `scores`:
    the trace's scores, which the score conditions are checked against)."""
    f = rule.filter or {}
    if f.get("scores") and score_conditions(f) is None:
        return False
    if not _filter_matches(f, row, trace, list(scores)):
        return False
    level = row.max_level if isinstance(row, TraceRow) else row.level
    rank = LEVELS.index(level) if level in LEVELS else 1
    if f.get("min_level") in LEVELS and rank < LEVELS.index(f["min_level"]):
        return False
    return not (f.get("has_error") is not None and truthy(f["has_error"]) != (level == "ERROR"))


def _context(row: TraceRow | ObservationRow, observations: list[ObservationRow]) -> ObjectContext:
    return ObjectContext(
        input=row.input,
        output=row.output,
        metadata=row.metadata or {},
        observations=[
            {"name": o.name, "input": o.input, "output": o.output}
            for o in sorted(observations, key=lambda o: o.start_time)
        ],
    )


# --- Spend and scrubbing -----------------------------------------------------------------------


def scrub_error(text: str, secrets: list[str]) -> str:
    return providers.scrub(str(text), secrets)[:ERROR_MAX]


def backoff_seconds(attempts: int) -> float:
    delay = min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)
    return delay + random.uniform(0, delay / 2)  # jitter spreads retries of a failing provider


# --- The handler -------------------------------------------------------------------------------


def handle(container: Container, message: Mapping[str, Any]) -> None:
    """`eval_job {execution_id}`. Never raises: every outcome is recorded on the row."""
    execution_id = message.get("execution_id")
    if not isinstance(execution_id, str) or not _claim(container, execution_id):
        return
    secrets: list[str] = []
    try:
        _execute(container, execution_id, secrets)
    except Skipped as exc:
        _finish(container, execution_id, "skipped", error=str(exc))
    except Permanent as exc:
        _finish(container, execution_id, "failed", error=scrub_error(str(exc), secrets))
    except spend.CapReached as exc:
        _finish(container, execution_id, "capped", error=str(exc))
    except Retryable as exc:
        _retry(container, execution_id, scrub_error(str(exc), secrets))
    except Exception as exc:  # a bug or an outage must not leave the row running
        log.exception("eval job %s crashed", execution_id)
        _retry(container, execution_id, scrub_error(f"{type(exc).__name__}: {exc}", secrets))


def _claim(container: Container, execution_id: str) -> bool:
    with container.session_factory.begin() as db:
        return bool(
            db.execute(
                update(EvalExecution)
                .where(EvalExecution.id == execution_id, EvalExecution.status == "pending")
                .values(
                    status="running",
                    attempts=EvalExecution.attempts + 1,
                    started_at=utcnow(),
                    finished_at=None,
                )
            ).rowcount
        )


def _finish(
    container: Container,
    execution_id: str,
    status: str,
    *,
    error: str | None = None,
    **values: Any,
) -> None:
    with container.session_factory.begin() as db:
        db.execute(
            update(EvalExecution)
            .where(EvalExecution.id == execution_id)
            .values(status=status, error=error, finished_at=utcnow(), estimated_cost=0.0, **values)
        )


def _retry(container: Container, execution_id: str, error: str) -> None:
    """Back to pending with a delayed wake-up, or `failed` once the attempts are used up."""
    max_attempts = container.settings.eval_max_attempts
    with container.session_factory.begin() as db:
        attempts = db.scalar(select(EvalExecution.attempts).where(EvalExecution.id == execution_id))
        if (attempts or 0) < max_attempts:
            delay = backoff_seconds(attempts or 1)
            db.execute(
                update(EvalExecution)
                .where(EvalExecution.id == execution_id)
                .values(
                    status="pending",
                    error=error,
                    started_at=None,
                    estimated_cost=0.0,
                    run_after=datetime.now(UTC) + timedelta(seconds=delay),
                )
            )
        else:
            delay = None
    if delay is None:
        _finish(container, execution_id, "failed", error=error)
        return
    try:
        container.queue.enqueue({"type": EVAL_JOB, "execution_id": execution_id}, delay)
    except Exception:  # the row is pending; the sweeper re-sends the wake-up
        log.exception("could not enqueue the retry of eval job %s", execution_id)


def retry_execution(container: Container, project_id: str, execution_id: str) -> bool:
    """Manual retry of a failed (or capped) execution: a fresh run of the attempts. False when
    the execution is not the project's or is not in a retryable state."""
    with container.session_factory.begin() as db:
        changed = db.execute(
            update(EvalExecution)
            .where(
                EvalExecution.id == execution_id,
                EvalExecution.project_id == project_id,
                EvalExecution.status.in_(RETRYABLE_STATUSES),
            )
            .values(
                status="pending",
                attempts=0,
                error=None,
                started_at=None,
                finished_at=None,
                run_after=utcnow(),
            )
        ).rowcount
    if changed:
        container.queue.enqueue({"type": EVAL_JOB, "execution_id": execution_id})
    return bool(changed)


def _execute(container: Container, execution_id: str, secrets: list[str]) -> None:
    settings, sessions, store = container.settings, container.session_factory, container.trace_store
    with sessions() as db:
        ex = db.get(EvalExecution, execution_id)
        if ex is None:
            return
        project_id, object_type, object_id = ex.project_id, ex.object_type, ex.object_id
        trace_id, attempt = ex.trace_id or ex.object_id, ex.attempts
        version = db.get(EvalTemplateVersion, ex.template_version_id)
        rule: Any = db.get(EvalRule, ex.rule_id) if ex.rule_id else None
        experiment = (
            _experiment_job(db, ex, settings.experiment_environment) if ex.run_item_id else None
        )
        if experiment is not None:
            rule = experiment.rule
        if version is None or rule is None:
            raise Permanent("the rule or the template version no longer exists")
        template = db.get(EvalTemplate, version.template_id)
        model = rule_service.effective_model(
            db, project_id, rule.connection_id, rule.model, version
        )
        connection = db.get(LLMConnection, model.connection_id) if model.connection_id else None
        db.expunge_all()
    policy = ConnectionPolicy.from_settings(settings)

    # 1. The object, and whether the rule still wants it.
    row: Any
    if experiment is not None:  # bound from the run item and its item version, not the trace
        row, context = experiment.row, experiment.context
    else:
        detail = store.get_trace(project_id, trace_id)
        if detail is None:
            raise Skipped("the trace no longer exists")
        row = detail.trace
        if object_type == OBSERVATION:
            row = next((o for o in detail.observations if o.id == object_id), None)
        if row is None or object_type not in (TRACE, OBSERVATION):
            raise Skipped("the object no longer exists")
        if not still_matches(rule, row, detail.trace, detail.scores):
            raise Skipped("the object no longer matches the rule's filter")
        context = _context(row, detail.observations if object_type == TRACE else [])

    # 2. Score config, variables, model.
    if rule.config_id:
        snapshot = config_cache(container).get(project_id, rule.config_id)
        if snapshot is None or snapshot.archived:
            raise Permanent("the score config is archived or no longer exists")
    variables = _variables(version.prompt)
    try:
        values = resolve_variables(variables, rule.mapping, context)
    except MappingError as exc:
        raise Permanent(str(exc)) from exc
    prompt = compile_text(version.prompt, values).text
    if connection is None or not model.model:
        raise Permanent("no LLM connection and model are set for this judge")
    cfg = connections.provider_config(policy, connection)
    if cfg is None:
        raise Permanent("the LLM connection's key needs re-entry")
    stored = connections_secrets(policy, connection)
    secrets.extend(stored)
    schema = dict(version.output_schema)
    params = dict(version.params or {})

    # 3. The cap, then the call.
    definition = rule_service.model_def(container.models, project_id, model.model)
    estimate = spend.estimate_cost(
        definition, spend.count_prompt_tokens(definition, prompt), params.get("max_tokens")
    )
    with sessions() as db:
        cap = spend.get_cap(db, project_id, settings)
    reservation = spend.reserve(sessions, project_id, estimate, cap)
    with sessions.begin() as db:
        db.execute(
            update(EvalExecution)
            .where(EvalExecution.id == execution_id)
            .values(estimated_cost=reservation.amount)
        )
    # This claim (the attempt number) still owns the row: not reset by the sweeper meanwhile.
    held = spend.claim_check(EvalExecution, execution_id, EvalExecution.attempts == attempt)
    try:
        reply, judge_trace_id, cost = _call(
            container, project_id, template.name, prompt, schema, params, connection,
            model.model, secrets,
        )  # fmt: skip
    except BaseException:
        spend.release(sessions, reservation, held)  # no usable call happened
        raise
    spend.settle(sessions, reservation, cost, held)  # an unknown cost counts as the estimate

    # 4. Output, then the score.
    try:
        judgement = validate_output(schema, reply)
    except Retryable:
        _record_trace(container, execution_id, judge_trace_id, cost)
        raise
    _write_score(
        container, project_id, execution_id, rule, row, trace_id, object_type, judgement,
        dataset_run_id=experiment.run_id if experiment else None,
    )  # fmt: skip
    _finish(
        container,
        execution_id,
        "succeeded",
        score_id=score_id_for(execution_id),
        judge_trace_id=judge_trace_id,
        cost=cost,
    )
    log.info("eval %s succeeded on attempt %d", execution_id, attempt)


@dataclass(frozen=True)
class _ExperimentRule:
    """What an experiment evaluator supplies in place of a rule."""

    score_name: str
    mapping: dict[str, Any]
    config_id: str | None
    connection_id: str | None
    model: str | None
    filter: dict[str, Any]


@dataclass(frozen=True)
class _ExperimentJob:
    rule: _ExperimentRule
    row: Any  # carries `environment` for the score
    context: ObjectContext
    run_id: str


def _experiment_job(db, ex: EvalExecution, environment: str) -> _ExperimentJob | None:
    """The evaluator of a run item: the run's config, the run item's output and the item as of
    the run's version (so `{{expected_output}}` binds without waiting for the trace)."""
    from types import SimpleNamespace

    from traceloom.datasets import service as datasets
    from traceloom.db.models import Project
    from traceloom.db.models_datasets import DatasetRun, DatasetRunItem

    run_item = db.get(DatasetRunItem, ex.run_item_id)
    run = db.get(DatasetRun, run_item.run_id) if run_item else None
    if run is None:
        return None
    spec = next(
        (
            e
            for e in run.config.get("evaluators", [])
            if e["template_version_id"] == ex.template_version_id
        ),
        None,
    )
    if spec is None:
        return None
    project = db.get(Project, ex.project_id)
    item = datasets.item_at_version(
        db, project, datasets.get_item(db, project, run_item.item_id), run_item.item_version
    )
    rule = _ExperimentRule(
        score_name=spec["score_name"],
        mapping=spec.get("mapping") or {},
        config_id=spec.get("config_id"),
        connection_id=run.config.get("connection_id"),
        model=run.config.get("model"),
        filter={},
    )
    context = ObjectContext(
        input=item.input,
        output=run_item.output,
        expected_output=item.expected_output,
        metadata=item.metadata or {},
    )
    return _ExperimentJob(rule, SimpleNamespace(environment=environment), context, run.id)


def _variables(prompt: str) -> list[str]:
    from traceloom.prompts.variables import find_variables

    return find_variables(prompt)


def connections_secrets(policy: ConnectionPolicy, row: LLMConnection) -> list[str]:
    try:
        return Secrets.load(policy.box.decrypt(row.secret_ciphertext)).values()
    except Exception:
        return []


def _record_trace(
    container: Container, execution_id: str, judge_trace_id: str | None, cost: float | None
) -> None:
    """Keep the judge trace and cost of a failed attempt on the row, for the log."""
    with container.session_factory.begin() as db:
        db.execute(
            update(EvalExecution)
            .where(EvalExecution.id == execution_id)
            .values(judge_trace_id=judge_trace_id, cost=cost)
        )


def _call(
    container: Container,
    project_id: str,
    template_name: str,
    prompt: str,
    schema: dict[str, Any],
    params: dict[str, Any],
    connection: LLMConnection,
    model: str,
    secrets: list[str],
) -> tuple[str, str | None, float | None]:
    """One model call, recorded as a judge trace. Returns the reply text, the trace id and the
    cost. Raises Retryable on a provider error."""
    policy = ConnectionPolicy.from_settings(container.settings)
    cfg = connections.provider_config(policy, connection)
    assert cfg is not None
    messages = [
        {"role": "system", "content": _SYSTEM % _describe(schema)},
        {"role": "user", "content": prompt},
    ]
    spec = RunSpec(
        messages=messages,
        model=model,
        params=params,
        response_schema=response_schema(schema),
        trace=True,
        secrets=secrets,
        environment=container.settings.eval_environment,
        trace_name=f"eval:{template_name}",
        generation_name=f"eval:{template_name}",
    )
    text: list[str] = []
    errors: list[str] = []
    cost: float | None = None
    trace_id: str | None = None
    try:
        provider = providers.create(connection.provider, cfg)
        for event in Run(container, project_id, provider, spec).events():
            if event.name == "delta":
                text.append(event.data["text"])
            elif event.name == "error":
                errors.append(event.data["message"])
            elif event.name == "usage":
                cost = event.data.get("cost")
            elif event.name == "done":
                trace_id = event.data.get("trace_id")
    except Exception as exc:
        raise Retryable(scrub_error(f"{type(exc).__name__}: {exc}", secrets)) from exc
    if errors:
        raise Retryable(scrub_error(errors[0], secrets))
    return "".join(text), trace_id, cost


def _write_score(
    container: Container,
    project_id: str,
    execution_id: str,
    rule: EvalRule,
    row: TraceRow | ObservationRow,
    trace_id: str,
    object_type: str,
    judgement: Judgement,
    dataset_run_id: str | None = None,
) -> None:
    settings = container.settings
    body: dict[str, Any] = {
        "id": score_id_for(execution_id),
        "traceId": trace_id,
        "name": rule.score_name,
        "value": judgement.value,
        "dataType": judgement.data_type,
        "comment": judgement.reasoning or None,
        "executionId": execution_id,
        "environment": row.environment,
    }
    if dataset_run_id:
        body["datasetRunId"] = dataset_run_id
    if object_type == OBSERVATION:
        body["observationId"] = row.id
    if rule.config_id:
        body["configId"] = rule.config_id
    event = {
        "id": f"eval-{execution_id}",
        "type": "score-create",
        "timestamp": datetime.now(UTC).isoformat(),
        "body": body,
    }
    result = accept_batch(
        project_id,
        json.dumps({"batch": [event]}).encode(),
        container.blob_store,
        container.queue,
        IngestLimits(settings.ingest_max_events, settings.ingest_max_bytes),
        score_check_for(config_cache(container)),
        trusted=True,  # sets executionId (source=EVAL), which clients cannot
    )
    if result.errors:
        raise Permanent(f"the score was rejected: {result.errors[0]['message']}")
