from __future__ import annotations

import pytest

from traceloom.auth.permissions import Role
from traceloom.datasets import compare, runs
from traceloom.datasets.service import Actor, DatasetError
from traceloom.db.base import utcnow
from traceloom.ports import ScoreRow


@pytest.fixture
def env(db, world, make_dataset, make_dataset_item):
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    dataset = make_dataset(world.project)
    items = [make_dataset_item(dataset, {"q": f"q{n}"}, {"a": n}) for n in range(5)]
    return dataset, items, member


def make_run(env, db, world, container, name, values, *, kind="NUMERIC"):
    dataset, items, member = env
    run = runs.create_run(db, world.project, member, dataset, name=name)
    links = []
    for item, value in zip(items, values, strict=True):
        tid = f"{name}-{item.id}"
        runs.link_run_item(
            db, world.project, member, run, item, trace_id=tid, output="o", latency_ms=10.0,
            cost=0.01,
        )  # fmt: skip
        links.append((tid, value))
    db.commit()  # release the write lock before the trace store writes
    for tid, value in links:
        container.trace_store.add_score(
            world.project.id,
            ScoreRow(
                id=f"s-{tid}", name="correct", timestamp=utcnow(), trace_id=tid, value=value,
                data_type=kind, source="EVAL", dataset_run_id=run.id,
            ),
        )  # fmt: skip
    return run


def go(env, db, world, container, runs_, **opts):
    return compare.compare_runs(
        db, container.trace_store, world.project, env[0], [r.id for r in runs_], **opts
    )


def test_best_marker_and_regressed_items(env, db, world, container):
    v1 = make_run(env, db, world, container, "v1", [0.5] * 5)
    v2 = make_run(env, db, world, container, "v2", [0.9, 0.9, 0.9, 0.4, 0.3])
    out = go(env, db, world, container, [v1, v2], only_regressed=True)
    a1, a2 = out.aggregates
    assert a2.scores["correct"].best and not a1.scores["correct"].best
    assert a2.scores["correct"].n == 5 and a2.total_cost == pytest.approx(0.05)
    assert {r.item_id for r in out.rows} == {env[1][3].id, env[1][4].id}


def test_threshold_hides_small_deltas(env, db, world, container):
    v1 = make_run(env, db, world, container, "v1", [0.5] * 5)
    v2 = make_run(env, db, world, container, "v2", [0.9, 0.9, 0.9, 0.4, 0.3])
    out = go(env, db, world, container, [v1, v2], only_regressed=True, threshold=0.15)
    assert [r.item_id for r in out.rows] == [env[1][4].id]


def test_boolean_true_to_false_regresses(env, db, world, container):
    v1 = make_run(env, db, world, container, "v1", [1, 1, 1, 1, 0], kind="BOOLEAN")
    v2 = make_run(env, db, world, container, "v2", [1, 0, 1, 1, 1], kind="BOOLEAN")
    out = go(env, db, world, container, [v1, v2], only_regressed=True, threshold=2)
    assert [r.item_id for r in out.rows] == [env[1][1].id]


def test_other_dataset_run_rejected(env, db, world, container, make_dataset):
    v1 = make_run(env, db, world, container, "v1", [0.5] * 5)
    other = make_dataset(world.project)
    with pytest.raises(DatasetError):
        compare.compare_runs(db, container.trace_store, world.project, other, [v1.id])


def test_pagination_100_per_page(db, world, container, make_dataset, make_dataset_item):
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    dataset = make_dataset(world.project)
    items = [make_dataset_item(dataset, {"q": n}) for n in range(130)]
    r = runs.create_run(db, world.project, member, dataset, name="r")
    for item in items:
        runs.link_run_item(db, world.project, member, r, item, latency_ms=1.0)
    db.commit()
    args = (db, container.trace_store, world.project, dataset, [r.id])
    first = compare.compare_runs(*args)
    second = compare.compare_runs(*args, page=2)
    assert (len(first.rows), len(second.rows), first.pages) == (100, 30, 2)
