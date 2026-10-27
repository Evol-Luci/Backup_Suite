"""Conservative immutable manifest merging; no publication or plan persistence.

Only genuinely divergent regular-file content needs a worker. Admission is
nonblocking: service threads never queue behind another project's calculation.
"""
from dataclasses import replace
from difflib import SequenceMatcher
from multiprocessing.connection import Connection
from pathlib import Path
import os
import selectors
import struct
import subprocess
import sys
import threading
import time
from typing import Callable

from solace_engine.errors import RepositoryError
from solace_engine.models import FileEntry
from solace_engine.repository import ProjectRepository
from .errors import CoreError
from .integration_types import MergeConflict, MergeOutcome
from .workspace_trees import validate_workspace_links

_MAX_BYTES = 1024 * 1024
_MAX_LINES = 50000
_WORKER_TIMEOUT = 2.0
_WORKER_SLOTS = threading.BoundedSemaphore(2)
# A merge can include all three bounded inputs, plus its single-byte tag.
_MAX_MESSAGE = 3 * _MAX_BYTES + 1
_TEXT_REASONS = frozenset({'non_text', 'text_too_large', 'text_too_many_lines', 'overlapping_text'})


def scalar_merge(base, current, incoming):
    if current == incoming or incoming == base:
        return current, False
    if current == base:
        return incoming, False
    return None, True


def _text_lines(data):
    if len(data) > _MAX_BYTES:
        return None, 'text_too_large'
    if b'\0' in data:
        return None, 'non_text'
    try:
        data.decode('utf-8', 'strict')
    except UnicodeDecodeError:
        return None, 'non_text'
    lines = data.splitlines(keepends=True)
    if len(lines) > _MAX_LINES:
        return None, 'text_too_many_lines'
    return lines, None


def _hunks(base, changed):
    return [(start, end, changed[new_start:new_end])
            for operation, start, end, new_start, new_end
            in SequenceMatcher(None, base, changed, autojunk=False).get_opcodes()
            if operation != 'equal']


def _touches(left, right):
    a, b, _ = left
    c, d, _ = right
    if a == b:
        return c <= a <= d
    if c == d:
        return a <= c <= b
    return max(a, c) < min(b, d)


def merge_text(base: bytes, current: bytes, incoming: bytes) -> tuple[bytes | None, str | None]:
    """Merge disjoint base-relative byte lines, preserving all original endings.

    Equality needs no text interpretation. Callers must isolate difficult diffs;
    this pure function deliberately owns neither processes nor mutable storage.
    """
    selected, conflict = scalar_merge(base, current, incoming)
    if not conflict:
        return selected, None
    versions = []
    for data in (base, current, incoming):
        lines, reason = _text_lines(data)
        if reason:
            return None, reason
        versions.append(lines)
    base_lines, current_lines, incoming_lines = versions
    left = _hunks(base_lines, current_lines)
    right = _hunks(base_lines, incoming_lines)
    accepted = list(left)
    # Sorted, non-overlapping hunk lists allow a linear overlap scan. Identical
    # replacements coalesce; insertion boundaries are deliberately conservative.
    index = 0
    for hunk in right:
        while index < len(left) and left[index][1] < hunk[0]:
            index += 1
        scan = index
        duplicate = False
        while scan < len(left) and left[scan][0] <= hunk[1]:
            if left[scan] == hunk:
                duplicate = True
            elif _touches(left[scan], hunk):
                return None, 'overlapping_text'
            scan += 1
        if not duplicate:
            accepted.append(hunk)
    merged = list(base_lines)
    for start, end, replacement in sorted(accepted, key=lambda h: (h[0], h[1]), reverse=True):
        merged[start:end] = replacement
    return b''.join(merged), None


def _text_worker(connection, base, current, incoming):
    try:
        merged, reason = merge_text(base, current, incoming)
        connection.send_bytes(b'B' + merged if reason is None else b'R' + reason.encode('ascii'))
    finally:
        connection.close()


def _stdio_worker(worker):
    """Exec entrypoint: bootstrap contains no input bytes or pickled state."""
    request = Connection(os.dup(0), readable=True, writable=False)
    response = Connection(os.dup(1), readable=False, writable=True)
    try:
        values = tuple(request.recv_bytes(maxlength=_MAX_BYTES) for _ in range(3))
        worker(response, *values)
    finally:
        request.close()
        response.close()


