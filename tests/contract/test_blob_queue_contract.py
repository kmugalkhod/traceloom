"""Contract for BlobStore and Queue. Lite adapters always; S3/Redis run under TRACELOOM_IT=1
(their fake-backed unit tests live in tests/unit)."""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from traceloom.adapters.lite.inproc_queue import InProcQueue
from traceloom.adapters.lite.local_blob import LocalBlobStore
from traceloom.ports import BlobNotFound, BlobStoreError


@pytest.fixture(params=["local"])
def blobs(request, tmp_path):
    return LocalBlobStore(tmp_path / "blobs")


def test_put_get_overwrite(blobs):
    blobs.put("pending/p1/a.json", b"one")
    assert blobs.get("pending/p1/a.json") == b"one"
    blobs.put("pending/p1/a.json", b"two")
    assert blobs.get("pending/p1/a.json") == b"two"


def test_missing_blob_raises_not_found(blobs):
    with pytest.raises(BlobNotFound):
        blobs.get("nope")
    with pytest.raises(BlobNotFound):
        blobs.move("nope", "elsewhere")


def test_move_and_delete(blobs):
    blobs.put("pending/p1/a.json", b"x")
    blobs.move("pending/p1/a.json", "processed/p1/a.json")
    assert blobs.get("processed/p1/a.json") == b"x"
    with pytest.raises(BlobNotFound):
        blobs.get("pending/p1/a.json")
    blobs.delete("processed/p1/a.json")
    blobs.delete("processed/p1/a.json")  # idempotent
    with pytest.raises(BlobNotFound):
        blobs.get("processed/p1/a.json")


def test_list_by_prefix_with_modified_time(blobs):
    blobs.put("pending/p1/a.json", b"1")
    blobs.put("pending/p2/b.json", b"2")
    blobs.put("processed/p1/c.json", b"3")
    listed = dict(blobs.list("pending/"))
    assert set(listed) == {"pending/p1/a.json", "pending/p2/b.json"}
    assert all(isinstance(t, datetime) and t.tzinfo for t in listed.values())
    assert list(blobs.list("nothing/")) == []


@pytest.mark.parametrize("key", ["../escape", "/abs/path", "", "a/../../escape"])
def test_keys_cannot_escape_the_root(blobs, key):
    with pytest.raises(BlobStoreError):
        blobs.put(key, b"x")


def test_write_failure_is_a_blob_store_error(tmp_path):
    (tmp_path / "file").write_text("not a dir")
    store = LocalBlobStore(tmp_path / "file")  # root is a file: mkdir fails
    with pytest.raises(BlobStoreError):
        store.put("pending/p/a.json", b"x")


# --- Queue ---


@pytest.fixture(params=["inproc"])
def queue(request):
    q = InProcQueue()
    yield q
    q.stop()


def test_enqueue_is_delivered_to_the_handler(queue):
    seen = []
    queue.start(seen.append)
    queue.enqueue({"n": 1})
    queue.enqueue({"n": 2})
    queue.drain()
    assert seen == [{"n": 1}, {"n": 2}]
    assert queue.ping()


def test_pause_holds_messages_and_resume_releases_them(queue):
    seen = []
    queue.start(seen.append)
    queue.pause()
    for n in range(3):
        queue.enqueue({"n": n})
    time.sleep(0.2)
    assert seen == [] and queue.pending == 3
    queue.resume()
    queue.drain()
    assert [m["n"] for m in seen] == [0, 1, 2]


def test_drain_while_paused_processes_in_the_caller(queue):
    seen, threads = [], set()
    queue.start(lambda m: (seen.append(m), threads.add(threading.current_thread())))
    queue.pause()
    queue.enqueue({"n": 1})
    queue.drain()
    assert seen == [{"n": 1}] and threads == {threading.current_thread()}


def test_handler_errors_do_not_kill_the_consumer(queue):
    seen = []

    def handler(message):
        if message["n"] == 1:
            raise RuntimeError("boom")
        seen.append(message["n"])

    queue.start(handler)
    for n in (1, 2):
        queue.enqueue({"n": n})
    queue.drain()
    assert seen == [2] and queue.ping()


def test_discard_simulates_a_crash(queue):
    seen = []
    queue.start(seen.append)
    queue.pause()
    queue.enqueue({"n": 1})
    assert queue.discard() == 1
    queue.resume()
    queue.drain()
    assert seen == []


def test_stop_makes_ping_false(queue):
    queue.start(lambda m: None)
    assert queue.ping()
    queue.stop()
    assert not queue.ping()
