"""UI experiments (FR-D7; ADR-0013, ADR-0014): start a run and the `experiment_item` worker.

`start_experiment` creates the run at the dataset's current version with one `dataset_run_items`
row per active item, then enqueues one `experiment_item {run_item_id}` job per row. The run's
`config` is a snapshot, so later edits to the prompt do not change a running experiment:

    {"prompt_name", "prompt_version", "prompt_type", "prompt", "connection_id", "model", "params",
     "mapping": {variable: {"source", "path", "default"}},
     "evaluators": [{"template_version_id", "score_name", "mapping", "config_id"}]}

The handler claims the row (pending -> running), compiles the prompt with the item as of its
version, reserves the estimated cost against the daily cap (over the cap: the item is `capped`),
calls the provider traced in the experiment environment, and stores the output, latency and cost
on the run item. Then it creates one `eval_executions` row per evaluator (no delay, bound to the
run item) and enqueues them. The rows are the source of truth, so a restart is recovered by the
sweeper (`due_messages`): pending rows are re-sent and rows stuck in `running` are reset.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.datasets import runs, service
from traceloom.datasets.service import Actor, DatasetError
from traceloom.db.base import utcnow
from traceloom.db.models import LLMConnection, Project
from traceloom.db.models_datasets import Dataset, DatasetRun, DatasetRunItem
from traceloom.db.models_eval import EvalExecution, EvalTemplateVersion
from traceloom.evals import rule_service, spend
from traceloom.evals.mapping import MappingError, ObjectContext, resolve_variables
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy, Secrets
from traceloom.llm.run import Run, RunSpec
from traceloom.prompts import service as prompt_service
from traceloom.prompts.variables import compile_messages, find_message_variables, find_variables
from traceloom.queue_registry import EVAL_JOB, EXPERIMENT_ITEM
from traceloom.services import audit

log = logging.getLogger("traceloom.experiments")

DUE_GRACE_SECONDS = 30
ERROR_MAX = 1000


class Failed(Exception):
    """The item cannot be processed; the message is safe to show."""


# --- Start -------------------------------------------------------------------------------------


def start_experiment(
    db: Session,
    container: Container,
    project: Project,
    actor: Actor,
    dataset: Dataset,
    *,
    name: str,
    prompt_name: str,
    prompt_version: int | None = None,
    prompt_label: str | None = None,
    connection_id: str,
    model: str,
    params: Mapping[str, Any] | None = None,
    mapping: Mapping[str, Any] | None = None,
    evaluators: list[Mapping[str, Any]] | None = None,
) -> DatasetRun:
    """Create the run and its items, then enqueue one job per item (after the commit)."""
    actor.require(Perm.EXPERIMENTS_RUN, "You need the Member role to run experiments.")
    connection = db.get(LLMConnection, connection_id)
    if connection is None or connection.project_id != project.id:
        raise DatasetError("Pick an LLM connection of this project.")
    if not (model or "").strip():
        raise DatasetError("Pick a model.")
    prompt_actor = prompt_service.Actor(actor.type, actor.id, actor.role, actor.name)
    try:
        view = prompt_service.get(
            db, project, prompt_actor, prompt_name, label=prompt_label, version=prompt_version
        )
    except (prompt_service.PromptError, prompt_service.PromptNotFound) as exc:
        raise DatasetError(str(exc)) from exc
    chosen = []
    for spec in evaluators or []:
        version = db.get(EvalTemplateVersion, spec.get("template_version_id"))
        if version is None:
            raise DatasetError("An evaluator template no longer exists.")
        chosen.append(
            {
                "template_version_id": version.id,
                "score_name": spec.get("score_name") or "experiment",
                "mapping": dict(spec.get("mapping") or {}),
                "config_id": spec.get("config_id"),
            }
        )
    config = {
        "prompt_name": view.name,
        "prompt_version": view.version,
        "prompt_type": view.type,
        "prompt": view.prompt,
        "connection_id": connection.id,
        "model": model.strip(),
        "params": dict(params or {}),
        "mapping": dict(mapping or {}),
        "evaluators": chosen,
    }
    run = runs.create_run(db, project, actor, dataset, name=name, config=config)
    now = utcnow()
    rows = [
        DatasetRunItem(
            run_id=run.id,
            project_id=project.id,
            item_id=item.id,
            item_version=item.version,
            status="pending",
            run_after=now,
        )
        for item in runs.run_item_source(db, project, run)
    ]
    db.add_all(rows)
    audit.record(
        db,
        action="experiment.start",
        actor_type=actor.type,
        actor_id=actor.id,
        org_id=project.org_id,
        project_id=project.id,
        target=run.id,
        detail={"dataset": dataset.name, "run": run.name, "items": len(rows), "model": model},
    )
    db.commit()
    for row in rows:
        container.queue.enqueue({"type": EXPERIMENT_ITEM, "run_item_id": row.id})
    if not rows:
        runs.set_status(db, project, actor, run, "completed")
        db.commit()
    return run


# --- The handler -------------------------------------------------------------------------------


def handle(container: Container, message: Mapping[str, Any]) -> None:
    """`experiment_item {run_item_id}`. Never raises: every outcome is recorded on the row."""
    run_item_id = message.get("run_item_id")
    if not isinstance(run_item_id, str) or not _claim(container, run_item_id):
        return
    secrets: list[str] = []
    try:
        _execute(container, run_item_id, secrets)
    except spend.CapReached as exc:
        _finish(container, run_item_id, "capped", error=str(exc))
    except Failed as exc:
        _finish(container, run_item_id, "failed", error=providers.scrub(str(exc), secrets))
    except Exception as exc:  # a bug or an outage must not leave the row running
        log.exception("experiment item %s crashed", run_item_id)
        text = providers.scrub(f"{type(exc).__name__}: {exc}", secrets)
        _finish(container, run_item_id, "failed", error=text[:ERROR_MAX])
    _complete_run_if_done(container, run_item_id)


def _claim(container: Container, run_item_id: str) -> bool:
    with container.session_factory.begin() as db:
        return bool(
            db.execute(
                update(DatasetRunItem)
                .where(DatasetRunItem.id == run_item_id, DatasetRunItem.status == "pending")
                .values(status="running", run_after=utcnow(), error=None)
            ).rowcount
        )


def _finish(
    container: Container, run_item_id: str, status: str, *, error: str | None = None, **values: Any
) -> None:
    with container.session_factory.begin() as db:
        db.execute(
            update(DatasetRunItem)
            .where(DatasetRunItem.id == run_item_id)
            .values(
                status=status,
                error=error[:ERROR_MAX] if error else None,
                run_after=None,
                estimated_cost=0.0,
                **values,
            )
        )


def _execute(container: Container, run_item_id: str, secrets: list[str]) -> None:
    settings, sessions = container.settings, container.session_factory
    with sessions() as db:
        row = db.get(DatasetRunItem, run_item_id)
        run = db.get(DatasetRun, row.run_id) if row else None
        if row is None or run is None:
            raise Failed("the run item no longer exists")
        project_id, item_id, item_version, config = (
            row.project_id,
            row.item_id,
            row.item_version,
            run.config,
        )
        project = db.get(Project, project_id)
        item = service.item_at_version(
            db, project, service.get_item(db, project, item_id), item_version
        )
        connection = db.get(LLMConnection, config.get("connection_id"))
        if connection is None or connection.project_id != project_id:
            raise Failed("the LLM connection no longer exists")
        db.expunge_all()
    policy = ConnectionPolicy.from_settings(settings)
    cfg = connections.provider_config(policy, connection)
    if cfg is None:
        raise Failed("the LLM connection's key needs re-entry")
    try:
        secrets.extend(Secrets.load(policy.box.decrypt(connection.secret_ciphertext)).values())
    except Exception:  # noqa: S110 - scrubbing is best effort
        pass

    messages = _compile(config, item)
    model, params = config["model"], dict(config.get("params") or {})
    text = " ".join(str(m.get("content", "")) for m in messages)
    definition = rule_service.model_def(container.models, project_id, model)
    estimate = spend.estimate_cost(
        definition, spend.count_prompt_tokens(definition, text), params.get("max_tokens")
    )
    with sessions() as db:
        cap = spend.get_cap(db, project_id, settings)
    reservation = spend.reserve(sessions, project_id, estimate, cap)
    with sessions.begin() as db:
        db.execute(
            update(DatasetRunItem)
            .where(DatasetRunItem.id == run_item_id)
            .values(estimated_cost=reservation.amount)
        )
    try:
        output, trace_id, cost, latency, error = _call(
            container, project_id, config, messages, params, connection, cfg, secrets
        )
    except BaseException:
        spend.release(sessions, reservation)
        raise
    if error:
        spend.release(sessions, reservation)
        raise Failed(error)
    spend.settle(sessions, reservation, cost or 0.0)
    _finish(
        container,
        run_item_id,
        "completed",
        output=output,
        trace_id=trace_id,
        cost=cost,
        latency_ms=latency,
    )
    _create_evaluations(container, run_item_id, project_id, trace_id, config)


def _compile(config: Mapping[str, Any], item: service.ItemView) -> list[dict[str, Any]]:
    prompt = config["prompt"]
    messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
    names = find_variables(prompt) if isinstance(prompt, str) else find_message_variables(messages)
    ctx = ObjectContext(input=item.input, expected_output=item.expected_output,
                        metadata=item.metadata or {})  # fmt: skip
    try:
        values = resolve_variables(names, config.get("mapping"), ctx)
    except MappingError as exc:
        raise Failed(str(exc)) from exc
    return compile_messages(messages, values).messages


def _call(container, project_id, config, messages, params, connection, cfg, secrets):
    """One model call recorded as a trace in the experiment environment.
    Returns (output, trace_id, cost, latency_ms, error)."""
    spec = RunSpec(
        messages=messages,
        model=config["model"],
        params=params,
        trace=True,
        secrets=secrets,
        prompt_name=config.get("prompt_name"),
        prompt_version=config.get("prompt_version"),
        environment=container.settings.experiment_environment,
        trace_name=f"experiment:{config.get('prompt_name')}",
        generation_name=f"experiment:{config.get('prompt_name')}",
    )
    text: list[str] = []
    errors: list[str] = []
    cost = latency = trace_id = None
    provider = providers.create(connection.provider, cfg)
    for event in Run(container, project_id, provider, spec).events():
        if event.name == "delta":
            text.append(event.data["text"])
        elif event.name == "error":
            errors.append(event.data["message"])
        elif event.name == "usage":
            cost, latency = event.data.get("cost"), event.data.get("latency_ms")
        elif event.name == "done":
            trace_id = event.data.get("trace_id")
    return "".join(text), trace_id, cost, latency, errors[0] if errors else None


def _create_evaluations(
    container: Container, run_item_id: str, project_id: str, trace_id: str | None, config: Mapping
) -> None:
    """One execution per evaluator, due now, then the wake-ups. Safe to repeat."""
    if not trace_id:
        return
    ids: list[str] = []
    with container.session_factory.begin() as db:
        have = set(
            db.scalars(
                select(EvalExecution.template_version_id).where(
                    EvalExecution.run_item_id == run_item_id
                )
            )
        )
        for spec in config.get("evaluators") or []:
            if spec["template_version_id"] in have:
                continue
            ex = EvalExecution(
                project_id=project_id,
                rule_id=None,
                run_item_id=run_item_id,
                template_version_id=spec["template_version_id"],
                object_type="trace",
                object_id=trace_id,
                trace_id=trace_id,
                status="pending",
                run_after=utcnow(),
            )
            db.add(ex)
            db.flush()
            ids.append(ex.id)
    for execution_id in ids:
        try:
            container.queue.enqueue({"type": EVAL_JOB, "execution_id": execution_id})
        except Exception:  # the row is pending; the sweeper re-sends it
            log.exception("could not enqueue evaluator %s", execution_id)


def _complete_run_if_done(container: Container, run_item_id: str) -> None:
    """The run is `completed` once no item is pending or running (`failed` if every item failed)."""
    with container.session_factory.begin() as db:
        row = db.get(DatasetRunItem, run_item_id)
        if row is None:
            return
        statuses = list(
            db.scalars(select(DatasetRunItem.status).where(DatasetRunItem.run_id == row.run_id))
        )
        if any(s in ("pending", "running") for s in statuses):
            return
        status = "failed" if statuses and all(s == "failed" for s in statuses) else "completed"
        db.execute(
            update(DatasetRun)
            .where(DatasetRun.id == row.run_id, DatasetRun.status == "running")
            .values(status=status, finished_at=utcnow())
        )


# --- Sweeper -----------------------------------------------------------------------------------


def due_messages(container: Container) -> list[dict[str, Any]]:
    """`experiment_item` messages for pending items nobody picked up, after resetting items stuck
    in `running` (their reservation is released) back to pending."""
    settings, sessions = container.settings, container.session_factory
    now = datetime.now(UTC)
    stuck_before = now - timedelta(seconds=settings.eval_stuck_seconds)
    releases: list[spend.Reservation] = []
    reset_ids: list[str] = []
    with sessions.begin() as db:
        stuck = db.scalars(
            select(DatasetRunItem)
            .where(DatasetRunItem.status == "running", DatasetRunItem.run_after < stuck_before)
            .limit(settings.eval_sweep_batch)
        ).all()
        for row in stuck:
            project_id, started, held = row.project_id, row.run_after, row.estimated_cost or 0.0
            reset = db.execute(
                update(DatasetRunItem)
                .where(DatasetRunItem.id == row.id, DatasetRunItem.status == "running")
                .values(status="pending", run_after=now, estimated_cost=0.0)
            ).rowcount
            if reset:
                reset_ids.append(row.id)
            if reset and held > 0 and started is not None:
                releases.append(spend.Reservation(project_id, spend.utc_day(started), held))
        due_before = now - timedelta(seconds=DUE_GRACE_SECONDS)
        ids = db.scalars(
            select(DatasetRunItem.id)
            .where(
                DatasetRunItem.status == "pending",
                (DatasetRunItem.run_after.is_(None)) | (DatasetRunItem.run_after <= due_before),
            )
            .order_by(DatasetRunItem.run_after)
            .limit(settings.eval_sweep_batch)
        ).all()
    for reservation in releases:
        spend.release(sessions, reservation)
    return [{"type": EXPERIMENT_ITEM, "run_item_id": i} for i in [*reset_ids, *ids]]