def _worker_command():
    # Pin only this package location. Inheriting/pickling sys.path, argv or other
    # multiprocessing preparation metadata can itself fill a bootstrap pipe.
    root = str(Path(__file__).resolve().parent.parent)
    code = (f'import sys; sys.path.insert(0, {root!r}); '
            'from solace_core.integration_merge import _stdio_worker, _text_worker; '
            '_stdio_worker(_text_worker)')
    return [sys.executable, '-c', code]


def _start_worker():
    # Popen returns after exec, before Python imports/sitecustomize run. All
    # potentially large writes happen explicitly below, never during startup.
    return subprocess.Popen(_worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)


def _send_worker_inputs(connection, values, deadline):
    """Deliver bounded frames without waiting on a stalled interpreter/reader."""
    fd = connection.fileno()
    os.set_blocking(fd, False)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        for value in values:
            for part in (struct.pack('!i', len(value)), value):
                pending = memoryview(part)
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        return 'merge_timeout'
                    try:
                        written = os.write(fd, pending[:65536])
                    except BlockingIOError:
                        continue
                    pending = pending[written:]
    return None


def _receive_worker(connection, deadline):
    """Read bounded Connection framing without a blocking recv/unpickle.

    poll()+recv_bytes() can block forever after a partial header or payload.
    Nonblocking raw reads keep the same deadline over the entire response.
    """
    fd = connection.fileno()
    os.set_blocking(fd, False)
    buffer = bytearray()
    expected = None
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return None, 'merge_timeout'
            try:
                block = os.read(fd, min(65536, _MAX_MESSAGE + 5 - len(buffer)))
            except BlockingIOError:
                continue
            if not block:
                return None, 'merge_worker_malformed' if buffer else 'merge_worker_failed'
            buffer.extend(block)
            if expected is None and len(buffer) >= 4:
                expected = struct.unpack('!i', buffer[:4])[0]
                if not 1 <= expected <= _MAX_MESSAGE:
                    return None, 'merge_worker_malformed'
            if expected is not None and len(buffer) >= expected + 4:
                if len(buffer) != expected + 4:
                    return None, 'merge_worker_malformed'
                payload = bytes(buffer[4:])
                if payload[:1] == b'B':
                    merged = payload[1:]
                    try:
                        merged.decode('utf-8', 'strict')
                    except UnicodeDecodeError:
                        return None, 'merge_worker_malformed'
                    if b'\0' in merged:
                        return None, 'merge_worker_malformed'
                    return merged, None
                if payload[:1] == b'R':
                    try:
                        reason = payload[1:].decode('ascii')
                    except UnicodeDecodeError:
                        pass
                    else:
                        if reason in _TEXT_REASONS:
                            return None, reason
                return None, 'merge_worker_malformed'


def _bounded_merge(base, current, incoming):
    # Bound values before sending them to the child, even if metadata lied.
    if any(len(data) > _MAX_BYTES for data in (base, current, incoming)):
        return None, 'text_too_large'
    if not _WORKER_SLOTS.acquire(blocking=False):
        return None, 'merge_worker_busy'
    process = None
    try:
        deadline = time.monotonic() + _WORKER_TIMEOUT
        process = _start_worker()
        reason = _send_worker_inputs(process.stdin, (base, current, incoming), deadline)
        if reason:
            return None, reason
        process.stdin.close()
        result = _receive_worker(process.stdout, deadline)
        if result[1] in {'merge_timeout', 'merge_worker_malformed', 'merge_worker_failed'}:
            return result
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return None, 'merge_timeout'
        if process.returncode != 0:
            return None, 'merge_worker_failed'
        # The child owns the only remaining writer. After its clean exit the
        # first frame must be followed by EOF, never an ignored second message.
        if os.read(process.stdout.fileno(), 1):
            return None, 'merge_worker_malformed'
        return result
    except (OSError, ValueError, EOFError, RuntimeError):
        return None, 'merge_worker_failed'
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdin.close()
            process.stdout.close()
        _WORKER_SLOTS.release()


def _entry_key(entry):
    if entry is None:
        return None
    return entry.kind, entry.mode, entry.size, entry.file_digest


def _content_key(entry):
    return None if entry is None else (entry.size, entry.file_digest)


