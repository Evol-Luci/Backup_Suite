"""Exact contribution ancestry and atomic, unpublished integration drafts."""
from contextlib import contextmanager
from dataclasses import asdict
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from solace_engine.repository import ProjectRepository
from .catalog import ProjectCatalog
from .errors import CoreError, classify
from .integration_merge import merge_entries, _entry_key
from .integration_snapshots import SnapshotStore, canonical, entry_json, entry_from_json
from .integration_types import Identity, Target, PreparedPlan, MergeConflict
from .lineage import Lineage
from .schema import require_integrations
from .task_operations import rowdict, staging_entries
from .tasks import TaskCatalog
from .workspace_paths import directory_identity, protected_excluder


def relevant_boundaries(roots, target):
    """Bind only ownership intersecting the fixed target; keep global guards."""
    target = Path(target)
    return sorted({str(path) for path in map(Path, roots)
                   if path.is_relative_to(target) or target.is_relative_to(path)})


def _refuse(code, message):
    raise CoreError(code, message, 'Inspect the task, retained plan and destination before preparing a new plan.')


def review_token(plan, expected):
    """Stable original B/R/C/M authentication, shared by every retained reader."""
    value = dict(plan=plan.id, revision=plan.revision, expected=expected,
                 **{name: asdict(getattr(plan, name)) for name in ('base','current','incoming','candidate')})
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@contextmanager
def _directory(path):
    """Hold every component without following a substituted ancestor."""
    path = Path(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        yield fd
    finally:
        os.close(fd)


class IntegrationPlans:
    def __init__(self, service, catalog):
        self.service, self.catalog = service, catalog
        self.tasks = TaskCatalog(catalog)
        self.paths = service.workspace_paths
        self.coordinator = service.coordinator(catalog.registration['id'])
        self._snapshots = None

    @property
    def snapshots(self):
        if self._snapshots is None:
            self._snapshots = SnapshotStore(self.catalog)
        return self._snapshots

    @staticmethod
    def _latest(db, task):
        row = db.execute("SELECT checkpoint_id FROM task_checkpoint_refs WHERE task_id=? AND role='result' "
                         'ORDER BY created_at DESC,rowid DESC LIMIT 1', (task,)).fetchone()
        return row[0] if row else None

    def target(self, db, task_id: str) -> Target:
        task = self.tasks.row(db, task_id)
        if task['parent_id']:
            parent = self.tasks.row(db, task['parent_id'])
            workspace = rowdict(db, 'SELECT * FROM workspaces WHERE task_id=?', (parent['id'],))
            if workspace is None:
                _refuse('WORKSPACE_UNAVAILABLE', 'The recorded parent has no published workspace.')
            identity = self.paths.validate(Path(workspace['path']), expected=workspace)
            return Target('workspace:'+workspace['id'], parent['id'], parent['revision'],
                          workspace['head_checkpoint'], Identity(**identity))
        self.catalog.validate()
        identity = directory_identity(Path(self.catalog.registration['root']), self.catalog.policy)
        return Target('source', None, None, Lineage(self.catalog).source_head(db), Identity(**identity))

    @staticmethod
    def _ancestor(db, ancestor, checkpoint):
        visited = set()
        while checkpoint is not None:
            if checkpoint in visited:
                _refuse('INTEGRATION_ANCESTRY_DIVERGED', 'Checkpoint ancestry contains a cycle.')
            visited.add(checkpoint)
            if checkpoint == ancestor:
                return True
            row = db.execute('SELECT parent_id FROM checkpoints WHERE id=?', (checkpoint,)).fetchone()
            if row is None:
                return False
            checkpoint = row[0]
        return False

    def baseline(self, db, task_id: str, result_id: str, target_key: str) -> tuple[str, str | None]:
        previous = rowdict(db, 'SELECT id,result_id FROM integration_receipts WHERE task_id=? AND target_key=? '
                           'ORDER BY created_at DESC,rowid DESC LIMIT 1', (task_id, target_key))
        workspace = rowdict(db, 'SELECT base_checkpoint FROM workspaces WHERE task_id=?', (task_id,))
        if workspace is None:
            _refuse('BASELINE_INELIGIBLE', 'The source task has no saved start checkpoint.')
        base = previous['result_id'] if previous else workspace['base_checkpoint']
        if not self._ancestor(db, base, result_id):
            _refuse('INTEGRATION_ANCESTRY_DIVERGED', 'The latest contribution or saved start is not an ancestor of this result.')
        return base, previous['id'] if previous else None

    def _inputs(self, db, params):
        task = self.tasks.assert_revision(db, params['task'], params['expected_revision'])
        if task['state'] != 'ready' or self._latest(db, task['id']) != params['result']:
            _refuse('TASK_STATE_INVALID', 'The source must be ready with this exact latest ready result.')
        context = rowdict(db, 'SELECT * FROM checkpoint_context WHERE checkpoint_id=?', (params['result'],))
        workspace = rowdict(db, 'SELECT * FROM workspaces WHERE task_id=?', (task['id'],))
        if (not context or context['purpose'] != 'ready' or not context['published']
                or context['task_id'] != task['id'] or not workspace or workspace['head_checkpoint'] != params['result']):
            _refuse('BASELINE_INELIGIBLE', 'The selected result is not the source task latest published ready head.')
        if task['parent_id']:
            self.tasks.assert_revision(db, task['parent_id'], params.get('expected_target_revision'))
        elif 'expected_target_revision' in params:
            _refuse('INVALID_REQUEST', 'A source target has no expected target task revision.')
        target = self.target(db, task['id'])
        if target.task_id is None:
            for child in db.execute('SELECT id,state FROM tasks WHERE parent_id=?', (task['id'],)):
                if child['state'] == 'cancelled':
                    continue
                latest = self._latest(db, child['id'])
                receipt = db.execute('SELECT 1 FROM integration_receipts WHERE task_id=? AND result_id=? AND target_key=?',
                                     (child['id'], latest, 'workspace:'+workspace['id'])).fetchone()
                if child['state'] != 'ready' or receipt is None:
                    _refuse('TASK_CHILDREN_PENDING', 'Every child must be cancelled or its latest ready result applied to this parent.')
        base, previous = self.baseline(db, task['id'], params['result'], target.key)
        return task, target, base, previous

    def _boundaries(self, operation_id=None):
        roots = {self.catalog.store, self.paths.config.workspace_root, self.paths.registry.path.parent/'projects', *self.paths.config.sync_roots}
        roots.update(path for path, _ in self.paths.managed_locations(include_read_claims=False))
        records = self.paths.registry.known_registrations()
        for record in records:
            catalog = ProjectCatalog(record, self.catalog.policy)
            with catalog.connect() as db:
                if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] == 4:
                    for row in db.execute('SELECT staging_path FROM check_runs'):
                        stage = Path(row[0])
                        roots.update((stage, stage.parent.parent/'check-evidence'/stage.name))
                if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (3, 4):
                    for table, column in (('integration_journals', 'journal_path'), ('integration_exports', 'path'), ('integration_claims', 'path')):
                        where = " WHERE kind!='target'" if table == 'integration_claims' else ''
                        roots.update(Path(row[0]) for row in db.execute(f'SELECT {column} FROM {table}'+where))
        if operation_id:
            with self.catalog.connect() as db:
                owned = {Path(row[0]) for row in db.execute("SELECT path FROM integration_claims WHERE operation_id=? AND kind!='target'",(operation_id,))}
                owned.update(Path(row[0]) for row in db.execute('SELECT journal_path FROM integration_journals WHERE operation_id=?',(operation_id,)))
            roots.difference_update(owned)
        return sorted(str(path) for path in roots)

    def _expected(self, db, params, target, roots):
        root = Path(target.identity.path)
        ancestors = [directory_identity(path, self.catalog.policy) for path in root.parents]
        return dict(task_id=params['task'], result_id=params['result'], source_revision=params['expected_revision'],
                    target=asdict(target), store_identity=directory_identity(self.catalog.store, self.catalog.policy),
                    ancestors=ancestors, protected_roots=relevant_boundaries(roots, root),
                    source_head=Lineage(self.catalog).source_head(db), checks='not_run_by_solace', directory_inventories={})

    @staticmethod
    def _excluder(root, profile, roots):
        # Catalog paths retain ownership even if an external writer replaces a
        # leaf/ancestor with a symlink. Never transfer that ownership to the
        # symlink destination when reusing the shared physical-root excluder.
        owned = [Path(path) for path in roots if not root.is_relative_to(Path(path))]
        physical = [path for path in owned if path.resolve() == path]
        protected = protected_excluder(root, profile, physical)
        def excluded(relative, metadata):
            path = root/relative
            return any(path.is_relative_to(owner) for owner in owned) or protected(relative, metadata)
        return excluded

    def _protected_delta(self, root, profile, roots, base, incoming):
        exclude = self._excluder(root, profile, roots)
        b = {e.path:e for e in base}; r = {e.path:e for e in incoming}
        for path in sorted(b.keys() | r.keys()):
            if _entry_key(b.get(path)) == _entry_key(r.get(path)):
                continue
            if any(Path(owned).is_relative_to(root/path) for owned in roots):
                _refuse('INTEGRATION_PROTECTED_PATH', 'The incoming delta crosses a retained Solace ownership boundary.')
            parts = path.split('/')
            for length in range(1, len(parts)+1):
                relative = '/'.join(parts[:length])
                try:
                    metadata = (root/relative).lstat()
                except (FileNotFoundError, NotADirectoryError):
                    metadata = os.stat_result((0,)*10)
                if (exclude(relative, metadata) or any(fnmatch.fnmatchcase(relative, pattern)
                    or fnmatch.fnmatchcase(parts[length-1], pattern) for pattern in profile['exclusions'])):
                    _refuse('INTEGRATION_PROTECTED_PATH', 'The incoming delta crosses a protected or excluded destination boundary.')

    def _inventories(self, root, current, candidate):
        """Record complete actual directory contents, including excluded/empty entries."""
        current_paths = {e.path for e in current}
        inventories = {}
        for entry in candidate:
            path = root/entry.path
            try:
                metadata = path.lstat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            with _directory(path) as fd:
                if not os.path.samestat(metadata, os.fstat(fd)):
                    _refuse('INTEGRATION_STALE', 'A replacement directory changed during inventory.')
                rows = staging_entries(fd)
            inventories[entry.path] = dict(identity=directory_identity(path, self.catalog.policy), entries=rows)
            # Directories themselves are not versioned: even empty remaining
            # directories cannot be silently deleted as part of a replacement.
            if any(stat.S_ISDIR(row['mode']) or entry.path+'/'+row['path'] not in current_paths for row in rows):
                _refuse('INTEGRATION_DIRECTORY_NOT_EMPTY', 'A directory replacement contains unversioned, protected or empty descendants.')
        return inventories

    def prepare(self, operation_id: str, request: dict) -> PreparedPlan:
        require_integrations(self.catalog)
        params = request['params']
        try:
            with self.paths.registry.lock, self.coordinator.lock:
                # Historical application is checked before revisions and live
                # target availability. It never authorizes a second application.
                with self.catalog.connect(write=True) as db:
                    task = self.tasks.row(db, params['task'])
                    workspace = db.execute('SELECT id FROM workspaces WHERE task_id=?', (task['parent_id'],)).fetchone() if task['parent_id'] else None
                    key = 'workspace:'+workspace[0] if workspace else 'source'
                    receipt = rowdict(db, 'SELECT id,plan_id,revision FROM integration_receipts WHERE task_id=? AND result_id=? AND target_key=?',
                                      (task['id'], params['result'], key))
                    if receipt:
                        self.catalog._transition(db, operation_id, 'succeeded', result=dict(plan_id=receipt['plan_id'],
                            revision=receipt['revision'], receipt_id=receipt['id'], already_applied=True))
                    else:
                        task, target, base_id, previous = self._inputs(db, params)
                        revisions = {task['id']: task['revision']}
                        if target.task_id:
                            revisions[target.task_id] = target.revision
                        owned = {r[0] for r in db.execute('SELECT task_id FROM task_claims WHERE operation_id=?',(operation_id,))}
                        if owned:
                            if owned != set(revisions): _refuse('TASK_BUSY','Preparation lost its exact accepted task claims.')
                            for task_id, revision in revisions.items(): self.tasks.assert_revision(db,task_id,revision)
                        else:
                            self.tasks.claim(db, operation_id, revisions)
                        from .integration_claims import IntegrationClaims
                        IntegrationClaims(self.service,self.catalog).claim_access(db,operation_id,[target.identity.path])
                        operation_params = {key:params[key] for key in ('project', 'task', 'result', 'expected_revision', 'expected_target_revision') if key in params}
                        db.execute('UPDATE operations SET params_json=? WHERE id=?', (canonical(operation_params), operation_id))
            if receipt:
                self.catalog.notify()
                return self.load(receipt['plan_id'], receipt['revision'])
            profile = json.loads(task['profile_json'])
            base = self.snapshots.version(base_id, profile)
            incoming = self.snapshots.version(params['result'], profile)
            b, r = self.snapshots.entries(base), self.snapshots.entries(incoming)
            roots = self._boundaries()
            root = Path(target.identity.path)
            self._protected_delta(root, profile, roots, b, r)
            with self.catalog.connect() as db:
                expected = self._expected(db, params, target, roots)
            repo = ProjectRepository.open_existing(root, self.catalog.store)
            excluded = self._excluder(root, profile, roots)
            current_entries, warnings = repo._scan(profile['exclusions'], excluded=excluded)
            if warnings:
                _refuse('INTEGRATION_CAPTURE_INCOMPLETE', 'In-scope scan warnings prevent a complete destination candidate.')
            current_entries = tuple(current_entries)
            outcome = merge_entries(b, current_entries, r, read_bytes=self.snapshots.read_bytes, write_bytes=self.snapshots.write_bytes)
            expected['directory_inventories'] = self._inventories(root, current_entries, outcome.entries)
            purpose = 'workspace' if target.task_id else 'source'
            verified_current = self.snapshots.verify_snapshot(current_entries,parent_id=target.head,profile=profile,
                task_id=target.task_id or task['id'],purpose=purpose)
            verified_candidate = self.snapshots.verify_snapshot(outcome.entries,parent_id=verified_current.checkpoint_id,profile=profile,
                task_id=target.task_id or task['id'],purpose=purpose)
            # Durable tree/task claims protect this live reinspection. No global
            # lock or writer transaction is held while bytes are hashed.
            inventories = self._inventories(root,current_entries,outcome.entries)
            observed,warnings = repo.inspect_tree(profile['exclusions'],excluded=excluded)
            if warnings or {e.path:_entry_key(e) for e in observed} != {e.path:_entry_key(e) for e in current_entries}:
                _refuse('INTEGRATION_STALE','Destination bytes changed during preparation.')
            with self.paths.registry.lock, self.coordinator.lock, self.catalog.connect(write=True) as db:
                _, checked_target, checked_base, checked_previous = self._inputs(db, params)
                if (checked_target, checked_base, checked_previous) != (target, base_id, previous):
                    _refuse('INTEGRATION_STALE', 'The task contribution or target changed during preparation.')
                for task_id in revisions:
                    owner = db.execute('SELECT operation_id FROM task_claims WHERE task_id=?', (task_id,)).fetchone()
                    if owner is None or owner[0] != operation_id:
                        _refuse('TASK_BUSY', 'The preparation no longer owns its source and target tasks.')
                if relevant_boundaries(roots, root) != relevant_boundaries(self._boundaries(), root):
                    _refuse('INTEGRATION_STALE', 'Protected ownership boundaries changed during preparation.')
                fresh = self._expected(db, params, target, roots)
                fresh['directory_inventories'] = inventories
                if fresh != expected:
                    _refuse('INTEGRATION_STALE', 'Physical identities or replacement inventories changed during preparation.')
                current = self.snapshots.insert_verified(db,verified_current)
                candidate = self.snapshots.insert_verified(db,verified_candidate)
                now = self.catalog._now(); plan_id = uuid.uuid4().hex
                revision = 1
                state = 'conflicted' if outcome.conflicts else 'prepared'
                db.execute('INSERT INTO integration_plans VALUES (?,?,?,?,?,?,?,?,?)',
                           (plan_id, task['id'], params['result'], target.key, target.task_id, state, revision, now, now))
                token = hashlib.sha256(canonical(dict(plan=plan_id, revision=revision, expected=expected,
                    base=asdict(base), incoming=asdict(incoming), current=asdict(current), candidate=asdict(candidate))).encode()).hexdigest()
                db.execute('INSERT INTO integration_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (plan_id, revision, base.checkpoint_id, incoming.checkpoint_id, current.checkpoint_id,
                     candidate.checkpoint_id, previous, canonical(profile), canonical(expected), candidate.manifest_sha256, token, now, now))
                self._decisions(db, plan_id, revision, b, current_entries, r, outcome)
                for role, version in (('base', base), ('incoming', incoming), ('current', current), ('candidate', candidate)):
                    db.execute('INSERT INTO integration_refs VALUES (?,?,?,?)', (version.checkpoint_id, plan_id, role, now))
                self.catalog._transition(db, operation_id, 'succeeded', result=dict(plan_id=plan_id, revision=revision,
                    state=state, task_id=task['id'], conflict_count=len(outcome.conflicts), checks='not_run_by_solace'), kind='integration.prepared')
                IntegrationClaims(self.service,self.catalog).release(db,operation_id)
            self.catalog.notify()
            return PreparedPlan(plan_id, revision, state, task['id'], params['result'], target, base, incoming, current, candidate, outcome.conflicts)
        except Exception as error:
            failure = classify(error)
            # If failure persistence is itself unavailable, rollback leaves the
            # short claims intact for explicit recovery rather than losing ownership.
            with self.catalog.connect(write=True) as db:
                operation = db.execute('SELECT state FROM operations WHERE id=?', (operation_id,)).fetchone()
                if operation and operation[0] in ('queued', 'running'):
                    self.catalog._transition(db, operation_id, 'failed', error=failure.as_dict())
                    from .integration_claims import IntegrationClaims
                    IntegrationClaims(self.service,self.catalog).release(db,operation_id)
            self.catalog.notify()
            raise failure from error

    @staticmethod
    def _decisions(db, plan, revision, base, current, incoming, outcome):
        b, c, r, m = ({e.path:e for e in entries} for entries in (base, current, incoming, outcome.entries))
        conflicts = {conflict.path:conflict for conflict in outcome.conflicts}
        for ordinal, path in enumerate(sorted(b.keys() | c.keys() | r.keys() | m.keys() | conflicts.keys())):
            conflict = conflicts.get(path)
            if conflict:
                kind = 'conflict'
            elif _entry_key(m.get(path)) == _entry_key(c.get(path)):
                kind = 'keep_current'
            elif m.get(path) is None:
                kind = 'delete'
            elif _entry_key(m.get(path)) == _entry_key(r.get(path)):
                kind = 'take_incoming'
            else:
                kind = 'merged'
            decision = dict(base=entry_json(b.get(path)), current=entry_json(c.get(path)), incoming=entry_json(r.get(path)), candidate=entry_json(m.get(path)))
            db.execute('INSERT INTO integration_decisions VALUES (?,?,?,?,?,?,?)',
                       (plan, revision, ordinal, path, kind, conflict.reason if conflict else None, canonical(decision)))

    def load(self, plan_id: str, revision: int | None = None) -> PreparedPlan:
        require_integrations(self.catalog)
        with self.catalog.connect() as db:
            plan = rowdict(db, 'SELECT * FROM integration_plans WHERE id=?', (plan_id,))
            if plan is None:
                _refuse('INTEGRATION_PLAN_NOT_FOUND', 'The integration plan does not exist.')
            revision = plan['current_revision'] if revision is None else revision
            row = rowdict(db, 'SELECT * FROM integration_revisions WHERE plan_id=? AND revision=?', (plan_id, revision))
            if row is None:
                _refuse('INTEGRATION_REVISION_CONFLICT', 'The requested plan revision does not exist.')
            profile = json.loads(row['profile_json'])
            versions = [self.snapshots._load(db, row[name+'_id'], profile)[0] for name in ('base', 'incoming', 'current', 'candidate')]
            if versions[-1].manifest_sha256 != row['manifest_sha256']:
                _refuse('INTEGRATION_MANIFEST_INVALID', 'The candidate no longer matches its retained digest.')
            expected = json.loads(row['expected_json']); value = expected['target']
            target = Target(**dict(value, identity=Identity(**value['identity'])))
            conflicts = []
            for decision in db.execute("SELECT path,reason,decision_json FROM integration_decisions WHERE plan_id=? AND revision=? AND kind='conflict' ORDER BY path", (plan_id, revision)):
                data = json.loads(decision[2])
                conflicts.append(MergeConflict(decision[0], decision[1], *(entry_from_json(data[name]) for name in ('base', 'current', 'incoming'))))
        retained = PreparedPlan(plan_id, revision, plan['state'], plan['task_id'], plan['result_id'], target, *versions, tuple(conflicts))
        if review_token(retained, expected) != row['review_token']:
            _refuse('INTEGRATION_MANIFEST_INVALID', 'The retained versions no longer match their original review binding.')
        return retained

    def abandon(self, request: dict) -> dict:
        require_integrations(self.catalog)
        params = request['params']
        with self.paths.registry.lock, self.coordinator.lock, self.catalog.connect(write=True) as db:
            cached = self.tasks.lookup_request(request, db)
            if cached is not None:
                return cached
            plan = rowdict(db, 'SELECT * FROM integration_plans WHERE id=?', (params['plan'],))
            if plan is None:
                _refuse('INTEGRATION_PLAN_NOT_FOUND', 'The integration plan does not exist.')
            if plan['current_revision'] != params['revision']:
                _refuse('INTEGRATION_REVISION_CONFLICT', 'The plan revision changed.')
            if plan['state'] not in ('conflicted', 'prepared', 'approved'):
                _refuse('RECOVERY_REQUIRED', 'Only a draft that has not begun application can be abandoned.')
            for journal in db.execute('SELECT * FROM integration_journals WHERE plan_id=?', (plan['id'],)):
                if journal['phase'] not in ('reserved', 'staged') or db.execute("SELECT 1 FROM integration_actions WHERE operation_id=? AND phase!='planned'", (journal['operation_id'],)).fetchone():
                    _refuse('RECOVERY_REQUIRED', 'A journal may have changed destination entries; finish or restore it first.')
                target = json.loads(journal['target_identity_json'])
                if directory_identity(Path(target['path']), self.catalog.policy) != target:
                    _refuse('RECOVERY_REQUIRED', 'The reserved target identity changed.')
                path = Path(journal['journal_path'])
                if journal['identity_json']:
                    if directory_identity(path, self.catalog.policy) != json.loads(journal['identity_json']):
                        _refuse('RECOVERY_REQUIRED', 'The reserved journal identity changed.')
                elif os.path.lexists(path):
                    _refuse('RECOVERY_REQUIRED', 'An unverified entry occupies the reserved journal path.')
                db.execute("UPDATE integration_journals SET phase='abandoned' WHERE operation_id=?", (journal['operation_id'],))
                db.execute('DELETE FROM integration_claims WHERE operation_id=?', (journal['operation_id'],))
                self.tasks.release(db, journal['operation_id'])
            db.execute("UPDATE integration_plans SET state='abandoned',updated_at=? WHERE id=?", (self.catalog._now(), plan['id']))
            result = dict(plan_id=plan['id'], revision=plan['current_revision'], state='abandoned', files_preserved=True)
            self.tasks.event(db, request, 'integration.abandoned', 'succeeded', dict(plan_id=plan['id']))
            self.tasks.save_request(db, request, result)
        self.catalog.notify()
        return result
