from __future__ import annotations

from datetime import datetime

import boto3
import pytest
from moto import mock_aws

from traceloom.adapters.full.s3_blob import S3BlobStore
from traceloom.ports import BlobNotFound, BlobStoreError


@pytest.fixture
def blobs(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        store = S3BlobStore("traceloom-test", access_key="a", secret_key="b", region="us-east-1")
        store.ensure_bucket()
        yield store


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


def test_move_and_delete_are_idempotent_where_the_port_says_so(blobs):
    blobs.put("pending/p1/a.json", b"x")
    blobs.move("pending/p1/a.json", "processed/p1/a.json")
    assert blobs.get("processed/p1/a.json") == b"x"
    with pytest.raises(BlobNotFound):
        blobs.get("pending/p1/a.json")
    blobs.delete("processed/p1/a.json")
    blobs.delete("processed/p1/a.json")
    with pytest.raises(BlobNotFound):
        blobs.get("processed/p1/a.json")


def test_list_by_prefix_with_aware_modified_time(blobs):
    blobs.put("pending/p1/a.json", b"1")
    blobs.put("pending/p2/b.json", b"2")
    blobs.put("processed/p1/c.json", b"3")
    listed = dict(blobs.list("pending/"))
    assert set(listed) == {"pending/p1/a.json", "pending/p2/b.json"}
    assert all(isinstance(t, datetime) and t.tzinfo for t in listed.values())
    assert list(blobs.list("nothing/")) == []


def test_list_pages_through_more_than_one_thousand_keys(blobs):
    for i in range(1005):
        blobs.put(f"pending/p/{i:04d}.json", b"x")
    assert len(list(blobs.list("pending/"))) == 1005


@pytest.mark.parametrize("key", ["../escape", "/abs", "", "a/../b"])
def test_bad_keys_are_rejected(blobs, key):
    with pytest.raises(BlobStoreError):
        blobs.put(key, b"x")


def test_ensure_bucket_is_idempotent_and_ping_works(blobs):
    blobs.ensure_bucket()
    assert blobs.ping() is True


def test_unreachable_store_is_a_blob_store_error(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        store = S3BlobStore("never-created", region="us-east-1")  # bucket does not exist
        with pytest.raises(BlobStoreError):
            store.put("pending/a.json", b"x")
        with pytest.raises(BlobStoreError):
            list(store.list("pending/"))
        assert boto3  # imported lazily by the store, eagerly here for moto
