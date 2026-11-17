"""Shared setup for the run comparison page tests."""

from __future__ import annotations

from traceloom.auth.permissions import Role
from traceloom.datasets import runs
from traceloom.datasets.service import Actor
from traceloom.db.base import utcnow
from traceloom.ports import ScoreRow


def actor(world, role: Role) -> Actor:
    return Actor.for_user(world.users[role], role)


def make_run(db, world, container, dataset, items, member, name, values, outputs=None):
    run = runs.create_run(db, world.project, member, dataset, name=name)
    links = []
    for n, (item, value) in enumerate(zip(items, values, strict=True)):
        tid = f"{name}-{item.id}"
        runs.link_run_item(
            db, world.project, member, run, item, trace_id=tid,
            output=outputs[n] if outputs else f"out {name} {n}", latency_ms=1200.0, cost=0.01,
        )  # fmt: skip
        links.append((tid, value))
    db.commit()  # release the write lock before the trace store writes
    for tid, value in links:
        container.trace_store.add_score(
            world.project.id,
            ScoreRow(
                id=f"s-{tid}", name="correct", timestamp=utcnow(), trace_id=tid, value=value,
                data_type="NUMERIC", source="EVAL", dataset_run_id=run.id,
            ),
        )  # fmt: skip
    return run
