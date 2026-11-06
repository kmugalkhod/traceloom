"""BlobStore on S3 or any S3-compatible service (MinIO). boto3 is imported only when used."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from traceloom.ports import BlobNotFound, BlobStoreError

_MISSING = {"NoSuchKey", "404", "NotFound"}


class S3BlobStore:
    def __init__(
        self,
        bucket: str,
        client: Any = None,
        *,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        if client is None:
            import boto3  # full mode only

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                aws_access_key_id=access_key or None,
                aws_secret_access_key=secret_key or None,
                region_name=region,
            )
        self.client = client

    @staticmethod
    def _code(exc: Exception) -> str:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))

    def _fail(self, action: str, key: str, exc: Exception) -> BlobStoreError:
        return BlobStoreError(f"cannot {action} {key}: {exc}")

    def ensure_bucket(self) -> None:
        """Create the bucket when it does not exist (dev and compose convenience)."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            if self._code(exc) not in _MISSING:
                raise BlobStoreError(f"cannot reach bucket {self.bucket}: {exc}") from exc
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception as create_exc:
                raise BlobStoreError(
                    f"cannot create bucket {self.bucket}: {create_exc}"
                ) from create_exc

    def ping(self) -> bool:
        self.client.head_bucket(Bucket=self.bucket)
        return True

    def put(self, key: str, data: bytes) -> None:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise BlobStoreError(f"invalid blob key {key!r}")
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except Exception as exc:
            raise self._fail("write", key, exc) from exc

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            if self._code(exc) in _MISSING:
                raise BlobNotFound(key) from exc
            raise self._fail("read", key, exc) from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)  # idempotent on S3
        except Exception as exc:
            raise self._fail("delete", key, exc) from exc

    def move(self, src: str, dst: str) -> None:
        try:
            self.client.copy_object(
                Bucket=self.bucket, Key=dst, CopySource={"Bucket": self.bucket, "Key": src}
            )
        except Exception as exc:
            if self._code(exc) in _MISSING:
                raise BlobNotFound(src) from exc
            raise self._fail("move", src, exc) from exc
        self.delete(src)

    def list(self, prefix: str) -> Iterator[tuple[str, datetime]]:
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    modified = item["LastModified"]
                    yield item["Key"], modified if modified.tzinfo else modified.replace(tzinfo=UTC)
        except Exception as exc:
            raise self._fail("list", prefix, exc) from exc
