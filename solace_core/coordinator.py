"""Per-project FIFO scheduling on a bounded shared worker pool."""
from collections import deque
import threading


class ProjectCoordinator:
    def __init__(self, executor):
        self.lock = threading.RLock()
        self.executor = executor
        self.pending = deque()
        self.ids = set()
        self.running = False

    @property
    def busy(self):
        with self.lock:
            return self.running or bool(self.pending)

    def submit(self, operation_id, function):
        with self.lock:
            if operation_id in self.ids:
                return
            self.ids.add(operation_id)
            self.pending.append((operation_id, function))
            if not self.running:
                self.running = True
                self.executor.submit(self._run)

    def _run(self):
        while True:
            with self.lock:
                if not self.pending:
                    self.running = False
                    return
                operation_id, function = self.pending.popleft()
            try:
                function()
            finally:
                with self.lock:
                    self.ids.discard(operation_id)
