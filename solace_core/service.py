"""Single local authority for registration, operation scheduling, and recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
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
        self._check_cancellations = {}
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
            for cancellation in self._check_cancellations.values(): cancellation.set()
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

    def _receipt_catalog(self, project, *, metadata=False):
        if metadata:
            # Retained integration receipts/metadata must not finish a pending
            # content/location handoff before their durable replay lookup.
            try:
                record, registered = self.registry.get(project), True
            except CoreError as error:
                if error.code != 'PROJECT_NOT_FOUND': raise
                record, registered = self.registry.retired_registration(project), False
            with self._guard:
                catalog = self._catalogs.get(project)
                if catalog is None or catalog.registration != record:
                    catalog = ProjectCatalog(record,self.registry.policy)
                    if registered: self._catalogs[project] = catalog
            catalog.validate(require_root=False,require_objects=False)
            return catalog, registered
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
        if schema_version(catalog) not in (2, 3, 4, 5): return
        with self.registry.connect() as db:
            receipts=[(row[0],json.loads(row[1])) for row in db.execute('SELECT id,response_json FROM requests ORDER BY rowid DESC')]
        for request_id,value in receipts:
            if (value.get('id')!=catalog.registration['id'] or 'previous_location' not in value): continue
            if any(value[key]!=catalog.registration[key] for key in ('root','store')): return
            with catalog.connect(metadata=True) as db:
                done=any(json.loads(row[0]).get('request_id')==request_id for row in db.execute(
                    "SELECT details_json FROM events WHERE kind='project.location.reconciled'"))
            if not done: self._rebase_recovery(catalog,value['previous_location'],request_id)
            return

    def _rebase_recovery(self,catalog,previous,request_id):
        from .schema import schema_version
        from .workspace_paths import directory_identity
        import json
        if schema_version(catalog) not in (2, 3, 4, 5): return
        if schema_version(catalog) in (3, 4, 5):
            from .integration_recovery import IntegrationRecovery
            IntegrationRecovery(self,catalog).reconcile_location(previous,request_id)
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
                        capabilities=['tasks.v1','workspaces.v1','project-upgrade.v1','prompt-retention.v1','integrations.v1','checks.v1','policy.v1'],
                        core_schema_supported=[1,2,3,4,5],core_schema_created=5,
                        prompt_maintenance=dict(self.maintenance.failures) if self.maintenance else {})
        if method == 'doctor':
            from .diagnostics import inspect_registry
            return inspect_registry(self.registry, p.get('project'))
        if method == 'project.add':
            return self.registry.add(Path(p['root']), Path(p['store']) if p.get('store') else None, reqid, actor)
        if method == 'project.list':
            return {'projects': self.registry.list()}
        project = p['project']
        if method in ('integration.queue.list', 'integration.queue.show'):
            from .policy_queue import PolicyQueue
            catalog = self.catalog(project)
            if method == 'integration.queue.list':
                return PolicyQueue.list(catalog, p.get('after', 0), p.get('limit', 128))
            return PolicyQueue.show(catalog, p['intent'])
        if method.startswith('task.policy.'):
            from .task_policy import TaskPolicy
            policy = TaskPolicy(self.catalog(project))
            if method == 'task.policy.show': return policy.show(p['task'])
            if method == 'task.policy.preview': return policy.preview(request)
            if method == 'task.policy.apply': return policy.apply(request)
        if method.startswith('project.policy.'):
            from .project_policy import ProjectPolicy
            policy = ProjectPolicy(self.catalog(project))
            if method == 'project.policy.show': return policy.show()
            if method == 'project.policy.preview': return policy.preview(request)
            if method == 'project.policy.apply': return policy.apply(request)
        if method.startswith('project.check-profile.') or method == 'task.check-profile.select':
            from .schema import require_checks
            from .check_profiles import CheckProfiles
            from .check_selection import CheckSelection
            catalog = self.catalog(project)
            require_checks(catalog)
            profiles = CheckProfiles(catalog)
            if method == 'project.check-profile.list': return profiles.list(p.get('after',0),p.get('limit',128))
            if method == 'project.check-profile.show': return profiles.show(p['profile_id'])
            if method == 'project.check-profile.preview': return profiles.preview(request)
            if method == 'project.check-profile.apply': return profiles.apply(request)
            return CheckSelection(catalog).select(request)
        if method.startswith('integration.check.'):
            from .check_runs import CheckRuns
            catalog, registered = self._receipt_catalog(project,metadata=True)
            from .schema import require_checks
            require_checks(catalog)
            runs = CheckRuns(self,catalog)
            if method == 'integration.check.show': return runs.show(p['run'])
            if method == 'integration.check.list': return runs.list(p['plan'],p.get('after',0),p.get('limit',128))
            if method == 'integration.check.cancel': return runs.cancel(request)
            if method == 'integration.check.run':
                if not registered:
                    raise CoreError('PROJECT_NOT_FOUND','New checks require a registered project.','Register the project before running checks.')
                return runs.start(request)
        if method.startswith('integration.'):
            from .integration_operations import IntegrationOperations
            catalog, registered = self._receipt_catalog(project,metadata=True)
            return IntegrationOperations(self,catalog).dispatch(request,registered=registered)
        coordinator = self.coordinator(project)
        if method in ('project.relink', 'project.unregister'):
            with self.registry.lock, coordinator.lock:
                from .protocol import fingerprint
                receipt_params=dict(project=project)
                if method=='project.relink':
                    receipt_params.update(root=str(Path(p['root']).resolve()),store=str(Path(p['store']).resolve()),confirmed_root=p['confirmed_root'])
                with self.registry.connect() as db:
                    receipt=self.registry._receipt(db,reqid,fingerprint(method,receipt_params,actor))
                if receipt is not None:
                    return receipt
                self.registry.get(project)
                try:
                    current = self.catalog(project)
                    current.validate(require_root=False)
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
                if current is not None:
                    self.workspace_paths.assert_access(Path(current.registration['root']))
                    with current.connect() as db:
                        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (4, 5) and db.execute("SELECT 1 FROM check_runs WHERE state IN ('reserved','running')").fetchone():
                            raise CoreError('PROJECT_BUSY','A configured check still owns this project.','Wait for or cancel its run before changing registration.',True)
                        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (3, 4, 5) and db.execute("SELECT 1 FROM integration_journals WHERE phase NOT IN ('committed','restored','abandoned')").fetchone():
                            raise CoreError('INTEGRATION_TARGET_BUSY','Integration recovery still owns this project.','Resolve integration recovery before changing registration.',True)
                if method == 'project.relink':
                    if current is None:
                        # An explicit repair may present the same store at its new
                        # location. With no in-process worker, classify its stopped
                        # readers before placement checks; publication claims remain.
                        candidate=self.registry.get(project)
                        location=self.registry.policy.check(Path(p['store']))
                        candidate.update(root=str(Path(p['root']).resolve()),store=str(location.canonical),mount_id=location.mount_id)
                        repair=ProjectCatalog(candidate,self.registry.policy)
                        repair.validate()
                        with repair.connect() as db:
                            if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (4, 5) and db.execute("SELECT 1 FROM check_runs WHERE state IN ('reserved','running')").fetchone():
                                raise CoreError('PROJECT_BUSY','A configured check still owns this project.','Resolve its run before changing registration.',True)
                        # Registry receipt plus logical unresolved-journal ownership
                        # gates the moved aliases until explicit recovery handoff.
                        repair.recover_interrupted()
                    if current is not None:
                        from .schema import schema_version
                        if schema_version(current) in (2, 3, 4, 5):
                            with current.connect() as db:
                                if db.execute('SELECT 1 FROM task_claims LIMIT 1').fetchone():
                                    raise CoreError('TASK_BUSY','Workspace recovery still owns this project.','Resolve or abandon it before relocating available storage.')
                    result = self.registry.relink(project, Path(p['root']), Path(p['store']), p['confirmed_root'], reqid, actor)
                    self._pending_recovery.add(project)
                else:
                    if current is None:
                        raise CoreError('STORE_UNAVAILABLE','Task ownership cannot be checked while the catalog is unavailable.','Reconnect or explicitly relink the store before unregistering.')
                    from .schema import schema_version
                    if schema_version(current) in (2, 3, 4, 5):
                        from .tasks import TaskCatalog
                        with current.connect() as db: TaskCatalog(current).assert_project_unregisterable(db)
                    result = self.registry.unregister(project, reqid, actor)
                    old = self._catalogs.pop(project, None)
                    if old: old.notify()
                    return result
            # Reconciliation can verify complete caches; it runs after the
            # durable registration handoff, outside the placement lock.
            try:
                self.catalog(project)
            except Exception as error:
                self.recovery.append(dict(project=project,error=classify(error).as_dict()))
            return result
        if method == 'project.show':
            return self.registry.show(project)
        if method in ('project.upgrade','project.prompt-policy.show','project.prompt-policy.set') or method.startswith(('task.','workspace.')):
            from .tasks import TaskCatalog
            from .prompts import PromptStore
            from .task_operations import TaskOperations
            from .schema import preview_upgrade,apply_upgrade,require_tasks
            placement = self.registry.lock if method in ('task.start','task.ready','workspace.checkpoint','workspace.relink','workspace.status','workspace.recovery.resume') else nullcontext()
            with placement, coordinator.lock:
                catalog,registered=self._receipt_catalog(project)
                if method=='project.upgrade':
                    if not p['preview']:
                        from .schema import schema_version
                        if schema_version(catalog) in (2, 3, 4, 5):
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
        placement = self.registry.lock if method in ('checkpoint.create','checkpoint.restore') else nullcontext()
        with placement, coordinator.lock:
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
                if method == 'checkpoint.create': self.workspace_paths.assert_access(Path(catalog.registration['root']))
                if method == 'checkpoint.restore':
                    self.workspace_paths.assert_access(canonical)
                    self.workspace_paths.assert_unowned(canonical)
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
                if schema_version(catalog) in (2, 3, 4, 5):
                    from .lineage import Lineage
                    return {'checkpoints': Lineage(catalog).published(p.get('all_workspaces',False))}
                return {'checkpoints': [asdict(cp) for cp in repo.list_checkpoints()]}
            if method == 'checkpoint.usage':
                from .schema import schema_version
                if schema_version(catalog) in (2, 3, 4, 5):
                    from .lineage import Lineage
                    return Lineage(catalog).source_usage()
                return asdict(repo.storage_usage())
        raise CoreError('INVALID_REQUEST', 'Unsupported method.', 'Use a documented core method.')

    def _work(self, catalog, operation_id, request):
        try:
            catalog.transition(operation_id, 'running')
            p, method = request['params'], request['method']
            from .integration_claims import IntegrationClaims
            claims = IntegrationClaims(self,catalog)
            paths = [catalog.registration['root']] if method == 'checkpoint.create' else ([p['destination']] if method == 'checkpoint.restore' else [])
            with self.registry.lock, self.coordinator(catalog.registration['id']).lock, catalog.connect(write=True) as db:
                if method == 'checkpoint.restore':
                    destination=Path(p['destination'])
                    self.workspace_paths.assert_unowned(destination)
                    # Retained managed paths were checked above; ordinary
                    # source ownership ends when its registration is removed.
                    for record in self.registry.list():
                        root=Path(record['root'])
                        if destination.is_relative_to(root) or root.is_relative_to(destination):
                            raise CoreError('UNSUPPORTED_STORAGE','The restore destination now overlaps registered data.','Choose a fresh separate destination.')
                claims.claim_access(db,operation_id,paths)
            repo = catalog.open_existing()
            if method == 'checkpoint.create':
                from .schema import schema_version
                if schema_version(catalog) in (2, 3, 4, 5):
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

        finally:
            try:
                with catalog.connect(write=True) as db:
                    if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (3, 4, 5):
                        db.execute('DELETE FROM integration_claims WHERE operation_id=?',(operation_id,))
            except Exception:
                self._pending_recovery.add(catalog.registration['id'])
