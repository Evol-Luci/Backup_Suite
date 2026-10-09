"""Deterministic, bounded FastCDC chunking for checkpoint file content."""

import hashlib
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from .errors import RepositoryError


# Versioned seed and index define a fixed gear table on every Python runtime.
_GEAR = tuple(
    int.from_bytes(
        hashlib.sha256(b"solace-fastcdc-v1:" + bytes((index,))).digest()[:8],
        "big",
    )
    for index in range(256)
)
_READ_SIZE = 65536
_WHOLE_FILE_LIMIT = 1048576


def _validate_sizes(min_size: int, average_size: int, max_size: int) -> None:
    if not 0 < min_size <= average_size <= max_size:
        raise ValueError("chunk sizes must satisfy 0 < min_size <= average_size <= max_size")


def _iter_chunks(
    blocks: Iterable[bytes], min_size: int, average_size: int, max_size: int
) -> Iterator[bytes]:
    # A stricter mask before the target and a looser one after it pull cuts
    # toward average_size; max_size always caps the buffer.
    bits = max(1, average_size.bit_length() - 1)
    early_mask = (1 << (bits + 1)) - 1
    late_mask = (1 << max(1, bits - 1)) - 1
    buffer = bytearray()
    fingerprint = 0

    for block in blocks:
        for byte in block:
            buffer.append(byte)
            size = len(buffer)
            if size < min_size:
                continue
            fingerprint = ((fingerprint << 1) + _GEAR[byte]) & 0xFFFFFFFFFFFFFFFF
            mask = early_mask if size < average_size else late_mask
            if size == max_size or fingerprint & mask == 0:
                yield bytes(buffer)
                buffer.clear()
                fingerprint = 0

    if buffer:
        yield bytes(buffer)


def iter_file_chunks(
    path: Path,
    min_size: int = 65536,
    average_size: int = 262144,
    max_size: int = 1048576,
) -> Iterator[bytes]:
    """Yield bounded chunks, rejecting observed changes before completion.

    Callers must exhaust the iterator before publishing its result. Metadata
    checks detect ordinary concurrent writes; this is not a filesystem snapshot
    or a defense against a privileged writer falsifying filesystem metadata.
    """
    _validate_sizes(min_size, average_size, max_size)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        consumed = 0
        if before.st_size <= _WHOLE_FILE_LIMIT:
            data = stream.read(_WHOLE_FILE_LIMIT + 1)
            consumed = len(data)
            if data:
                yield data
        else:
            for chunk in _iter_chunks(iter(lambda: stream.read(_READ_SIZE), b""),
                                      min_size, average_size, max_size):
                consumed += len(chunk)
                yield chunk
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns) or consumed != before.st_size:
            raise RepositoryError(f"file changed during checkpoint capture: {path}; retry when writes stop")


def split_bytes(
    data: bytes,
    min_size: int = 65536,
    average_size: int = 262144,
    max_size: int = 1048576,
) -> list[bytes]:
    """Split in-memory data with the same boundaries as iter_file_chunks."""
    _validate_sizes(min_size, average_size, max_size)
    if not data:
        return []
    if len(data) <= _WHOLE_FILE_LIMIT:
        return [data]
    return list(_iter_chunks((data,), min_size, average_size, max_size))
