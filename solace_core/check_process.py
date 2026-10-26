"""Bounded capture of approved local commands (not a security sandbox)."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
from typing import Callable

from .check_staging import StagedCheck
from .check_profiles import ENVIRONMENT_TEMPLATE


def process_start_time(pid: int) -> int | None:
    """Linux start ticks, including zombies; None when the identity is gone."""
    try:
        return int((Path('/proc')/str(pid)/'stat').read_text().rsplit(') ', 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


@dataclass(frozen=True)
class StepResult:
    status: str
    exit_code: int | None
    signal: int | None
    timed_out: bool
    cancelled: bool
    started_at: str
    finished_at: str
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    stdout_sha256: str
    stderr_sha256: str
    stdout_truncated: bool
    stderr_truncated: bool
    launch_error: str | None = None
    output_complete: bool = True


class CheckExecutor:
    TERM_GRACE_SECONDS = .25
    DRAIN_GRACE_SECONDS = 1.0

    def __init__(self, environment=None):
        # Callers supply the approved effective profile template, never os.environ.
        self.environment = dict(ENVIRONMENT_TEMPLATE if environment is None else environment)

    def execute(self, step: dict, stage: StagedCheck, cancel: threading.Event,
                on_start: Callable[[int, int], None]) -> StepResult:
        started = datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + step['timeout_seconds']
        cap = step['output_limit_bytes']
        captures = [bytearray(), bytearray()]
        counts = [0, 0]
        hashes = [hashlib.sha256(), hashlib.sha256()]
        timed_out = cancelled = False
        complete = True
        error = None
        rc = None
        proc = None
        identity = None
        selector = selectors.DefaultSelector()

        def signal_group(sig):
            # Do not reap the leader until group cleanup is over: its zombie
            # reserves the PID/PGID even if it exits before its descendants.
            if identity is not None and process_start_time(proc.pid) == identity:
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    pass

        try:
            if cancel.is_set():
                cancelled = True
            else:
                env = dict(self.environment)
                env.update(HOME=str(stage.home), TMPDIR=str(stage.tmpdir))
                try:
                    proc = subprocess.Popen(step['argv'], cwd=stage.root, env=env,
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, start_new_session=True)
                except (OSError, ValueError) as exc:
                    error = str(exc)
                if proc is not None:
                    identity = process_start_time(proc.pid)
                    if identity is None:
                        raise RuntimeError('Cannot record check process start identity.')
                    for index, stream in enumerate((proc.stdout, proc.stderr)):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, selectors.EVENT_READ, index)
                    on_start(proc.pid, identity)
                    stopping = None
                    killed = None
                    while True:
                        now = time.monotonic()
                        exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
                        if stopping is None:
                            if cancel.is_set():
                                cancelled = True
                            elif now >= deadline:
                                timed_out = True
                            if cancelled or timed_out:
                                signal_group(signal.SIGTERM)
                                stopping = now
                            elif exited and not selector.get_map():
                                # Also terminate descendants that closed inherited pipes.
                                signal_group(signal.SIGTERM)
                                stopping = now
                        if stopping is not None and killed is None and now >= stopping + self.TERM_GRACE_SECONDS:
                            signal_group(signal.SIGKILL)
                            killed = now
                        if killed is not None and exited and not selector.get_map():
                            break
                        if killed is not None and now >= killed + self.DRAIN_GRACE_SECONDS:
                            # A command can deliberately escape its group. Never hang
                            # forever on its inherited pipe or claim a full digest.
                            complete = not selector.get_map()
                            break
                        for key, _ in selector.select(.02):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                                continue
                            index = key.data
                            counts[index] += len(data)
                            hashes[index].update(data)
                            captures[index].extend(data[:max(0, cap-len(captures[index]))])
                    rc = proc.wait()
        finally:
            if proc is not None:
                if proc.returncode is None:
                    signal_group(signal.SIGKILL)
                    proc.wait()
                for stream in (proc.stdout, proc.stderr):
                    stream.close()
            selector.close()
        status = ('cancelled' if cancelled else 'timed_out' if timed_out else
                  'launch_error' if error else 'passed' if rc == 0 and complete else 'failed')
        return StepResult(status, rc if rc is not None and rc >= 0 else None,
                          -rc if rc is not None and rc < 0 else None,
                          timed_out, cancelled, started, datetime.now(timezone.utc).isoformat(),
                          bytes(captures[0]), bytes(captures[1]), *counts,
                          hashes[0].hexdigest(), hashes[1].hexdigest(),
                          counts[0] > cap, counts[1] > cap, error, complete)
