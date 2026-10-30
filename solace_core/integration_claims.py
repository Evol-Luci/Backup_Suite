"""Exact manual approval and durable ownership of affected live trees.

Placement mutations use registry.lock, then only this project's coordinator.
Callers of reserve/release own that short critical section and commit before I/O.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import uuid

from solace_engine.repository import ProjectRepository
from .errors import CoreError, classify
from .integration_plans import IntegrationPlans
from .integration_snapshots import canonical
from .integration_types import PreparedPlan, Journal
from .integration_merge import _entry_key
from .schema import require_integrations
from .task_operations import rowdict
from .workspace_paths import directory_identity, overlap, git_control_marker


def stale(message):
    raise CoreError('INTEGRATION_PLAN_STALE', message, 'Prepare and review a fresh plan before approval or application.')


class IntegrationClaims:
    def __init__(self, service, catalog):
        self.service, self.catalog = service, catalog
        self.paths = service.workspace_paths
        self.plans = IntegrationPlans(service, catalog)
        self.tasks = self.plans.tasks
        self.coordinator = self.plans.coordinator
        self._read_owner = ContextVar('integration_read_owner',default=None)

    def assert_access(self, path: Path, *, operation_id: str | None = None) -> None:
        self.paths.assert_access(path, operation_id=operation_id)

    def claim_access(self, db, operation_id, paths):
        """Short-lived reader ownership uses target claims, never a fake journal."""
        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] not in (3, 4, 5):
            for path in paths: self.assert_access(Path(path), operation_id=operation_id)
            return
        for path in dict.fromkeys(str(Path(path).absolute()) for path in paths):
            self.assert_access(Path(path), operation_id=operation_id)
            db.execute('INSERT OR IGNORE INTO integration_claims VALUES (?,?,?,?,?)',
                       (path, operation_id, 'target', None, self.catalog._now()))

    @contextmanager
    def read_access(self, paths):
        """Internal, body-free operation protects a synchronous long live read."""
        request=dict(version=1,id=str(uuid.uuid4()),method='integration.read',actor='core',
                     params=dict(project=self.catalog.registration['id']))
        with self.paths.registry.lock, self.coordinator.lock:
            if self.paths.registry.get(self.catalog.registration['id']) != self.catalog.registration:
                stale('Project registration changed before this synchronous integration read.')
            try:
                operation=self.catalog.accept_operation(request)['operation_id']
            except Exception:
                accepted=self.catalog.find_operation(request)
                if accepted is None: raise
                operation=accepted['operation_id']
            try:
                with self.catalog.connect(write=True) as db:
                    self.claim_access(db,operation,paths)
                    self.catalog._transition(db,operation,'running')
            except Exception:
                self.catalog.transition(operation,'failed',error=CoreError('INTEGRATION_TARGET_BUSY','The read could not reserve its tree.','Inspect ownership.').as_dict())
                raise
        token=self._read_owner.set(operation)
        failure=None
        try:
            yield operation
        except BaseException as error:
            failure=classify(error).as_dict()
            raise
        finally:
            self._read_owner.reset(token)
            with self.catalog.connect(write=True) as db:
                self.release(db,operation)
                self.catalog._transition(db,operation,'failed' if failure else 'succeeded',
                                         result=dict(read_released=True),error=failure)
            self.catalog.notify()

    def release(self, db, operation_id: str) -> None:
        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (3, 4, 5):
            db.execute('DELETE FROM integration_claims WHERE operation_id=?', (operation_id,))
        self.tasks.release(db, operation_id)

    def _rows(self, db, plan):
        header = rowdict(db, 'SELECT * FROM integration_plans WHERE id=?', (plan.id,))
        if header is None or header['current_revision'] != plan.revision or header['state'] not in ('prepared','approved','applying','recovery_required'):
            stale('The plan revision or eligibility changed.')
        row = rowdict(db, 'SELECT * FROM integration_revisions WHERE plan_id=? AND revision=?', (plan.id,plan.revision))
        if row is None: stale('The reviewed revision is unavailable.')
        return header, row

    def _ownership(self, plan, *, operation_id=None):
        root = Path(plan.target.identity.path)
        self.assert_access(root, operation_id=operation_id)
        for record in self.paths.registry.list():
            if record['id'] != self.catalog.registration['id'] and overlap(root, Path(record['root'])):
                raise CoreError('INTEGRATION_SHARED_TARGET', 'The target overlaps another registered source root.',
                                'Separate the registered sources before preparing integration.')
        # Force all retained catalogs to be available, including completed paths.
        self.paths.managed_locations()

    def _check_metadata(self, plan):
        if self.paths.registry.get(self.catalog.registration['id']) != self.catalog.registration:
            stale('Project registration changed after this plan was selected.')
        require_integrations(self.catalog)
        with self.catalog.connect() as db:
            header, row = self._rows(db, plan)
            journal = rowdict(db, 'SELECT operation_id FROM integration_journals WHERE plan_id=? AND revision=? '
                              "AND phase NOT IN ('committed','restored','abandoned')", (plan.id,plan.revision))
            expected = json.loads(row['expected_json'])
            task = self.tasks.row(db, plan.task_id)
            if (task['revision'] != expected['source_revision'] or task['state'] != 'ready'
                    or json.loads(task['profile_json']) != json.loads(row['profile_json'])):
                stale('The source task revision, state or capture profile changed.')
            params = dict(task=plan.task_id,result=plan.result_id,expected_revision=expected['source_revision'])
            if plan.target.task_id: params['expected_target_revision'] = plan.target.revision
            try:
                _, target, base, previous = self.plans._inputs(db, params)
                roots = self.plans._boundaries(journal['operation_id'] if journal else None)
                fresh = self.plans._expected(db, params, target, roots)
            except CoreError as error:
                if error.code in ('STORE_UNAVAILABLE','DATABASE_CORRUPT'): raise
                stale('The target, source result, head or ownership changed.')
            if (target != plan.target or base != plan.base.checkpoint_id or previous != row['previous_receipt_id']
                    or {k:v for k,v in fresh.items() if k!='directory_inventories'} !=
                       {k:v for k,v in expected.items() if k!='directory_inventories'}):
                stale('The reviewed identities, heads, contribution or protected boundaries changed.')
        self._ownership(plan, operation_id=journal['operation_id'] if journal else self._read_owner.get())
        return row, expected, roots

    def validate(self, plan: PreparedPlan, *, scan: bool) -> dict:
        row, expected, roots = self._check_metadata(plan)
        # Central loading authenticates B/R/C/M against the original review token.
        retained = self.plans.load(plan.id, plan.revision)
        if any(getattr(retained,name) != getattr(plan,name) for name in ('base','incoming','current','candidate','target')):
            stale('The supplied plan is not the exact retained review.')
        profile = json.loads(row['profile_json']); root = Path(plan.target.identity.path)
        current = self.plans.snapshots.entries(plan.current)
        candidate = self.plans.snapshots.entries(plan.candidate)
        try:
            self.plans._protected_delta(root,profile,roots,current,candidate)
            inventories = self.plans._inventories(root,current,candidate)
        except CoreError:
            stale('A protected path or directory replacement inventory changed.')
        if inventories != expected['directory_inventories']:
            stale('Directory replacement identities or entries changed.')
        if scan:
            repo = ProjectRepository.open_existing(root,self.catalog.store,scan_only=True)
            observed,warnings = repo.inspect_tree(profile['exclusions'],excluded=self.plans._excluder(root,profile,roots))
            if warnings or {e.path:_entry_key(e) for e in observed} != {e.path:_entry_key(e) for e in current}:
                stale('Destination bytes or modes changed after preparation.')
        return dict(plan=plan.id, revision=plan.revision, previous_receipt_id=row['previous_receipt_id'],
                    review_token=row['review_token'], expected=expected, checks='not_run_by_solace',
                    **{name:asdict(getattr(plan,name)) for name in ('base','incoming','current','candidate')})

    def approve(self, request: dict) -> dict:
        require_integrations(self.catalog); p = request['params']
        # Immutable replay precedes current revision, acknowledgement or live availability.
        with self.catalog.connect() as db:
            cached = self.tasks.lookup_request(request,db)
            if cached is not None: return cached
        checked = p.get('check_run') is not None
        if checked:
            from .schema import require_checks
            require_checks(self.catalog)
        if checked and p.get('accept_unchecked') is True:
            stale('Choose exactly one approval mode.')
        if not checked and p.get('accept_unchecked') is not True:
            raise CoreError('INTEGRATION_UNCHECKED','Solace has not run checks for this candidate.',
                            'Explicitly accept unchecked integration after reviewing this exact revision.')
        plan = self.plans.load(p['plan'],p['revision'])
        if plan.state != 'prepared' or plan.conflicts:
            stale('Only the current prepared, conflict-free revision can be approved.')
        with self.read_access([plan.target.identity.path]):
            binding = self.validate(plan,scan=True)
            if not secrets.compare_digest(p['token'],binding['review_token']):
                stale('The review token does not match this exact revision.')
            with self.paths.registry.lock, self.coordinator.lock:
                with self.catalog.connect() as db:
                    cached = self.tasks.lookup_request(request,db)
                    if cached is not None: return cached
                row,expected,_ = self._check_metadata(plan)
                if row['review_token'] != binding['review_token'] or expected != binding['expected']:
                    stale('The reviewed binding changed during approval.')
                if checked:
                    from .check_approvals import CheckApprovals
                    return CheckApprovals(self.service,self.catalog).approve(request,plan,binding)
                with self.catalog.connect(write=True) as db:
                    self._rows(db,plan)
                    from .policy_approvals import PolicyApprovals
                    policy = PolicyApprovals.stamp(db,None)
                    if policy: binding['policy'] = policy
                    token = secrets.token_hex(32); now = self.catalog._now()
                    db.execute('INSERT INTO integration_approvals VALUES (?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,plan.id,plan.revision,hashlib.sha256(token.encode()).hexdigest(),canonical(binding),request['actor'],1,now))
                    db.execute("UPDATE integration_plans SET state='approved',updated_at=? WHERE id=?", (now,plan.id))
                    db.execute('UPDATE integration_revisions SET validated_at=? WHERE plan_id=? AND revision=?',(now,plan.id,plan.revision))
                    result = dict(plan_id=plan.id,revision=plan.revision,manifest_sha256=plan.candidate.manifest_sha256,
                                  approval=token,checks='not_run_by_solace')
                    self.tasks.event(db,request,'integration.approved','succeeded',dict(plan_id=plan.id,revision=plan.revision))
                    self.tasks.save_request(db,request,result)
        self.catalog.notify()
        return result

    def reserve(self, db, operation_id: str, plan: PreparedPlan, *, kind: str = 'target') -> Journal:
        """Persist reserved-only intent; caller commits before creating the sibling."""
        if kind != 'target': raise ValueError('Integration journal reservations require target ownership.')
        header,row = self._rows(db,plan)
        self._check_metadata(plan)
        self._ownership(plan)
        root = Path(plan.target.identity.path)
        parent = root.parent
        journal = parent/('.solace-integration-'+operation_id)
        # Unlike workspace placement this permits the registered source itself,
        # but never a journal within any source or managed tree.
        parent_identity = directory_identity(parent,self.catalog.policy)
        target_identity = directory_identity(root,self.catalog.policy)
        if target_identity != asdict(plan.target.identity): stale('The target identity changed before reservation.')
        if (root == parent or parent_identity['device'] != target_identity['device']
                or parent_identity['mount_id'] != target_identity['mount_id']
                or not os.access(parent,os.W_OK|os.X_OK) or not parent.stat().st_mode & 0o222):
            raise CoreError('INTEGRATION_JOURNAL_PLACEMENT','The target needs a writable sibling on the same local filesystem.',
                            'Use a target with a writable local parent on its filesystem.')
        self.catalog.policy.check(journal,creating=True)
        # Do not follow substituted ancestors, even if resolve() would find valid storage.
        for path in (root,*root.parents):
            if path.is_symlink(): stale('A target ancestor became a symlink.')
        managed = [path for path,_ in self.paths.managed_locations()]
        # managed_locations includes every retained store and owned path.
        managed += [Path(r['root']) for r in self.paths.registry.list()]
        managed += list(self.paths.config.sync_roots)
        # Workspace parent sharing is allowed; concrete workspaces/intents still block.
        workspace_root = self.paths.config.workspace_root
        if not root.is_relative_to(workspace_root): managed.append(workspace_root)
        managed.append(self.paths.registry.path.parent/'projects')
        if any(overlap(journal,path) for path in managed) or any(git_control_marker(path/'.git') for path in (parent,*parent.parents)):
            raise CoreError('INTEGRATION_JOURNAL_PLACEMENT','The sibling journal overlaps protected ownership.',
                            'Separate source and managed roots before preparing integration.')
        if os.path.lexists(journal):
            raise CoreError('INTEGRATION_JOURNAL_PLACEMENT','The reserved journal name is already occupied.', 'Preserve the existing entry and inspect recovery.')
        revisions = {plan.task_id:json.loads(row['expected_json'])['source_revision']}
        if plan.target.task_id: revisions[plan.target.task_id] = plan.target.revision
        self.tasks.claim(db,operation_id,revisions)
        now = self.catalog._now()
        for path,claim_kind,identity in ((root,'target',target_identity),(journal,'journal',None)):
            db.execute('INSERT INTO integration_claims VALUES (?,?,?,?,?)',
                       (str(path),operation_id,claim_kind,canonical(identity) if identity else None,now))
        intent = dict(expected=json.loads(row['expected_json']),previous_receipt_id=row['previous_receipt_id'],
                      review_token=row['review_token'],parent_identity=parent_identity,
                      **{name:asdict(getattr(plan,name)) for name in ('base','incoming','current','candidate')})
        db.execute('INSERT INTO integration_journals VALUES (?,?,?,?,?,?,?,?)',
                   (operation_id,plan.id,plan.revision,str(journal),None,canonical(target_identity),canonical(intent),'reserved'))
        return Journal(operation_id,plan.id,plan.revision,journal,None,plan.target.identity)