def _merge_entry(path, base, current, incoming, read_bytes, write_bytes):
    selected, conflict = scalar_merge(_entry_key(base), _entry_key(current), _entry_key(incoming))
    if not conflict:
        return (current if selected == _entry_key(current) else incoming), None
    if current is None or incoming is None:
        return current, 'delete_modify'
    if current.kind != incoming.kind:
        return current, 'incompatible_kind'
    if (base is not None and base.kind != current.kind
            and _content_key(current) != _content_key(incoming)):
        return current, 'incompatible_kind'
    mode, conflict = scalar_merge(base.mode if base else None, current.mode, incoming.mode)
    if conflict:
        return current, 'incompatible_mode'
    content, conflict = scalar_merge(_content_key(base), _content_key(current), _content_key(incoming))
    if not conflict:
        donor = current if content == _content_key(current) else incoming
        return (donor if donor.mode == mode else replace(donor, mode=mode)), None
    if current.kind == 'symlink':
        return current, 'link_target'
    if base is None:
        return current, 'add_add'
    if any(e.size > _MAX_BYTES for e in (base, current, incoming)):
        return current, 'text_too_large'
    merged, reason = _bounded_merge(*(read_bytes(e) for e in (base, current, incoming)))
    if reason:
        return current, reason
    return write_bytes(path, merged, mode), None


def _invalid_manifest(error):
    raise CoreError('INTEGRATION_MANIFEST_INVALID', 'The integration manifest is not a safe complete tree.',
                    'Preserve all input versions and resolve the invalid paths or symlinks.') from error


def _entry_map(entries):
    try:
        ProjectRepository._validate_restore_entries(entries)
        result = {e.path: e for e in entries}
        if len(result) != len(entries):
            raise ValueError('duplicate manifest path')
        return result
    except (RepositoryError, ValueError) as error:
        _invalid_manifest(error)


def merge_entries(base: tuple[FileEntry, ...], current: tuple[FileEntry, ...],
                  incoming: tuple[FileEntry, ...], *,
                  read_bytes: Callable[[FileEntry], bytes],
                  write_bytes: Callable[[str, bytes, int], FileEntry]) -> MergeOutcome:
    """Merge immutable entry attributes, retaining current data at conflicts.

    Prefix collisions restore their current subtree. If independent changes
    produce an unsafe link graph, retain the complete validated current tree
    and explicitly conflict every reverted change as well as the unsafe links.
    Storage callbacks handle immutable objects only, never filesystem publication.
    """
    b, c, i = (_entry_map(entries) for entries in (base, current, incoming))
    candidate = {}
    conflicts = {}

    def record(path, reason):
        conflicts[path] = MergeConflict(path, reason, b.get(path), c.get(path), i.get(path))

    for path in sorted(b.keys() | c.keys() | i.keys()):
        selected, reason = _merge_entry(path, b.get(path), c.get(path), i.get(path), read_bytes, write_bytes)
        if selected is not None:
            candidate[path] = selected
        if reason:
            record(path, reason)

    roots = set()
    for path in sorted(candidate):
        parts = path.split('/')
        for index in range(1, len(parts)):
            ancestor = '/'.join(parts[:index])
            if ancestor in candidate:
                roots.add(ancestor)
                break
    for root in sorted(roots, key=lambda p: (p.count('/'), p)):
        prefix = root + '/'
        affected = {p for p in b.keys() | c.keys() | i.keys() | candidate.keys()
                    if p == root or p.startswith(prefix)}
        for path in sorted(affected):
            record(path, 'path_prefix')
            candidate.pop(path, None)
            if path in c:
                candidate[path] = c[path]

    entries = tuple(candidate[p] for p in sorted(candidate))
    targets = {e.path: read_bytes(e) for e in entries if e.kind == 'symlink'}
    try:
        validate_workspace_links(entries, targets)
    except CoreError:
        unsafe = []
        for entry in entries:
            if entry.kind == 'symlink':
                try:
                    validate_workspace_links((entry,), targets)
                except CoreError:
                    unsafe.append(entry.path)
        fallback = tuple(c[p] for p in sorted(c))
        try:
            validate_workspace_links(fallback, {e.path: read_bytes(e) for e in fallback if e.kind == 'symlink'})
        except (CoreError, RepositoryError) as error:
            _invalid_manifest(error)
        for path in sorted(set(unsafe) | {p for p in candidate.keys() | c.keys()
                                         if _entry_key(candidate.get(p)) != _entry_key(c.get(p))}):
            record(path, 'unsafe_link')
        entries = fallback
    except RepositoryError as error:
        _invalid_manifest(error)
    return MergeOutcome(entries, tuple(conflicts[p] for p in sorted(conflicts)))
