"""Bounded logical prompt expiry, coordinated separately for each project."""
import logging
import threading
from .errors import classify
from .prompts import PromptStore
from .schema import schema_version

log=logging.getLogger(__name__)

class PromptMaintenance:
    def __init__(self,service,*,interval_seconds=60.0,batch_size=100):
        if interval_seconds<=0 or type(batch_size) is not int or batch_size<1:
            raise ValueError('maintenance interval and batch size must be positive')
        self.service=service;self.interval_seconds=interval_seconds;self.batch_size=batch_size
        self.stop=threading.Event();self.thread=None;self.failures={}

    def start(self):
        if self.thread is not None: return
        self.run_once()
        self.thread=threading.Thread(target=self._run,name='solace-prompt-maintenance',daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.wait(self.interval_seconds): self.run_once()

    def run_once(self):
        if self.stop.is_set() or self.service._closed: return
        try:
            records=self.service.registry.list()
            self.failures.pop('_registry',None)
        except Exception as error:
            code=classify(error).code
            if self.failures.get('_registry')!=code:
                log.warning('prompt maintenance registry exception=%s',type(error).__name__)
            self.failures['_registry']=code
            return
        for record in records:
            if self.stop.is_set() or self.service._closed: return
            coordinator=self.service.coordinator(record['id'])
            if not coordinator.lock.acquire(blocking=False): continue
            try:
                catalog=self.service.catalog(record['id'])
                if schema_version(catalog)==2: PromptStore(catalog).purge_expired(limit=self.batch_size)
                self.failures.pop(record['id'],None)
            except Exception as error:
                code=classify(error).code
                if self.failures.get(record['id'])!=code:
                    log.warning('prompt maintenance project=%s exception=%s',record['id'],type(error).__name__)
                self.failures[record['id']]=code
            finally: coordinator.lock.release()

    def close(self):
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread(): self.thread.join()
