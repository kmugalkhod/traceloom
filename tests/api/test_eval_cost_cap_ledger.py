import threading
from datetime import UTC, datetime

from traceloom.db.models_eval import EvalSpendDaily
from traceloom.evals import spend
from traceloom.evals.spend import CapReached, Estimate

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def run_threads(count, work):
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def go(i):
        barrier.wait()
        try:
            results[i] = work(i)
        except Exception as exc:  # collected for the assertions
            results[i] = exc

    threads = [threading.Thread(target=go, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_20_concurrent_reservations_never_exceed_the_cap(db, container, world):
    sessions, pid = container.session_factory, world.project.id
    results = run_threads(
        20, lambda _: spend.reserve(sessions, pid, Estimate(0.3, True), cap=1.0, now=NOW)
    )
    granted = [r for r in results if isinstance(r, spend.Reservation)]
    refused = [r for r in results if isinstance(r, CapReached)]
    assert len(granted) == 3 and len(refused) == 17  # 3 x 0.3 fit under 1.0, a 4th does not
    row = db.get(EvalSpendDaily, (pid, NOW.date()))
    assert row.reserved_usd + row.spent_usd <= 1.0


def test_concurrent_settles_and_releases_balance_the_ledger(db, container, world):
    sessions, pid = container.session_factory, world.project.id
    held = [spend.reserve(sessions, pid, Estimate(0.1, True), cap=10, now=NOW) for _ in range(10)]

    def finish(i):
        if i % 2:
            spend.release(sessions, held[i])
        else:
            spend.settle(sessions, held[i], 0.05)

    assert not [r for r in run_threads(10, finish) if r is not None]
    db.expire_all()
    row = db.get(EvalSpendDaily, (pid, NOW.date()))
    assert row.reserved_usd == 0 or abs(row.reserved_usd) < 1e-9
    assert abs(row.spent_usd - 0.25) < 1e-9


def test_a_cap_of_zero_pauses_judges_even_with_an_empty_ledger(container, world):
    sessions, pid = container.session_factory, world.project.id
    results = run_threads(5, lambda _: spend.reserve(sessions, pid, Estimate(0.01, True), 0))
    assert all(isinstance(r, CapReached) for r in results)
