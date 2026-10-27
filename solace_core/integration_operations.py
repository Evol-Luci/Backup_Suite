"""Durable integration dispatch, FIFO acceptance and stage-aware publication outcomes."""
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import secrets
import uuid

from .errors import CoreError, classify
from .integration_claims import IntegrationClaims, stale
from .integration_commit import IntegrationCommit
from .integration_plans import IntegrationPlans
from .integration_publish import IntegrationPublisher
from .integration_recovery import IntegrationRecovery
from .integration_review import IntegrationReview
from .integration_snapshots import canonical
from .protocol import fingerprint
from .schema import require_integrations
from .tasks import TaskCatalog
from .workspace_paths import directory_identity

log = logging.getLogger(__name__)
_ASYNC = {'integration.prepare','integration.export','integration.apply',
          'integration.recovery.resume','integration.recovery.restore-before'}
_READS = {'integration.show','integration.diff','integration.receipt','integration.recovery.show'}


class IntegrationOperations:
    def __init__(self, service, catalog):
        self.service, self.catalog = service, catalog
        self.project = catalog.registration['id']
        self.coordinator = service.coordinator(self.project)
        self.tasks = TaskCatalog(catalog)
        self.plans = IntegrationPlans(service,catalog)
        self.claims = IntegrationClaims(service,catalog)
        self.review = IntegrationReview(service,catalog)

    def _notify(self):
        try: self.catalog.notify()
        except Exception as error:
            log.error('integration notification unavailable exception=%s',type(error).__name__)

    def _registered(self):
        if self.service.registry.get(self.project) != self.catalog.registration:
            stale('Project registration changed before this fresh integration work.')

    def dispatch(self, request: dict, *, registered: bool) -> dict:
        method, p = request['method'], request['params']
        # Metadata-only replay is deliberately before registration, revision or CAS.
        if method not in _READS:
            existing = self.catalog.find_operation(request)
            if existing is not None: return existing
        require_integrations(self.catalog)
        if method=='integration.show': return self.review.show(p['plan'])
        if method=='integration.diff': return self.review.diff(p)
        if method=='integration.receipt': return IntegrationCommit(self.service,self.catalog).receipt(p['receipt'],after=p.get('after',0),limit=p.get('limit',128))
        if method=='integration.recovery.show': return IntegrationRecovery(self.service,self.catalog).show(p['operation'],after=p.get('after',0),limit=p.get('limit',128))
        if not registered:
            raise CoreError('PROJECT_NOT_FOUND','The project is no longer registered.','Retained metadata is readable; register before new integration mutations.')
        with self.coordinator.lock:
            self._registered()
            self.service._recover_pending(self.project,self.catalog)
        if method in _ASYNC: return self.accept(request)
        handlers = {'integration.resolve':self.review.resolve,'integration.approve':self.claims.approve,
                    'integration.abandon':self.plans.abandon}
        try:
            if method=='integration.abandon':
                with self.service.registry.lock, self.coordinator.lock:
                    self._registered()
                    return self.plans.abandon(request)
            return handlers[method](request)
        except Exception:
            # A notify/reply exception after commit cannot erase a synchronous receipt.
            existing = self.catalog.find_operation(request)
            if existing is not None: return existing
            raise

    def _insert(self, db, operation, request, context):
        now = self.catalog._now()
        db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (operation,request['id'],fingerprint(request['method'],request['params'],request['actor']),
                    request['method'],request['actor'],'queued',now,now,None,None,canonical(context)))
        self.catalog._event(db,dict(id=operation,actor=request['actor']),'operation.queued','queued',{})

    @staticmethod
    def _approval(db, plan_id, revision, raw):
        digest = hashlib.sha256(raw.encode()).hexdigest()
        row = db.execute('SELECT * FROM integration_approvals WHERE token_sha256=?',(digest,)).fetchone()
        if (row is None or row['plan_id']!=plan_id or row['revision']!=revision or row['accepted_unchecked']!=1
                or not secrets.compare_digest(row['token_sha256'],digest)):
            stale('The supplied approval does not authorize this plan revision.')
        return dict(id=row['id'],token_sha256=row['token_sha256'],binding=json.loads(row['binding_json']))

    def accept(self, request: dict) -> dict:
        existing = self.catalog.find_operation(request)
        if existing is not None: return existing
        require_integrations(self.catalog)
        method, p = request['method'], request['params']
        if method.startswith('integration.recovery.'):
            try: accepted = IntegrationRecovery(self.service,self.catalog).accept(request)
            except Exception:
                accepted = self.catalog.find_operation(request)
                if accepted is None: raise
            operation = accepted['operation_id']
            self.coordinator.submit(operation,lambda:self.run(operation,request))
            return accepted
        # Authenticate immutable apply data outside short placement/SQL locks.
        plan = binding = None
        if method=='integration.apply':
            with self.catalog.connect(metadata=True) as db:
                historical = db.execute('SELECT id FROM integration_receipts WHERE plan_id=? AND revision=?',(p['plan'],p['revision'])).fetchone()
            if not historical:
                plan = self.plans.load(p['plan'],p['revision'])
                if plan.state!='approved' or plan.conflicts: stale('Only the current approved conflict-free plan can be applied.')
                binding = self.claims.validate(plan,scan=False)
        with self.service.registry.lock, self.coordinator.lock, self.catalog.connect(write=True,metadata=True) as db:
            existing = self.tasks.lookup_request(request,db)
            if existing is not None: return existing
            self._registered()
            operation = uuid.uuid4().hex
            context = {k:p[k] for k in ('project','task','result','plan','revision','expected_revision','expected_target_revision','destination','writers_paused') if k in p}
            if method=='integration.apply':
                if p.get('writers_paused') is not True:
                    raise CoreError('CONFIRMATION_REQUIRED','Application requires paused external writers.','Pause writers and acknowledge it on this request.')
                selected = self._approval(db,p['plan'],p['revision'],p['approval'])
                receipt = db.execute('SELECT id,details_json FROM integration_receipts WHERE plan_id=? AND revision=?',(p['plan'],p['revision'])).fetchone()
                if receipt:
                    if selected['binding'] != json.loads(receipt['details_json'])['approval']['binding']:
                        stale('This approval differs from the retained historical application binding.')
                    context['receipt_id'] = receipt['id']
                    self._insert(db,operation,request,context)
                else:
                    if plan is None or selected['binding'] != binding:
                        stale('The supplied approval binding differs from the exact reviewed candidate.')
                    header,_ = self.claims._rows(db,plan)
                    if header['state']!='approved': stale('The current plan is no longer approved.')
                    self._insert(db,operation,request,context)
                    self.claims.reserve(db,operation,plan)
                    intent = json.loads(db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?',(operation,)).fetchone()[0])
                    intent['approval'] = selected
                    db.execute('UPDATE integration_journals SET intent_json=? WHERE operation_id=?',(canonical(intent),operation))
            elif method=='integration.prepare':
                # Historical contribution lookup precedes task revisions/live trees.
                task = self.tasks.row(db,p['task'])
                if bool(task['parent_id']) != ('expected_target_revision' in p):
                    raise CoreError('INVALID_REQUEST','The target revision field does not match retained task ownership.',
                                    'Supply expected_target_revision for a child and omit it for a root task.')
                workspace = db.execute('SELECT id FROM workspaces WHERE task_id=?',(task['parent_id'],)).fetchone() if task['parent_id'] else None
                key = 'workspace:'+workspace[0] if workspace else 'source'
                receipt = db.execute('SELECT id FROM integration_receipts WHERE task_id=? AND result_id=? AND target_key=?',(p['task'],p['result'],key)).fetchone()
                if receipt:
                    context['receipt_id'] = receipt['id'];self._insert(db,operation,request,context)
                else:
                    task,target,_,_ = self.plans._inputs(db,p)
                    revisions = {task['id']:task['revision']}
                    if target.task_id: revisions[target.task_id] = target.revision
                    context.update(target=asdict(target),store_identity=directory_identity(self.catalog.store,self.catalog.policy))
                    self._insert(db,operation,request,context)
                    self.tasks.claim(db,operation,revisions)
                    self.claims.claim_access(db,operation,[target.identity.path])
            elif method=='integration.export':
                self.catalog.validate(require_root=False)
                self._insert(db,operation,request,context)
                context['destination_parent'] = self.review.reserve_export(db,operation,p)
                db.execute('UPDATE operations SET params_json=? WHERE id=?',(canonical(context),operation))
            else:
                raise CoreError('INVALID_REQUEST','Unknown integration operation.','Use a documented integration method.')
        self._notify()
        self.coordinator.submit(operation,lambda:self.run(operation,request))
        return dict(operation_id=operation)

    def _check_prepare(self, db, operation, p, context):
        task,target,_,_ = self.plans._inputs(db,p)
        if asdict(target)!=context['target'] or directory_identity(self.catalog.store,self.catalog.policy)!=context['store_identity']:
            stale('Preparation differs from its exact accepted target/store context.')
        expected = {task['id'],*([target.task_id] if target.task_id else [])}
        owned = {r[0] for r in db.execute('SELECT task_id FROM task_claims WHERE operation_id=?',(operation,))}
        if owned!=expected: stale('Preparation no longer owns its accepted tasks.')
        paths = {(r['path'],r['kind']) for r in db.execute('SELECT path,kind FROM integration_claims WHERE operation_id=?',(operation,))}
        if paths!={(target.identity.path,'target')}: stale('Preparation no longer owns its accepted target.')
        self.claims.assert_access(Path(target.identity.path),operation_id=operation)

    def run(self, operation_id: str, request: dict) -> None:
        try:
            if self.catalog.find_operation(request)!=dict(operation_id=operation_id):
                raise CoreError('REQUEST_ID_REUSED','Worker request differs from durable acceptance.','Inspect the original operation.')
            with self.service.registry.lock, self.coordinator.lock, self.catalog.connect(write=True,metadata=True) as db:
                row = db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
                if row['state'] not in ('queued','running'): return
                context = json.loads(row['params_json'])
                self._registered()
                if row['state']=='queued': self.catalog._transition(db,operation_id,'running')
                if 'receipt_id' in context:
                    receipt = db.execute('SELECT plan_id,revision FROM integration_receipts WHERE id=?',(context['receipt_id'],)).fetchone()
                    result = dict(receipt_id=context['receipt_id'],plan_id=receipt['plan_id'],revision=receipt['revision'],already_applied=True)
                    self.catalog._transition(db,operation_id,'succeeded',result=result)
                    return
                method,p = request['method'],request['params']
                if method=='integration.prepare': self._check_prepare(db,operation_id,p,context)
            if method=='integration.prepare': self.plans.prepare(operation_id,request)
            elif method=='integration.export': self.review.export(operation_id,request)
            elif method.startswith('integration.recovery.'):
                IntegrationRecovery(self.service,self.catalog).run(operation_id,request)
            elif method=='integration.apply':
                # Exact selected provenance was committed with ownership before scheduling.
                plan = self.plans.load(p['plan'],p['revision'])
                with self.catalog.connect() as db:
                    journal = db.execute('SELECT * FROM integration_journals WHERE operation_id=?',(operation_id,)).fetchone()
                    selected = self._approval(db,p['plan'],p['revision'],p['approval'])
                    if journal is None or json.loads(journal['intent_json']).get('approval')!=selected:
                        stale('Application lost its selected approval reservation.')
                    revisions={plan.task_id,*([plan.target.task_id] if plan.target.task_id else [])}
                    owned={r[0] for r in db.execute('SELECT task_id FROM task_claims WHERE operation_id=?',(operation_id,))}
                    path_rows=list(db.execute('SELECT * FROM integration_claims WHERE operation_id=?',(operation_id,)))
                    paths={r['kind']:r['path'] for r in path_rows}
                    if owned!=revisions or len(path_rows)!=2 or paths!={'target':plan.target.identity.path,'journal':journal['journal_path']}:
                        stale('Application lost its exact accepted ownership.')
                if p.get('writers_paused') is not True: stale('Application has no fresh writer acknowledgement.')
                publisher = IntegrationPublisher(self.service,self.catalog)
                staged = publisher.stage(operation_id,plan)
                publisher.publish(staged)
                publisher.verify_final(staged)
                with self.service.registry.lock, self.coordinator.lock, self.catalog.connect(write=True) as db:
                    IntegrationCommit(self.service,self.catalog).finish(db,staged,operation_id)
            self._notify()
        except Exception as error:
            self._fail(operation_id,error)

    def _fail(self, operation, error):
        try:
            with self.coordinator.lock, self.catalog.connect(write=True,metadata=True) as db:
                row = db.execute('SELECT state FROM operations WHERE id=?',(operation,)).fetchone()
                if row is None or row[0] not in ('queued','running'): return
                journal = db.execute("SELECT plan_id,identity_json FROM integration_journals WHERE operation_id=? AND phase NOT IN ('committed','restored','abandoned')",(operation,)).fetchone()
                recovery = db.execute('SELECT 1 FROM integration_recoveries WHERE operation_id=?',(operation,)).fetchone()
                export = db.execute("SELECT 1 FROM integration_exports WHERE operation_id=? AND phase!='published'",(operation,)).fetchone()
                uncertain = journal or recovery or export
                if journal:
                    db.execute("UPDATE integration_plans SET state='recovery_required' WHERE id=?",(journal['plan_id'],))
                    db.execute("UPDATE integration_journals SET phase=CASE WHEN identity_json IS NULL THEN 'reserved' ELSE 'recovery_required' END WHERE operation_id=?",(operation,))
                self.catalog._transition(db,operation,'interrupted' if uncertain else 'failed',error=classify(error).as_dict())
                if not uncertain: self.claims.release(db,operation)
            self._notify()
        except Exception as persist_error:
            self.service._pending_recovery.add(self.project)
            log.error('integration outcome unavailable id=%s exception=%s',operation,type(persist_error).__name__)
