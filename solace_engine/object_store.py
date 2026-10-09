"""Per-project content-addressed objects with durable atomic publication."""

import fcntl
import hashlib
import os
import re
import tempfile
from pathlib import Path

from .chunking import iter_file_chunks
from .errors import CorruptObject


_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ObjectStore:
    """Store immutable bytes beneath one project's store directory."""

    def __init__(self, store_dir: Path):
        self.store_dir = Path(store_dir)

    def _object_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or _DIGEST_PATTERN.fullmatch(digest) is None:
            raise ValueError("object digest must be 64 lowercase hexadecimal characters")
        return self.store_dir / "objects" / digest[:2] / digest

    def read_object(self, digest: str) -> bytes:
        """Return an object only if its bytes match the requested digest."""
        path = self._object_path(digest)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise CorruptObject(f"object {digest} is missing or unreadable") from error
        if hashlib.sha256(data).hexdigest() != digest:
            raise CorruptObject(f"object {digest} failed digest verification")
        return data

    def verify_object(self, digest: str) -> bool:
        """Check whether the named object exists with intact content."""
        self._object_path(digest)
        try:
            self.read_object(digest)
        except CorruptObject:
            return False
        return True

    def put_bytes(self, data: bytes) -> str:
        """Publish bytes atomically, reusing a verified existing object."""
        digest = hashlib.sha256(data).hexdigest()
        destination = self._object_path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            # Writers sharing this project serialize publication within the shard.
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            if destination.exists():
                self.read_object(digest)
                return digest
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=f".{digest}.", suffix=".tmp",
                    dir=destination.parent, delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())

                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    raise CorruptObject(f"temporary object {digest} failed digest verification")
                os.replace(temporary, destination)
                os.fsync(directory_fd)
                return digest
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        finally:
            os.close(directory_fd)

    def put_file(self, path: Path) -> tuple[str, int, tuple[str, ...]]:
        """Store file chunks and return whole-file digest, size, and object names."""
        whole = hashlib.sha256()
        size = 0
        chunks = []
        for chunk in iter_file_chunks(Path(path)):
            whole.update(chunk)
            size += len(chunk)
            chunks.append(self.put_bytes(chunk))
        if not chunks:
            chunks.append(self.put_bytes(b""))
        return whole.hexdigest(), size, tuple(chunks)
