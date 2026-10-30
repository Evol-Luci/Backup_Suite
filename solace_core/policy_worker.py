"""Independent wakeable reconciliation thread, never a project FIFO job."""
import logging
import threading
from .errors import classify
from .policy_queue import PolicyQueue

log = logging.getLogger(__name__)


class PolicyWorker:
    def __init__(self, service, interval_seconds=1.0):
        self.service = service
        self.interval_seconds = interval_seconds
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        self.failures = {}

    def start(self):
        if self.thread is not None: return
        self.thread = threading.Thread(target=self._run, name='solace-policy', daemon=True)
        self.thread.start()

    def wake(self):
        self.wakeup.set()

    def _run(self):
        while not self.stop.is_set():
            self.wakeup.clear()
            self.run_once()
            self.wakeup.wait(self.interval_seconds)

    def run_once(self):
        if self.stop.is_set() or self.service._closed: return
        try:
            records = self.service.registry.list()
            self.failures.pop('_registry', None)
        except Exception as error:
            self.failures['_registry'] = classify(error).code
            return
        for record in records:
            if self.stop.is_set() or self.service._closed: return
            try:
                PolicyQueue.tick(self.service, record['id'])
                self.failures.pop(record['id'], None)
            except Exception as error:
                self.failures[record['id']] = classify(error).code
                log.debug('policy reconciliation unavailable project=%s exception=%s', record['id'], type(error).__name__)

    def close(self):
        self.stop.set()
        self.wakeup.set()
        if self.thread and self.thread is not threading.current_thread(): self.thread.join()
