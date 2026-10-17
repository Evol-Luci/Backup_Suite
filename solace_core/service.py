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
    def __init__(self, registry, *, workers=8, config=None):
        self.registry = registry
        from .workspace_paths import CoreConfig,WorkspacePaths
        data=Path(os.environ.get('XDG_DATA_HOME') or str(Path.home()/'.local/share'))
        self.config=config or CoreConfig.load(data)
        self.workspace_paths=WorkspacePaths(registry,self.config,registry.policy)
        registry.workspace_guard=self.workspace_paths.guard_source
        registry.workspace_store_guard=self.workspace_paths.guard_store
        self._closed = False
        self.maintenance = None
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
            from .maintenance import PromptMaintenance
            self.maintenance=PromptMaintenance(self)
            self.maintenance.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._guard:
            if self._closed: return
            self._closed = True
        if self.maintenance is not None: self.maintenance.close()
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
            self._reconcile_location(catalog)
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

    def _reconcile_location(self,catalog):
        """Complete the latest explicit registry hand-off, including after a crash."""
        import json
        from .schema import schema_version
        if schema_version(catalog) not in (2, 3): return
        with self.registry.connect() as db:
            receipts=[(row[0],json.loads(row[1])) for row in db.execute('SELECT id,response_json FROM requests ORDER BY rowid DESC')]
        for request_id,value in receipts:
            if (value.get('id')!=catalog.registration['id'] or 'previous_location' not in value): continue
            if any(value[key]!=catalog.registration[key] for key in ('root','store')): return
            with catalog.connect() as db:
                done=any(json.loads(row[0]).get('request_id')==request_id for row in db.execute(
                    "SELECT details_json FROM events WHERE kind='project.location.reconciled'"))
            if not done: self._rebase_recovery(catalog,value['previous_location'],request_id)
            return

    def _rebase_recovery(self,catalog,previous,request_id):
        from .schema import schema_version
        from .workspace_paths import directory_identity
        import json
        if schema_version(catalog) not in (2, 3): return
        from .workspace_trees import _verify_tree,_targets,_manifest_digest
        from solace_engine.repository import ProjectRepository
        caches=[]
        with catalog.connect() as db:
            rows=[dict(row) for row in db.execute('SELECT * FROM workspace_caches')]
        repo=ProjectRepository.open_existing(catalog.store.parent,catalog.store)
        for row in rows:
            old=Path(row['path'])
            try: relative=old.relative_to(Path(previous['store']))
            except ValueError:
                raise CoreError('CACHE_CORRUPT','A cache path is outside its recorded store.','Preserve it for inspection.')
            if relative.parts!=('workspace-cache',row['checkpoint_id'],row['cache_id'],'tree'):
                raise CoreError('CACHE_CORRUPT','A cache path has an unexpected layout.','Preserve it for inspection.')
            path=catalog.store/relative
            if path.is_symlink(): raise CoreError('CACHE_CORRUPT','A cache path became a symlink.','Preserve it for inspection.')
            _verify_tree(repo,row['checkpoint_id'],path,sealed=True)
            entries=repo.get_file_entries(row['checkpoint_id'])
            if _manifest_digest(entries,_targets(repo,entries))!=row['manifest_sha256']:
                raise CoreError('CACHE_CORRUPT','Cache manifest metadata changed.','Preserve it for inspection.')
            caches.append((str(path),json.dumps(directory_identity(path,catalog.policy)),row['checkpoint_id']))
        with catalog.connect(write=True) as db:
            for path,identity,checkpoint in caches:
                db.execute('UPDATE workspace_caches SET path=?,identity_json=? WHERE checkpoint_id=?',(path,identity,checkpoint))
            for row in db.execute("SELECT operation_id,recovery_operation_id,expected_json FROM workspace_intents WHERE phase NOT IN ('committed','abandoned')").fetchall():
                context=json.loads(row['expected_json'])
                context['store_identity']=directory_identity(catalog.store,catalog.policy)
                capture=context.get('capture_identity')
                if capture and not context.get('parent') and capture['path']==previous['root']:
                    context['capture_identity']=directory_identity(Path(catalog.registration['root']),catalog.policy)
                db.execute('UPDATE workspace_intents SET expected_json=? WHERE operation_id=?',(json.dumps(context),row['operation_id']))
                for operation in (row['operation_id'],row['recovery_operation_id']):
                    if operation:
                        old=db.execute('SELECT params_json FROM operations WHERE id=?',(operation,)).fetchone()
                        params=json.loads(old[0]);params.update(context)
                        db.execute('UPDATE operations SET params_json=? WHERE id=?',(json.dumps(params),operation))
            catalog._event(db,dict(id=None,actor='core'),'project.location.reconciled','succeeded',dict(request_id=request_id))

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
            return dict(pid=os.getpid(), protocol_version=1, projects=self.registry.list(), recovery=self.recovery,
                        capabilities=['tasks.v1','workspaces.v1','project-upgrade.v1','prompt-retention.v1'],
                        core_schema_supported=[1,2,3],core_schema_created=3,
                        prompt_maintenance=dict(self.maintenance.failures) if self.maintenance else {})
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
                from .protocol import fingerprint
                receipt_params=dict(project=project)
                if method=='project.relink':
                    receipt_params.update(root=str(Path(p['root']).resolve()),store=str(Path(p['store']).resolve()),confirmed_root=p['confirmed_root'])
                with self.registry.connect() as db:
                    receipt=self.registry._receipt(db,reqid,fingerprint(method,receipt_params,actor))
                if receipt is not None:
                    if method=='project.relink':
                        try: self.catalog(project)
                        except CoreError: pass  # Stable receipt; pending reconciliation still gates new work.
                    return receipt
                self.registry.get(project)
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
                    if current is not None:
                        from .schema import schema_version
                        if schema_version(current) in (2, 3):
                            with current.connect() as db:
                                if db.execute('SELECT 1 FROM task_claims LIMIT 1').fetchone():
                                    raise CoreError('TASK_BUSY','Workspace recovery still owns this project.','Resolve or abandon it before relocating available storage.')
                    result = self.registry.relink(project, Path(p['root']), Path(p['store']), p['confirmed_root'], reqid, actor)
                    self._pending_recovery.add(project)
                    try:
                        self.catalog(project)
                    except Exception as error:
                        self.recovery.append(dict(project=project, error=classify(error).as_dict()))
                    return result
                if current is None:
                    raise CoreError('STORE_UNAVAILABLE','Task ownership cannot be checked while the catalog is unavailable.','Reconnect or explicitly relink the store before unregistering.')
                from .schema import schema_version
                if schema_version(current) in (2, 3):
                    from .tasks import TaskCatalog
                    with current.connect() as db: TaskCatalog(current).assert_project_unregisterable(db)
                result = self.registry.unregister(project, reqid, actor)
                old = self._catalogs.pop(project, None)
                if old: old.notify()
                return result
        if method == 'project.show':
            return self.registry.show(project)
        if method in ('project.upgrade','project.prompt-policy.show','project.prompt-policy.set') or method.startswith(('task.','workspace.')):
            from .tasks import TaskCatalog
            from .prompts import PromptStore
            from .task_operations import TaskOperations
            from .schema import preview_upgrade,apply_upgrade,require_tasks
            with coordinator.lock:
                catalog,registered=self._receipt_catalog(project)
                if method=='project.upgrade':
                    if not p['preview']:
                        from .schema import schema_version
                        if schema_version(catalog) in (2, 3):
                            with catalog.connect() as db: cached=TaskCatalog(catalog).lookup_request(request,db)
                            if cached is not None: return cached
                    if not registered: raise CoreError('PROJECT_NOT_FOUND','The project is no longer registered.','Register it before upgrading.')
                    return preview_upgrade(catalog) if p['preview'] else apply_upgrade(catalog,p.get('plan_token'),request)
                require_tasks(catalog);tasks=TaskCatalog(catalog);prompts=PromptStore(catalog)
                if method=='workspace.recovery.show': return TaskOperations(self,catalog).inspect_recovery(p['operation'])
                if method=='task.show': return tasks.show(p['task'])
                if method=='task.list': return tasks.list_page(p.get('parent'),p.get('after',0),p.get('limit',128))
                if method=='task.prompt.show': return prompts.read(p['task'])
                if method=='project.prompt-policy.show': return prompts.policy()
                if method=='workspace.status' and not p.get('refresh',False):
                    import json
                    from .observations import change_page
                    with catalog.connect() as db:
                        row=db.execute('SELECT observed_json FROM workspaces WHERE task_id=?',(p['task'],)).fetchone()
                    report=json.loads(row[0]) if row and row[0] else None
                    return dict(task_id=p['task'],workspace=tasks.show(p['task'])['workspace'],
                                changes=change_page(report,p.get('after',0),p.get('limit',128),p.get('observation')))
                with catalog.connect() as db: cached=tasks.lookup_request(request,db)
                if cached is not None: return cached
                if not registered: raise CoreError('PROJECT_NOT_FOUND','The project is no longer registered.','Retained records are read-only.')
                if method=='workspace.recovery.abandon': return TaskOperations(self,catalog).abandon(request)
                if method=='workspace.staging.cleanup': return TaskOperations(self,catalog).cleanup_staging(request)
                if method in ('task.block','task.resume','task.cancel'): return tasks.transition(request)
                if method=='task.create': return tasks.create(request)
                if method=='task.prompt.delete': return prompts.delete(request)
                if method=='project.prompt-policy.set': return prompts.set_policy(request)
                return TaskOperations(self,catalog).accept(request)
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
                from .schema import schema_version
                if schema_version(catalog) in (2, 3):
                    from .lineage import Lineage
                    return {'checkpoints': Lineage(catalog).published(p.get('all_workspaces',False))}
                return {'checkpoints': [asdict(cp) for cp in repo.list_checkpoints()]}
            if method == 'checkpoint.usage':
                from .schema import schema_version
                if schema_version(catalog) in (2, 3):
                    from .lineage import Lineage
                    return Lineage(catalog).source_usage()
                return asdict(repo.storage_usage())
        raise CoreError('INVALID_REQUEST', 'Unsupported method.', 'Use a documented core method.')

    def _work(self, catalog, operation_id, request):
        try:
            catalog.transition(operation_id, 'running')
            repo = catalog.open_existing()
            p, method = request['params'], request['method']
            if method == 'checkpoint.create':
                from .schema import schema_version
                if schema_version(catalog) in (2, 3):
                    from .lineage import Lineage
                    lineage=Lineage(catalog)
                    def publish(db,cp):
                        lineage.publish_source(db,cp,p.get('exclusions',[]))
                        catalog.complete_checkpoint(db,operation_id,cp)
                    repo.create_checkpoint(p['message'],p.get('exclusions',()),parent_id=lineage.source_head(),publication_hook=publish)
                else:
                    repo.create_checkpoint(p['message'],p.get('exclusions',()),
                                           publication_hook=lambda db,cp: catalog.complete_checkpoint(db,operation_id,cp))
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
