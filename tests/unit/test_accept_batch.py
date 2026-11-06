import json

import pytest

from traceloom.ingest.accept import (
    BatchTooLarge,
    IngestLimits,
    InvalidBatch,
    StorageUnavailable,
    accept_batch,
)
from traceloom.ports import BlobStoreError

LIMITS = IngestLimits(max_events=5, max_bytes=10_000)


class FakeBlobs:
    def __init__(self, fail=False):
        self.data, self.fail = {}, fail

    def put(self, key, data):
        if self.fail:
            raise BlobStoreError("disk full")
        self.data[key] = data


class FakeQueue:
    def __init__(self, fail=False):
        self.messages, self.fail = [], fail

    def enqueue(self, message):
        if self.fail:
            raise RuntimeError("redis down")
        self.messages.append(dict(message))


def good(i=1, type_="trace-create"):
    return {
        "id": f"e{i}",
        "type": type_,
        "timestamp": "2026-11-06T12:00:00Z",
        "body": {"id": f"t{i}"},
    }


def send(batch, blobs=None, queue=None, limits=LIMITS, **extra):
    blobs, queue = blobs or FakeBlobs(), queue or FakeQueue()
    raw = json.dumps({"batch": batch, **extra}).encode()
    return accept_batch("p1", raw, blobs, queue, limits), blobs, queue


def test_valid_events_are_stored_and_queued_invalid_are_reported():
    bad = {"id": "bad", "type": "nope", "timestamp": "2026-11-06T12:00:00Z", "body": {}}
    result, blobs, queue = send([good(1), bad, good(2)], metadata={"sdk": "x"})
    assert result.successes == [{"id": "e1", "status": 201}, {"id": "e2", "status": 201}]
    assert [(e["id"], e["status"]) for e in result.errors] == [("bad", 400)]
    assert "unsupported" in result.errors[0]["message"]
    assert len(queue.messages) == 1
    message = queue.messages[0]
    assert message["project_id"] == "p1" and message["blob_key"].startswith("pending/p1/")
    stored = json.loads(blobs.data[message["blob_key"]])
    assert [e["id"] for e in stored["events"]] == ["e1", "e2"]  # the invalid one is not stored
    assert stored["metadata"] == {"sdk": "x"}


def test_events_without_ids_get_an_index_label():
    result, _, queue = send([{"type": "trace-create"}, "junk"])
    assert [e["id"] for e in result.errors] == ["index-0", "index-1"]
    assert queue.messages == []


def test_all_invalid_writes_nothing():
    _, blobs, queue = send(["junk"])
    assert blobs.data == {} and queue.messages == []


def test_too_many_events_raises_limit_error():
    with pytest.raises(BatchTooLarge):
        send([good(i) for i in range(6)])


def test_too_many_bytes_raises_limit_error():
    with pytest.raises(BatchTooLarge):
        send([good(1)], limits=IngestLimits(max_events=5, max_bytes=20))


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"batch": 3}', b"{}"])
def test_malformed_envelope_is_invalid_batch(raw):
    with pytest.raises(InvalidBatch):
        accept_batch("p1", raw, FakeBlobs(), FakeQueue(), LIMITS)


def test_blob_failure_is_a_distinct_error_and_nothing_is_queued():
    queue = FakeQueue()
    with pytest.raises(StorageUnavailable):
        send([good()], blobs=FakeBlobs(fail=True), queue=queue)
    assert queue.messages == []


def test_enqueue_failure_still_succeeds_because_blob_is_durable():
    result, blobs, _ = send([good()], queue=FakeQueue(fail=True))
    assert result.successes and len(blobs.data) == 1
