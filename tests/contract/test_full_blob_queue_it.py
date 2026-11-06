"""S3 and Redis adapters against real MinIO and Redis. Runs only with TRACELOOM_IT=1
(docker compose up -d minio redis). Their fake-backed unit tests are in tests/unit."""

from __future__ import annotations

import os
import threading
import uuid

import pytest

pytestmark = pytest.mark.it


@pytest.fixture
def blobs():
    from traceloom.adapters.full.s3_blob import S3BlobStore

    store = S3BlobStore(
        f"tl-it-{uuid.uuid4().hex[:8]}",
        endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000"),
        access_key=os.environ.get("S3_ACCESS_KEY", "traceloom"),
        secret_key=os.environ.get("S3_SECRET_KEY", "traceloom-minio"),
    )
    store.ensure_bucket()
    yield store


def test_s3_put_get_list_move_delete(blobs):
    from traceloom.ports import BlobNotFound

    blobs.put("pending/p/a.json", b"1")
    assert blobs.get("pending/p/a.json") == b"1"
    assert [k for k, _ in blobs.list("pending/")] == ["pending/p/a.json"]
    blobs.move("pending/p/a.json", "processed/p/a.json")
    with pytest.raises(BlobNotFound):
        blobs.get("pending/p/a.json")
    blobs.delete("processed/p/a.json")


def test_redis_queue_delivers_messages():
    from traceloom.adapters.full.redis_queue import RedisQueue

    queue = RedisQueue(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        queue_name=f"tl-it-{uuid.uuid4().hex[:8]}",
    )
    got, done = [], threading.Event()
    queue.start(lambda m: (got.append(m), done.set()))
    try:
        queue.enqueue({"project_id": "p", "blob_key": "k"})
        assert done.wait(15) and got == [{"project_id": "p", "blob_key": "k"}]
        assert queue.ping()
    finally:
        queue.stop()
