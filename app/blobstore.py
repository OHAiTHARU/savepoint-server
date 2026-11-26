"""Content-addressed blob store.

Blobs live at <root>/<h[0:2]>/<h[2:4]>/<h>.zst. The hash is always SHA-256 of
the uncompressed content. Writes go to a temp file, are fsynced, then renamed
into place, so a crash never leaves a partial blob under its final name.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator

import zstandard

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1024 * 1024
ZSTD_LEVEL = 10


class HashMismatch(Exception):
    def __init__(self, expected: str, actual: str):
        super().__init__(f"expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual


def is_valid_hash(value: str) -> bool:
    return bool(HASH_RE.match(value))


class BlobWriter:
    """Incremental writer: feed chunks, then commit() with the expected hash."""

    def __init__(self, store: "BlobStore"):
        self.store = store
        fd, name = tempfile.mkstemp(prefix="upload-", suffix=".zst.tmp", dir=store.tmp_dir)
        self._file = os.fdopen(fd, "wb")
        self._tmp = Path(name)
        self._hasher = hashlib.sha256()
        self._compressor = zstandard.ZstdCompressor(level=ZSTD_LEVEL).compressobj()
        self.size = 0
        self._done = False

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._hasher.update(chunk)
        self.size += len(chunk)
        self._file.write(self._compressor.compress(chunk))

    def commit(self, expected_hash: str) -> tuple[int, int]:
        """Verify and move into place. Returns (size, stored_size)."""
        try:
            self._file.write(self._compressor.flush())
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            actual = self._hasher.hexdigest()
            if actual != expected_hash:
                raise HashMismatch(expected_hash, actual)
            stored_size = self._tmp.stat().st_size
            final = self.store.path_for(expected_hash)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self._tmp, final)
            _fsync_dir(final.parent)
            self._done = True
            return self.size, stored_size
        finally:
            if not self._done:
                self.abort()

    def abort(self) -> None:
        try:
            if not self._file.closed:
                self._file.close()
        finally:
            self._tmp.unlink(missing_ok=True)
            self._done = True


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class BlobStore:
    def __init__(self, root: Path, tmp_dir: Path):
        self.root = root
        self.tmp_dir = tmp_dir
        root.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, h: str) -> Path:
        return self.root / h[0:2] / h[2:4] / f"{h}.zst"

    def exists(self, h: str) -> bool:
        return self.path_for(h).exists()

    def writer(self) -> BlobWriter:
        return BlobWriter(self)

    def put_bytes(self, data: bytes, expected_hash: str | None = None) -> tuple[str, int, int]:
        h = expected_hash or hashlib.sha256(data).hexdigest()
        w = self.writer()
        w.write(data)
        size, stored = w.commit(h)
        return h, size, stored

    def open_stream(self, h: str) -> Iterator[bytes]:
        """Yields decompressed content in chunks."""
        path = self.path_for(h)
        with path.open("rb") as fh:
            reader = zstandard.ZstdDecompressor().stream_reader(fh)
            while True:
                chunk = reader.read(CHUNK)
                if not chunk:
                    break
                yield chunk

    def copy_to(self, h: str, out: BinaryIO) -> None:
        for chunk in self.open_stream(h):
            out.write(chunk)

    def read_bytes(self, h: str) -> bytes:
        return b"".join(self.open_stream(h))

    def delete(self, h: str) -> None:
        path = self.path_for(h)
        path.unlink(missing_ok=True)
        for parent in (path.parent, path.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break

    def iter_hashes(self) -> Iterator[str]:
        for p in self.root.glob("*/*/*.zst"):
            name = p.name[:-4]
            if is_valid_hash(name):
                yield name

    def clean_tmp(self, older_than_seconds: float) -> int:
        import time

        cutoff = time.time() - older_than_seconds
        removed = 0
        for p in self.tmp_dir.glob("upload-*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        return removed
