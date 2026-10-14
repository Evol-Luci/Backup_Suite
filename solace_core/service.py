"""Single local authority for registration, operation scheduling, and recovery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import fcntl
import logging
import os
import threading
from .catalog import ProjectCatalog
from .coordinator import ProjectCoordinator
from .errors import CoreError, classify
from .protocol import validate_request, response

log = logging.getLogger(__name__)


class CoreService:
    def __init__(self, registry, *, workers=8):
        self.registry = registry
        self._closed = False
        self._guard = threading.RLock()
        self._catalogs, self._coordinators = {}, {}
        self._lock_fd = os.open(registry.path.parent / 'core.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self._lock_fd)
            raise CoreError('PROJECT_BUSY', 'Another core owns this user registry.', 'Connect to the existing core socket.') from error
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='solace-operation')
        self.recovery = []
        try:
            self.recovery.extend(registry.reconcile_intents())
            for record in registry.list():
                try:
                    catalog = self.catalog(record['id'])
                    ids = catalog.recover_interrupted()
                    if ids: self.recovery.append(dict(project=record['id'], operations=ids, state='interrupted'))
                except Exception as error:
                    self.recovery.append(dict(project=record['id'], error=classify(error).as_dict()))
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._guard:
            if self._closed: return
            self._closed = True
        self.executor.shutdown(wait=True)
        os.close(self._lock_fd)

    def coordinator(self, project):
        with self._guard:
            if project not in self._coordinators:
                self._coordinators[project] = ProjectCoordinator(self.executor)
            return self._coordinators[project]

    def catalog(self, project):
        record = self.registry.get(project)
        with self._guard:
            current = self._catalogs.get(project)
            if current is None or current.registration != record:
                current = ProjectCatalog(record, self.registry.policy)
                self._catalogs[project] = current
            return current

    def handle(self, request):
        request_id = request.get('id') if isinstance(request, dict) else None
        try:
            validate_request(request)
            if self._closed:
                raise CoreError('CORE_UNAVAILABLE', 'The core is stopping.', 'Retry after the user service restarts.', True)
            return response(request_id, result=self._dispatch(request))
        except Exception as error:
            return response(request_id, error=classify(error))

    def _dispatch(self, request):
        method, p, actor, reqid = request['method'], request['params'], request['actor'], request['id']
        if method == 'core.status':
            return dict(pid=os.getpid(), protocol_version=1, projects=self.registry.list(), recovery=self.recovery)
        if method == 'doctor':
            from .diagnostics import inspect_registry
            return inspect_registry(self.registry, p.get('project'))
        if method == 'project.add':
            return self.registry.add(Path(p['root']), Path(p['store']) if p.get('store') else None, reqid, actor)
        if method == 'project.list':
            return {'projects': self.registry.list()}
        project = p['project']
        coordinator = self.coordinator(project)
        if method in ('project.relink', 'project.unregister'):
            with coordinator.lock:
                if coordinator.busy and self.catalog(project).has_pending():
                    raise CoreError('PROJECT_BUSY', 'This project has queued or running operations.', 'Wait for its operations to finish before changing registration.', True)
                if method == 'project.relink':
                    return self.registry.relink(project, Path(p['root']), Path(p['store']), p['confirmed_root'], reqid, actor)
                result = self.registry.unregister(project, reqid, actor)
                old = self._catalogs.pop(project, None)
                if old: old.notify()
                return result
        if method == 'project.show':
            return self.registry.show(project)
        catalog = self.catalog(project)
        if method == 'operation.show':
            return catalog.get_operation(p['operation'])
        if method == 'event.subscribe':
            catalog.events_after(p.get('after', 0))
            return {'project': project, 'after': p.get('after', 0)}
        if method in ('checkpoint.create', 'checkpoint.verify', 'checkpoint.restore'):
            with coordinator.lock:
                catalog.validate()
                if method == 'checkpoint.restore':
                    destination = Path(p['destination']).absolute()
                    canonical = destination.resolve()
                    request = dict(request, params=dict(p, destination=str(canonical)))
                    receipt = catalog.find_operation(request)
                    if receipt is not None:
                        return receipt
                    if destination.exists() or destination.is_symlink():
                        raise FileExistsError()
                    for record in self.registry.list():
                        for location in (Path(record['root']), Path(record['store']), self.registry.path.parent):
                            if canonical.is_relative_to(location) or location.is_relative_to(canonical):
                                raise CoreError('UNSUPPORTED_STORAGE', 'The restore destination overlaps managed data or a source root.', 'Choose a new separate destination.')
                    request = dict(request, params=dict(p, destination=str(canonical)))
                accepted = catalog.accept_operation(request)
                operation_id = accepted['operation_id']
                if catalog.get_operation(operation_id)['state'] == 'queued':
                    coordinator.submit(operation_id, lambda: self._work(catalog, operation_id, request))
                return accepted
        with coordinator.lock:
            if coordinator.busy and catalog.has_pending():
                raise CoreError('PROJECT_BUSY', 'This project is processing an operation.', 'Inspect its operation or retry after completion.', True)
            repo = catalog.open_existing()
            if method == 'checkpoint.list':
                return {'checkpoints': [asdict(cp) for cp in repo.list_checkpoints()]}
            if method == 'checkpoint.usage':
                return asdict(repo.storage_usage())
        raise CoreError('INVALID_REQUEST', 'Unsupported method.', 'Use a documented core method.')

    def _work(self, catalog, operation_id, request):
        try:
            catalog.transition(operation_id, 'running')
            repo = catalog.open_existing()
            p, method = request['params'], request['method']
            if method == 'checkpoint.create':
                repo.create_checkpoint(p['message'], p.get('exclusions', ()),
                                       publication_hook=lambda db, cp: catalog.complete_checkpoint(db, operation_id, cp))
                catalog.notify()
            elif method == 'checkpoint.verify':
                report = asdict(repo.verify_checkpoint(p['checkpoint']))
                if report['ok']:
                    catalog.transition(operation_id, 'succeeded', result=report)
                else:
                    error = CoreError('CORRUPT_OBJECT', 'Checkpoint verification failed.', 'Preserve the store and inspect the verification result.').as_dict()
                    catalog.transition(operation_id, 'failed', result=report, error=error)
            elif method == 'checkpoint.restore':
                repo.restore_checkpoint(p['checkpoint'], Path(p['destination']))
                catalog.validate()
                catalog.transition(operation_id, 'succeeded', result={'destination': p['destination'], 'checkpoint_id': p['checkpoint']})
        except Exception as error:
            # Stored outcomes are authoritative even if a post-commit notification fails.
            try:
                if catalog.get_operation(operation_id)['state'] in ('queued','running'):
                    catalog.transition(operation_id, 'failed', error=classify(error).as_dict())
            except Exception:
                log.error('operation unavailable id=%s exception=%s', operation_id, type(error).__name__)
