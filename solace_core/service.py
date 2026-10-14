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
        self._pending_recovery = set()
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
            records = registry.list()
            self._pending_recovery.update(record['id'] for record in records)
            for record in records:
                try:
                    self.catalog(record['id'])
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
        with self.coordinator(project).lock:
            record = self.registry.get(project)
            with self._guard:
                current = self._catalogs.get(project)
                if current is None or current.registration != record:
                    current = ProjectCatalog(record, self.registry.policy)
                    self._catalogs[project] = current
            self._recover_pending(project, current)
            return current

    def _recover_pending(self, project, catalog):
        if project in self._pending_recovery:
            ids = catalog.recover_interrupted()
            self._pending_recovery.discard(project)
            self.recovery[:] = [row for row in self.recovery if not (row.get('project') == project and 'error' in row)]
            if ids:
                self.recovery.append(dict(project=project, operations=ids, state='interrupted'))

    def _receipt_catalog(self, project):
        try:
            return self.catalog(project), True
        except CoreError as error:
            if error.code != 'PROJECT_NOT_FOUND':
                raise
            catalog = ProjectCatalog(self.registry.retired_registration(project), self.registry.policy)
            if not self.coordinator(project).busy and catalog.has_pending():
                self._pending_recovery.add(project)
            self._recover_pending(project, catalog)
            return catalog, False

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
                try:
                    current = self.catalog(project)
                except Exception as error:
                    code = classify(error).code
                    if code not in {'PROJECT_NOT_FOUND', 'STORE_UNAVAILABLE', 'DATABASE_CORRUPT',
                                    'SCHEMA_VERSION_UNSUPPORTED', 'STORE_IDENTITY_MISMATCH',
                                    'UNSUPPORTED_STORAGE', 'PERMISSION_DENIED'}:
                        raise
                    if coordinator.busy:
                        raise CoreError('PROJECT_BUSY', 'An in-process operation still owns this project.',
                                        'Wait for that operation to stop before changing registration.', True) from error
                    current = None  # Explicit lifecycle changes can repair unavailable locations.
                if current is not None and current.has_pending():
                    raise CoreError('PROJECT_BUSY', 'This project has queued or running operations.', 'Wait for its operations to finish before changing registration.', True)
                if method == 'project.relink':
                    result = self.registry.relink(project, Path(p['root']), Path(p['store']), p['confirmed_root'], reqid, actor)
                    try:
                        self.catalog(project)  # Reconcile a previously unavailable store at its validated new path.
                    except Exception as error:
                        self.recovery.append(dict(project=project, error=classify(error).as_dict()))
                    return result
                result = self.registry.unregister(project, reqid, actor)
                old = self._catalogs.pop(project, None)
                if old: old.notify()
                return result
        if method == 'project.show':
            return self.registry.show(project)
        with coordinator.lock:
            if method in ('checkpoint.create', 'checkpoint.verify', 'checkpoint.restore', 'operation.show'):
                catalog, registered = self._receipt_catalog(project)
            else:
                catalog, registered = self.catalog(project), True
            if method == 'operation.show':
                return catalog.get_operation(p['operation'])
            if method == 'event.subscribe':
                catalog.events_after(p.get('after', 0))
                return {'project': project, 'after': p.get('after', 0)}
            if method in ('checkpoint.create', 'checkpoint.verify', 'checkpoint.restore'):
                if method == 'checkpoint.restore':
                    destination = Path(p['destination']).absolute()
                    canonical = destination.resolve()
                    request = dict(request, params=dict(p, destination=str(canonical)))
                receipt = catalog.find_operation(request)
                if receipt is not None:
                    return receipt
                if not registered:
                    raise CoreError('PROJECT_NOT_FOUND', 'The project is no longer registered.',
                                    'Retained receipts can be inspected; new operations require a registered project.')
                catalog.validate()
                if method == 'checkpoint.restore':
                    if destination.exists() or destination.is_symlink():
                        raise FileExistsError()
                    for record in self.registry.list():
                        for location in (Path(record['root']), Path(record['store']), self.registry.path.parent):
                            if canonical.is_relative_to(location) or location.is_relative_to(canonical):
                                raise CoreError('UNSUPPORTED_STORAGE', 'The restore destination overlaps managed data or a source root.', 'Choose a new separate destination.')
                accepted = catalog.accept_operation(request)
                operation_id = accepted['operation_id']
                if catalog.get_operation(operation_id)['state'] == 'queued':
                    coordinator.submit(operation_id, lambda: self._work(catalog, operation_id, request))
                return accepted
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
                with self.coordinator(catalog.registration['id']).lock:
                    self._pending_recovery.add(catalog.registration['id'])
                log.error('operation unavailable id=%s exception=%s', operation_id, type(error).__name__)
