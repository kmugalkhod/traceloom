"""BlobStore on a local folder (TRACELOOM_BLOB_DIR)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from traceloom.ports import BlobNotFound, BlobStoreError


class LocalBlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not key or path == self.root or self.root not in path.parents:
            raise BlobStoreError(f"invalid blob key {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename, so a reader (or a crash) never sees a partial blob.
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise BlobStoreError(f"cannot write {key}: {exc}") from exc

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFound(key) from exc
        except OSError as exc:
            raise BlobStoreError(f"cannot read {key}: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise BlobStoreError(f"cannot delete {key}: {exc}") from exc

    def move(self, src: str, dst: str) -> None:
        source, target = self._path(src), self._path(dst)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        except FileNotFoundError as exc:
            raise BlobNotFound(src) from exc
        except OSError as exc:
            raise BlobStoreError(f"cannot move {src}: {exc}") from exc

    def list(self, prefix: str) -> Iterator[tuple[str, datetime]]:
        base = self.root / prefix
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix != ".tmp":
                modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                yield path.relative_to(self.root).as_posix(), modified
