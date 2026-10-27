"""Publish verified integration metadata in the caller's single write transaction.

No file publication, CAS verification, commit or notification occurs here. The
caller holds the coordinator and has just verified/synced M with the publisher.
"""
from dataclasses import asdict, replace
import hashlib
import json
import uuid

from solace_engine.models import FileEntry
from .errors import CoreError
from .integration_claims import IntegrationClaims, stale
from .integration_plans import IntegrationPlans, review_token
from .integration_publish import IntegrationPublisher
from .integration_snapshots import canonical, entry_json
from .integration_types import Identity, Journal, PreparedPlan, Target, Version
from .lineage import Lineage
from .observations import decision_page, encoded_size, SUMMARY_BYTES
from .task_operations import rowdict
from .workspace_paths import directory_identity


class IntegrationCommit:
    def __init__(self, service, catalog):
        self.service, self.catalog = service, catalog
        self.claims = IntegrationClaims(service, catalog)
        self.plans, self.tasks = self.claims.plans, self.claims.tasks

    @staticmethod
    def latest_application(db, task_id: str, target_key: str) -> dict | None:
        """Application of the latest immutable result, independent of raw state."""
        latest = IntegrationPlans._latest(db, task_id)
        if latest is None: return None
        receipt = db.execute('SELECT id FROM integration_receipts WHERE task_id=? AND result_id=? AND target_key=?',
                             (task_id, latest, target_key)).fetchone()
        return dict(result_id=latest, target_key=target_key, applied=receipt is not None,
                    receipt_id=receipt[0] if receipt else None)

    @staticmethod
    def _operation(db, journal, operation_id):
        operation = rowdict(db, 'SELECT * FROM operations WHERE id=?', (operation_id,))
        original = rowdict(db, 'SELECT * FROM operations WHERE id=?', (journal.operation_id,))
        if operation is None or original is None or original['kind'] != 'integration.apply':
            stale('The publication operation is not the reserved apply.')
        if operation_id != journal.operation_id:
            recovery = rowdict(db, 'SELECT * FROM integration_recoveries WHERE operation_id=?', (operation_id,))
            if (operation['kind'] != 'integration.recovery.resume' or not recovery or
                    recovery['original_operation_id'] != journal.operation_id or recovery['mode'] != 'resume' or
                    original['state'] != 'interrupted'):
                stale('The recovery operation does not resume this interrupted apply.')
        return operation

    def _metadata(self, db, journal, row):
        intent = json.loads(row['intent_json'])
        header = rowdict(db, 'SELECT * FROM integration_plans WHERE id=?', (journal.plan_id,))
        revision = rowdict(db, 'SELECT * FROM integration_revisions WHERE plan_id=? AND revision=?',
                           (journal.plan_id,journal.revision))
        if (header is None or revision is None or header['current_revision'] != journal.revision or
                header['state'] not in ('applying','recovery_required') or row['phase'] != 'files_ready' or
                row['plan_id'] != journal.plan_id or row['revision'] != journal.revision or
                row['journal_path'] != str(journal.path) or journal.identity is None or
                json.loads(row['identity_json'] or 'null') != asdict(journal.identity) or
                json.loads(row['target_identity_json']) != asdict(journal.target)):
            stale('Publication requires this exact verified files-ready journal and revision.')
        expected = json.loads(revision['expected_json'])
        from .integration_recovery import IntegrationRecovery
        physical = IntegrationRecovery(self.service,self.catalog).physical_intent(intent,journal)
        physical_expected = physical['expected']
        value = expected['target']; target = Target(**dict(value, identity=Identity(**value['identity'])))
        versions = [Version(**intent[name]) for name in ('base','incoming','current','candidate')]
        plan = PreparedPlan(header['id'],journal.revision,header['state'],header['task_id'],header['result_id'],target,*versions,())
        binding = dict(plan=plan.id,revision=plan.revision,previous_receipt_id=revision['previous_receipt_id'],
                       review_token=revision['review_token'],expected=expected,checks='not_run_by_solace',
                       **{name:asdict(getattr(plan,name)) for name in ('base','incoming','current','candidate')})
        if (intent['expected'] != expected or intent['review_token'] != revision['review_token'] or
                intent['previous_receipt_id'] != revision['previous_receipt_id'] or
                intent.get('operation_id') != journal.operation_id or intent.get('plan_id') != plan.id or
                intent.get('revision') != plan.revision or physical.get('target') != asdict(journal.target) or
                physical.get('journal') != asdict(journal.identity) or physical_expected['target']['identity'] != asdict(journal.target) or
                header['target_key'] != target.key or header['target_task_id'] != target.task_id or
                review_token(plan,expected) != revision['review_token'] or
                plan.candidate.manifest_sha256 != revision['manifest_sha256'] or
                plan.incoming.checkpoint_id != plan.result_id):
            stale('The journal no longer matches its exact reviewed version binding.')
        selected = intent.get('approval')
        if isinstance(selected,dict) and isinstance(selected.get('binding',{}).get('checks'),dict):
            from .check_approvals import CheckApprovals
            CheckApprovals.verify(db,selected,plan)
            if {**selected['binding'], 'checks':'not_run_by_solace'} != binding:
                stale('The checked approval differs from the reviewed binding.')
        else:
            approval = rowdict(db,'SELECT * FROM integration_approvals WHERE id=?',(selected.get('id'),)) if isinstance(selected,dict) else None
            if (not approval or selected != dict(id=approval['id'],token_sha256=approval['token_sha256'],binding=json.loads(approval['binding_json'])) or
                    selected['binding'] != binding or approval['plan_id'] != plan.id or approval['revision'] != plan.revision or
                    approval['accepted_unchecked'] != 1):
                stale('The selected approval does not authorize this exact publication.')
        target = replace(target, identity=journal.target)
        plan = replace(plan, target=target)
        task = self.tasks.row(db,plan.task_id)
        if task['revision'] != expected['source_revision'] or json.loads(task['profile_json']) != json.loads(revision['profile_json']):
            stale('The source task revision or profile changed.')
        params = dict(task=plan.task_id,result=plan.result_id,expected_revision=expected['source_revision'])
        if target.task_id: params['expected_target_revision'] = target.revision
        _, fresh_target, base, previous = self.plans._inputs(db,params)
        roots = self.plans._boundaries(journal.operation_id)
        fresh = self.plans._expected(db,params,fresh_target,roots)
        if (fresh_target != target or base != plan.base.checkpoint_id or previous != revision['previous_receipt_id'] or
                {k:v for k,v in fresh.items() if k!='directory_inventories'} !=
                {k:v for k,v in physical_expected.items() if k!='directory_inventories'}):
            stale('Pinned destination identities, heads or contribution ancestry changed.')
        if directory_identity(journal.path,self.catalog.policy) != asdict(journal.identity):
            stale('The verified journal identity changed.')
        self.claims._ownership(plan,operation_id=journal.operation_id)
        owned_tasks = {r[0] for r in db.execute('SELECT task_id FROM task_claims WHERE operation_id=?',(journal.operation_id,))}
        if owned_tasks != {plan.task_id, *([target.task_id] if target.task_id else [])}:
            stale('The apply no longer owns its exact affected tasks.')
        claims = {r['kind']:dict(r) for r in db.execute('SELECT * FROM integration_claims WHERE operation_id=?',(journal.operation_id,))}
        if len(claims) != 2 or db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?',(journal.operation_id,)).fetchone()[0] != 2:
            stale('The apply no longer owns its exact affected paths.')
        for kind, identity in (('target',journal.target),('journal',journal.identity)):
            claim = claims.get(kind)
            if not claim or claim['path'] != identity.path or json.loads(claim['identity_json'] or 'null') != asdict(identity):
                stale('The affected path claim no longer matches its verified identity.')
        actions = [dict(r) for r in db.execute('SELECT * FROM integration_actions WHERE operation_id=? ORDER BY ordinal',(journal.operation_id,))]
        IntegrationPublisher._validate_actions(intent,actions)
        if any(action['phase'] != 'done' for action in actions):
            stale('Publication still has unfinished file actions.')
        if db.execute("SELECT 1 FROM integration_decisions WHERE plan_id=? AND revision=? AND kind='conflict'",(plan.id,plan.revision)).fetchone():
            stale('The reviewed candidate still contains conflicts.')
        # Match all four SQL manifests to retained immutable decision metadata.
        # This reads no CAS bytes and never recaptures or recomputes M.
        decisions = [json.loads(r[0]) for r in db.execute('SELECT decision_json FROM integration_decisions WHERE plan_id=? AND revision=? ORDER BY path', (plan.id,plan.revision))]
        purpose = 'workspace' if target.task_id else 'source'
        for name in ('base','incoming','current','candidate'):
            version = getattr(plan,name)
            if version.checkpoint_id != revision[name+'_id'] or version.profile_json != canonical(json.loads(revision['profile_json'])):
                stale('A pinned version or capture profile changed.')
            cp = rowdict(db,'SELECT * FROM checkpoints WHERE id=?',(version.checkpoint_id,))
            context = rowdict(db,'SELECT * FROM checkpoint_context WHERE checkpoint_id=?',(version.checkpoint_id,))
            entries = [entry_json(FileEntry(r['path'],r['kind'],r['mode'],r['size'],r['file_digest'],tuple(c[0] for c in db.execute(
                'SELECT digest FROM chunk_refs WHERE checkpoint_id=? AND path=? ORDER BY ordinal',(version.checkpoint_id,r['path'])))))
                for r in db.execute('SELECT * FROM file_entries WHERE checkpoint_id=? ORDER BY path',(version.checkpoint_id,))]
            retained = [d[name] for d in decisions if d[name] is not None]
            if (not cp or not context or canonical(entries) != canonical(retained) or json.loads(context['profile_json']) != json.loads(version.profile_json) or
                    cp['file_count'] != len(entries) or cp['logical_bytes'] != sum(e['size'] for e in entries) or json.loads(cp['warnings_json'])):
                stale('A pinned immutable manifest or context changed.')
            if name in ('current','candidate'):
                if (canonical(entries) != canonical(intent[name+'_entries']) or context['purpose'] != purpose or context['published'] != 0 or
                        context['task_id'] != target.task_id or context['workspace_id'] != (target.key.split(':',1)[1] if target.task_id else None) or
                        cp['parent_id'] != (target.head if name=='current' else plan.current.checkpoint_id)):
                    stale('The before/after contexts or explicit ancestry changed.')
        return plan, revision, intent

    def finish(self, db, journal: Journal, operation_id: str) -> dict:
        if not db.in_transaction:
            raise CoreError('INVALID_REQUEST','Publication requires the caller’s write transaction.','Hold the coordinator and open one catalog write transaction.')
        operation = self._operation(db,journal,operation_id)
        if operation['state'] == 'succeeded': return json.loads(operation['result_json'])
        if operation['state'] != 'running': stale('Only a running apply or linked resume may publish success.')
        row = rowdict(db,'SELECT * FROM integration_journals WHERE operation_id=?',(journal.operation_id,))
        if row is None: stale('The reserved journal no longer exists.')
        plan, revision, intent = self._metadata(db,journal,row)
        existing = db.execute('SELECT id FROM integration_receipts WHERE task_id=? AND result_id=? AND target_key=?',
                              (plan.task_id,plan.result_id,plan.target.key)).fetchone()
        if existing: stale('This result already has an application receipt; replay that receipt before file publication.')
        before, after = plan.current.checkpoint_id, plan.candidate.checkpoint_id
        now = self.catalog._now(); receipt_id = uuid.uuid4().hex
        Lineage(self.catalog).publish_integration(db,before,after,plan.target)
        if plan.target.task_id: self.tasks.receive_integration(db,plan.target.task_id,before,after,now)
        for checkpoint, role in ((before,'before'),(after,'after')):
            db.execute('INSERT INTO integration_refs VALUES (?,?,?,?)',(checkpoint,receipt_id,role,now))
        # Read the event sequence from the insertion in this same transaction.
        self.catalog._event(db,operation,'integration.applied','succeeded',dict(receipt_id=receipt_id,plan_id=plan.id,revision=plan.revision))
        event_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        details = dict(project_id=self.catalog.registration['id'],actor=operation['actor'],event_id=event_id,
                       original_operation_id=journal.operation_id,approval=intent['approval'],checks=intent['approval']['binding']['checks'],
                       previous_receipt_id=revision['previous_receipt_id'],target=asdict(plan.target),
                       profile=json.loads(revision['profile_json']),protected_roots=intent['expected']['protected_roots'],
                       decisions=dict(plan_id=plan.id,revision=plan.revision),
                       **{name:asdict(getattr(plan,name)) for name in ('base','incoming','current','candidate')})
        db.execute('INSERT INTO integration_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (receipt_id,plan.id,plan.revision,plan.task_id,plan.result_id,plan.target.key,operation_id,before,after,canonical(details),now))
        db.execute("UPDATE integration_journals SET phase='committed' WHERE operation_id=?",(journal.operation_id,))
        db.execute("UPDATE integration_plans SET state='applied',updated_at=? WHERE id=?",(now,plan.id))
        result = dict(receipt_id=receipt_id,plan_id=plan.id,revision=plan.revision,task_id=plan.task_id,
                      result_id=plan.result_id,target_key=plan.target.key,before_id=before,after_id=after,
                      operation_id=operation_id,event_id=event_id,checks=intent['approval']['binding']['checks'])
        self.catalog._transition(db,operation_id,'succeeded',result=result)
        if operation_id != journal.operation_id:
            db.execute('UPDATE integration_recoveries SET result_json=? WHERE operation_id=?',(canonical(result),operation_id))
        self.claims.release(db,journal.operation_id)
        return result

    def receipt(self, receipt_id: str, *, after: int = 0, limit: int = 128) -> dict:
        with self.catalog.connect(metadata=True) as db:
            row = rowdict(db,'SELECT * FROM integration_receipts WHERE id=?',(receipt_id,))
            if row is None:
                raise CoreError('INTEGRATION_RECEIPT_NOT_FOUND','The retained receipt does not exist.','Use a receipt ID returned by this project.')
            details = json.loads(row.pop('details_json'))
            row['receipt_id'] = row.pop('id')
            # Exact approval binding is retained at rest. Ordinary pages expose
            # selected provenance without duplicating its inventories/bindings.
            selected = details['approval']
            public = {k:details[k] for k in ('project_id','actor','event_id','original_operation_id','checks','previous_receipt_id','profile','protected_roots','base','incoming','current','candidate')}
            public['approval'] = dict(id=selected['id'],token_sha256=selected['token_sha256'],
                                      binding_sha256=hashlib.sha256(canonical(selected['binding']).encode()).hexdigest())
            public.update(target_head=details['target']['head'],target_task_id=details['target']['task_id'])
            result = dict(row,**public)
            if encoded_size(result) > SUMMARY_BYTES:
                raise CoreError('RESPONSE_TOO_LARGE','The receipt summary exceeds its byte limit.','Inspect the retained receipt locally or export its plan.')
            return decision_page(db,row['plan_id'],row['revision'],after=after,limit=limit,summary_fields=result)
