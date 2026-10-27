"""Single caller transaction publishes already verified files and retained evidence."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from integration_fixture import IntegrationCase
from solace_core.errors import CoreError
from solace_core.integration_claims import IntegrationClaims
from solace_core.integration_publish import IntegrationPublisher
from solace_core.integration_review import IntegrationReview
from solace_core.lineage import Lineage


class InjectedFailure(Exception): pass


class FailAfterStatement:
    def __init__(self, db, at=None):
        self.db, self.at, self.count = db, at, 0
    def __getattr__(self, name): return getattr(self.db, name)
    def execute(self, sql, args=()):
        result = self.db.execute(sql, args)
        if sql.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
            self.count += 1
            if self.count == self.at: raise InjectedFailure(sql)
        return result


class CommitTests(IntegrationCase):
    def committer(self):
        from solace_core.integration_commit import IntegrationCommit
        return IntegrationCommit(self.service, self.catalog)

    def stage(self, plan, *, verify=True):
        claims = IntegrationClaims(self.service, self.catalog)
        review = IntegrationReview(self.service, self.catalog).show(plan.id)
        approval = claims.approve(self.request('integration.approve', plan=plan.id,
            revision=plan.revision, token=review['review_token'], accept_unchecked=True))
        request = self.request('integration.apply', plan=plan.id, revision=plan.revision,
                               approval=approval['approval'], writers_paused=True)
        operation = self.catalog.accept_operation(request)['operation_id']
        self.catalog.transition(operation, 'running')
        claims.validate(plan, scan=True)
        with self.registry.lock, self.service.coordinator(self.project).lock, self.catalog.connect(write=True) as db:
            journal = claims.reserve(db, operation, plan)
            selected = dict(db.execute('SELECT * FROM integration_approvals WHERE token_sha256=?',
                (hashlib.sha256(approval['approval'].encode()).hexdigest(),)).fetchone())
            intent = json.loads(db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?', (operation,)).fetchone()[0])
            intent['approval'] = dict(id=selected['id'], token_sha256=selected['token_sha256'], binding=json.loads(selected['binding_json']))
            db.execute('UPDATE integration_journals SET intent_json=? WHERE operation_id=?', (json.dumps(intent), operation))
        publisher = IntegrationPublisher(self.service, self.catalog)
        journal = publisher.stage(operation, plan)
        publisher.publish(journal)
        if verify: publisher.verify_final(journal)
        return journal

    def root_plan(self, **params):
        task = self.create_task(**params)
        workspace = Path(self.start(task)['path'])
        (workspace/'file').write_bytes(b'incoming')
        ready = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        self.assertEqual(ready['state'], 'succeeded', ready)
        return self.prepare_direct(task, ready['result']['checkpoint_id'])

    def finish(self, journal, operation=None):
        with self.service.coordinator(self.project).lock, self.catalog.connect(write=True) as db:
            return self.committer().finish(db, journal, operation or journal.operation_id)

    def state(self):
        tables = ('source_head','workspaces','tasks','checkpoint_context','task_checkpoint_refs',
                  'integration_refs','integration_receipts','integration_journals','integration_plans',
                  'integration_claims','task_claims','operations','events','integration_recoveries')
        with self.catalog.connect() as db:
            return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in tables}

    def assert_sql_rollback(self,plan,journal,operation=None):
        baseline = self.state(); operation = operation or journal.operation_id
        with self.catalog.connect(write=True) as db:
            probe = FailAfterStatement(db)
            self.committer().finish(probe, journal, operation)
            statement_count = probe.count
            db.rollback()
        self.assertGreater(statement_count, 10)
        for index in range(1, statement_count+1):
            with self.subTest(statement=index), self.assertRaises(InjectedFailure):
                with self.service.coordinator(self.project).lock, self.catalog.connect(write=True) as db:
                    self.committer().finish(FailAfterStatement(db,index), journal, operation)
            self.assertEqual(self.state(), baseline)
        return self.finish(journal,operation)

    def test_files_ready_is_not_history_and_every_sql_statement_rolls_back(self):
        plan = self.root_plan(); journal = self.stage(plan)
        self.assertEqual(Lineage(self.catalog).source_head(), plan.target.head)
        self.assertEqual(self.state()['integration_receipts'], [])
        result = self.assert_sql_rollback(plan,journal)
        self.assertEqual(Lineage(self.catalog).source_head(), result['after_id'])

    def test_parent_lifecycle_and_workspace_statements_roll_back_together(self):
        parent, plan = self.child_plan('ready'); journal = self.stage(plan)
        result = self.assert_sql_rollback(plan,journal)
        self.assertEqual(self.show(parent)['workspace']['head_checkpoint'],result['after_id'])

    def test_success_records_exact_history_approval_decisions_event_and_releases_claims(self):
        (self.root/'keep.tmp').write_bytes(b'excluded')
        plan = self.root_plan(exclusions=['*.tmp']); journal = self.stage(plan)
        with self.catalog.connect() as db:
            selected = json.loads(db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?', (journal.operation_id,)).fetchone()[0])['approval']
        result = self.finish(journal)
        receipt = self.committer().receipt(result['receipt_id'])
        self.assertEqual((result['before_id'], result['after_id']), (plan.current.checkpoint_id,plan.candidate.checkpoint_id))
        self.assertEqual({k:receipt['approval'][k] for k in ('id','token_sha256')}, {k:selected[k] for k in ('id','token_sha256')})
        self.assertEqual(receipt['checks'], 'not_run_by_solace')
        self.assertEqual(receipt['profile'], json.loads(plan.candidate.profile_json))
        self.assertEqual(receipt['protected_roots'], selected['binding']['expected']['protected_roots'])
        self.assertTrue(receipt['entries'])
        self.assertNotIn('candidate_entries', json.dumps(receipt))
        with self.catalog.connect() as db:
            cps = [dict(db.execute('SELECT * FROM checkpoints WHERE id=?',(cp,)).fetchone()) for cp in (result['before_id'],result['after_id'])]
            self.assertEqual([cp['parent_id'] for cp in cps], [plan.target.head,result['before_id']])
            contexts = [dict(db.execute('SELECT * FROM checkpoint_context WHERE checkpoint_id=?',(cp,)).fetchone()) for cp in (result['before_id'],result['after_id'])]
            self.assertTrue(all(row['purpose']=='source' and row['published']==1 for row in contexts))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_claims').fetchone()[0],0)
            event = dict(db.execute('SELECT * FROM events WHERE seq=?',(receipt['event_id'],)).fetchone())
            self.assertEqual(event['operation_id'], journal.operation_id)
            details = json.loads(db.execute('SELECT details_json FROM integration_receipts').fetchone()[0])
            self.assertEqual(details['decisions'],dict(plan_id=plan.id,revision=plan.revision))
            self.assertNotIn('entries', details)
        self.assertEqual((self.root/'keep.tmp').read_bytes(), b'excluded')
        self.assertEqual(self.show(plan.task_id)['latest_result_application']['receipt_id'], result['receipt_id'])

    def child_plan(self, state):
        parent = self.create_task(); self.start(parent)
        child, result, _ = self.ready_task({'file':b'child'}, parent=parent)
        if state == 'ready':
            done = self.wait_operation(self.mutate('task.ready', parent)['operation_id'])
            self.assertEqual(done['state'],'succeeded',done)
        elif state == 'blocked': self.mutate('task.block',parent,reason='waiting on review')
        return parent, self.prepare_direct(child,result)

    def child_lifecycle(self, state):
        parent, plan = self.child_plan(state)
        old = self.show(parent); child = self.show(plan.task_id)
        source_head = Lineage(self.catalog).source_head()
        self.assertEqual(old['children_application'],dict(ready=1,applied=0,unapplied=1))
        self.assertEqual(old['aggregate'], 'blocked' if state=='blocked' else 'awaiting_integration')
        result = self.finish(self.stage(plan))
        new = self.show(parent); incoming = self.show(plan.task_id)
        self.assertEqual(new['state'], 'active' if state=='ready' else state)
        self.assertEqual(new['revision'],old['revision']+1)
        self.assertEqual(new['block_reason'],old['block_reason'])
        self.assertEqual(new['results'],old['results'])
        self.assertEqual(new['children_application'],dict(ready=1,applied=1,unapplied=0))
        self.assertNotEqual(new['aggregate'],'awaiting_integration')
        self.assertEqual(new['workspace']['head_checkpoint'],result['after_id'])
        self.assertIsNone(new['workspace']['last_observed'])
        self.assertEqual(incoming['state'],'ready')
        self.assertEqual(incoming['revision'],child['revision'])
        self.assertEqual(incoming['workspace']['head_checkpoint'],plan.result_id)
        self.assertEqual(Lineage(self.catalog).source_head(),source_head)
        with self.catalog.connect() as db:
            for cp in (result['before_id'],result['after_id']):
                ctx = dict(db.execute('SELECT * FROM checkpoint_context WHERE checkpoint_id=?',(cp,)).fetchone())
                self.assertEqual((ctx['purpose'],ctx['task_id'],ctx['workspace_id'],ctx['published']),
                                 ('workspace',parent,plan.target.key.split(':')[1],1))
        return parent, plan, result

    def test_ready_parent_reopens_and_old_ready_result_is_retained(self): self.child_lifecycle('ready')
    def test_blocked_parent_retains_reason(self): self.child_lifecycle('blocked')
    def test_active_parent_stays_active(self): self.child_lifecycle('active')

    def test_unapplied_child_blocks_root_and_cancel_preserves_applied_bytes(self):
        parent, plan = self.child_plan('ready')
        parent_result = self.show(parent)['results'][-1]
        with self.assertRaises(CoreError) as caught: self.prepare_direct(parent,parent_result)
        self.assertEqual(caught.exception.code,'TASK_CHILDREN_PENDING')
        journal = self.stage(plan); result = self.finish(journal)
        parent_root = Path(self.show(parent)['workspace']['path'])
        self.mutate('task.cancel', plan.task_id)
        self.assertEqual((parent_root/'file').read_bytes(),b'child')
        self.assertEqual(self.committer().receipt(result['receipt_id'])['receipt_id'],result['receipt_id'])

    def test_incremental_contribution_and_historical_prepare_preserve_destination_edits(self):
        plan = self.root_plan(); first = self.finish(self.stage(plan))
        (self.root/'file').write_bytes(b'destination edit')
        self.mutate('task.resume',plan.task_id)
        workspace = Path(self.show(plan.task_id)['workspace']['path'])
        (workspace/'new').write_bytes(b'second contribution')
        ready = self.wait_operation(self.mutate('task.ready',plan.task_id)['operation_id'])
        second = self.prepare_direct(plan.task_id,ready['result']['checkpoint_id'])
        self.assertEqual(second.base.checkpoint_id,plan.result_id)
        applied = self.finish(self.stage(second))
        self.assertEqual((self.root/'file').read_bytes(),b'destination edit')
        self.assertEqual((self.root/'new').read_bytes(),b'second contribution')
        self.assertEqual(self.committer().receipt(applied['receipt_id'])['previous_receipt_id'],first['receipt_id'])
        (self.root/'file').write_bytes(b'later edit')
        self.prepare_direct(plan.task_id,plan.result_id)
        replay = self.catalog.get_operation(self.operation)['result']
        self.assertTrue(replay['already_applied'])
        self.assertEqual(replay['receipt_id'],first['receipt_id'])
        self.assertEqual((self.root/'file').read_bytes(),b'later edit')
        self.assertEqual(Lineage(self.catalog).source_usage()['source_head'],applied['after_id'])
        self.assertNotEqual(Lineage(self.catalog).source_usage()['source_head'],ready['result']['checkpoint_id'])

    def test_notification_failure_cannot_change_committed_replay(self):
        plan = self.root_plan(); journal = self.stage(plan)
        result = self.finish(journal)
        with patch.object(self.catalog,'notify',side_effect=RuntimeError('notify')):
            with self.assertRaises(RuntimeError): self.catalog.notify()
            self.assertEqual(self.finish(journal),result)
        self.assertEqual(self.catalog.get_operation(journal.operation_id)['state'],'succeeded')

    def test_resume_commits_own_outcome_without_rewriting_interrupted_apply(self):
        plan = self.root_plan(); journal = self.stage(plan)
        error = dict(code='OPERATION_INTERRUPTED',message='interrupted',hint='recover',retryable=False)
        self.catalog.transition(journal.operation_id,'interrupted',error=error)
        historical = self.catalog.get_operation(journal.operation_id)
        request = self.request('integration.recovery.resume',operation=journal.operation_id,writers_paused=True)
        recovery = self.catalog.accept_operation(request)['operation_id']; self.catalog.transition(recovery,'running')
        with self.catalog.connect(write=True) as db:
            db.execute('INSERT INTO integration_recoveries VALUES (?,?,?,NULL,?)',(recovery,journal.operation_id,'resume','{}'))
        result = self.assert_sql_rollback(plan,journal,recovery)
        self.assertEqual(self.catalog.get_operation(journal.operation_id),historical)
        self.assertEqual(self.catalog.get_operation(recovery)['result'],result)
        receipt = self.committer().receipt(result['receipt_id'])
        self.assertEqual(receipt['operation_id'],recovery)
        self.assertEqual(receipt['original_operation_id'],journal.operation_id)
        with self.catalog.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT result_json FROM integration_recoveries WHERE operation_id=?',(recovery,)).fetchone()[0]),result)

    def test_refuses_unverified_phase_claim_loss_stale_revision_and_wrong_approval(self):
        plan = self.root_plan(); journal = self.stage(plan,verify=False)
        with self.assertRaises(CoreError): self.finish(journal)
        IntegrationPublisher(self.service,self.catalog).verify_final(journal)
        for sql,args in [
            ('DELETE FROM task_claims WHERE task_id=?',(plan.task_id,)),
            ('DELETE FROM integration_claims WHERE path=?',(str(journal.path),)),
            ('UPDATE tasks SET revision=revision+1 WHERE id=?',(plan.task_id,)),
            ('UPDATE source_head SET checkpoint_id=?',(plan.result_id,)),
            ("UPDATE integration_journals SET intent_json=json_remove(intent_json,'$.approval') WHERE operation_id=?",(journal.operation_id,)),
        ]:
            with self.subTest(sql=sql), self.assertRaises(CoreError):
                with self.catalog.connect(write=True) as db:
                    db.execute(sql,args)
                    self.committer().finish(db,journal,journal.operation_id)
        self.finish(journal)

    def test_receipt_paging_counts_and_validation(self):
        plan = self.root_plan(); result = self.finish(self.stage(plan)); committer = self.committer()
        first = committer.receipt(result['receipt_id'],limit=1)
        entries = first['entries'][:]; cursor = first['next_after']
        while cursor is not None:
            page = committer.receipt(result['receipt_id'],after=cursor,limit=1)
            entries += page['entries']; cursor = page['next_after']
        self.assertEqual(len(entries),first['total'])
        self.assertEqual(committer.receipt(result['receipt_id'],after=first['total'])['entries'],[])
        for args in (dict(limit=0),dict(limit=257),dict(after=True),dict(after=-1),dict(after=first['total']+1)):
            with self.assertRaises(CoreError): committer.receipt(result['receipt_id'],**args)

    def test_final_commit_uses_metadata_only_and_source_usage_ignores_later_task_history(self):
        plan = self.root_plan(); journal = self.stage(plan)
        with patch('solace_core.integration_snapshots.SnapshotStore._verify',side_effect=AssertionError('CAS rehash')):
            result = self.finish(journal)
        other, _, _ = self.ready_task({'large':b'x'*10000})
        usage = Lineage(self.catalog).source_usage()
        with self.catalog.connect() as db:
            expected = db.execute('SELECT logical_bytes FROM checkpoints WHERE id=?',(result['after_id'],)).fetchone()[0]
        self.assertEqual(usage['logical_bytes'],expected)
        self.assertEqual(usage['source_head'],result['after_id'])
        self.assertNotEqual(self.show(other)['workspace']['head_checkpoint'],usage['source_head'])

    def test_receiving_parent_can_save_again_and_integrate_to_source(self):
        parent, plan, child_receipt = self.child_lifecycle('ready')
        old_result = self.show(parent)['results'][0]
        done = self.wait_operation(self.mutate('task.ready',parent)['operation_id'])
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual(self.show(parent)['aggregate'],'ready')
        root_plan = self.prepare_direct(parent,done['result']['checkpoint_id'])
        result = self.finish(self.stage(root_plan))
        self.assertEqual((self.root/'file').read_bytes(),b'child')
        self.assertEqual(self.show(plan.task_id)['latest_result_application']['receipt_id'],child_receipt['receipt_id'])
        self.assertEqual(self.show(parent)['latest_result_application']['receipt_id'],result['receipt_id'])
        self.assertIn(old_result,self.show(parent)['results'])

    def test_rejects_pinned_manifest_context_approval_and_claim_metadata_drift(self):
        plan = self.root_plan(); journal = self.stage(plan)
        for sql,args in [
            ('UPDATE checkpoints SET parent_id=? WHERE id=?',(plan.result_id,plan.current.checkpoint_id)),
            ("UPDATE checkpoint_context SET purpose='workspace' WHERE checkpoint_id=?",(plan.candidate.checkpoint_id,)),
            ('UPDATE file_entries SET mode=384 WHERE checkpoint_id=?',(plan.incoming.checkpoint_id,)),
            ("UPDATE integration_approvals SET binding_json='{}'",()),
            ("UPDATE integration_claims SET identity_json='{}' WHERE kind='target'",()),
            ("INSERT INTO integration_claims VALUES (?,?,'journal',NULL,'now')",(str(self.base/'extra'),journal.operation_id)),
        ]:
            with self.subTest(sql=sql), self.assertRaises(CoreError):
                with self.catalog.connect(write=True) as db:
                    db.execute(sql,args)
                    self.committer().finish(db,journal,journal.operation_id)
        self.finish(journal)

    def test_unlinked_recovery_cannot_publish_and_receipts_are_immutable(self):
        plan = self.root_plan(); journal = self.stage(plan)
        request = self.request('integration.recovery.resume',operation=journal.operation_id,writers_paused=True)
        recovery = self.catalog.accept_operation(request)['operation_id']; self.catalog.transition(recovery,'running')
        with self.assertRaises(CoreError): self.finish(journal,recovery)
        result = self.finish(journal)
        import sqlite3
        for sql in ('UPDATE integration_receipts SET details_json=details_json','DELETE FROM integration_receipts'):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.catalog.connect(write=True) as db: db.execute(sql)
        self.assertEqual(self.finish(journal),result)

    def test_receipt_pages_bound_complete_escaped_response_and_refuse_oversized_row(self):
        plan = self.root_plan(); journal = self.stage(plan)
        # Synthetic metadata exercises the byte ceiling without creating host
        # pathnames beyond Linux pathname limits. No file bytes are embedded.
        data = json.dumps(dict(base=None,current=None,incoming=None,candidate=None))
        with self.catalog.connect(write=True) as db:
            ordinal = db.execute('SELECT COUNT(*) FROM integration_decisions').fetchone()[0]
            for index in range(180):
                db.execute('INSERT INTO integration_decisions VALUES (?,?,?,?,?,?,?)',
                    (plan.id,plan.revision,ordinal+index,'escaped-'+str(index)+'é'*250,'keep_current',None,data))
        result = self.finish(journal)
        from solace_core.observations import encoded_size, PAGE_BYTES
        page = self.committer().receipt(result['receipt_id'],limit=256)
        self.assertLess(encoded_size(page),PAGE_BYTES+1)
        self.assertIsNotNone(page['next_after'])
        self.assertLess(len(page['entries']),page['total'])
        count = len(page['entries']); cursor = page['next_after']
        while cursor is not None:
            rest = self.committer().receipt(result['receipt_id'],after=cursor,limit=256)
            count += len(rest['entries']); cursor = rest['next_after']
            self.assertLessEqual(encoded_size(rest),PAGE_BYTES)
        self.assertEqual(count,page['total'])
        # A separate retained plan can contain an individually oversized decision.
        other = self.root_plan(); second = self.stage(other)
        with self.catalog.connect(write=True) as db:
            ordinal = db.execute('SELECT COUNT(*) FROM integration_decisions WHERE plan_id=?',(other.id,)).fetchone()[0]
            db.execute('INSERT INTO integration_decisions VALUES (?,?,?,?,?,?,?)',
                (other.id,other.revision,ordinal,'huge','keep_current','é'*70000,data))
        applied = self.finish(second)
        with self.assertRaises(CoreError) as caught:
            self.committer().receipt(applied['receipt_id'],after=ordinal)
        self.assertEqual(caught.exception.code,'RESPONSE_TOO_LARGE')


    def test_final_commit_refuses_completed_action_metadata_substitution(self):
        plan = self.root_plan(); journal = self.stage(plan)
        with self.assertRaises(CoreError):
            with self.catalog.connect(write=True) as db:
                db.execute("UPDATE integration_actions SET path='unapproved' WHERE operation_id=?",(journal.operation_id,))
                self.committer().finish(db,journal,journal.operation_id)
        self.finish(journal)

    def test_new_child_result_becomes_pending_after_prior_result_was_applied(self):
        parent, first, _ = self.child_lifecycle('active')
        self.mutate('task.resume',first.task_id)
        workspace = Path(self.show(first.task_id)['workspace']['path'])
        (workspace/'next').write_bytes(b'next contribution')
        ready = self.wait_operation(self.mutate('task.ready',first.task_id)['operation_id'])
        self.assertEqual(ready['state'],'succeeded',ready)
        self.assertEqual(self.show(parent)['children_application'],dict(ready=1,applied=0,unapplied=1))
        self.assertFalse(self.show(first.task_id)['latest_result_application']['applied'])
        plan = self.prepare_direct(first.task_id,ready['result']['checkpoint_id'])
        self.assertEqual(plan.base.checkpoint_id,first.result_id)
        self.finish(self.stage(plan))
        self.assertEqual(self.show(parent)['children_application'],dict(ready=1,applied=1,unapplied=0))

    def test_immutable_result_publication_needs_no_online_incoming_workspace(self):
        plan = self.root_plan(); journal = self.stage(plan)
        # Claimed task views hide paths; obtain the retained path from metadata.
        with self.catalog.connect() as db:
            workspace = Path(db.execute('SELECT path FROM workspaces WHERE task_id=?',(plan.task_id,)).fetchone()[0])
        workspace.rename(self.base/'offline-workspace')
        result = self.finish(journal)
        self.assertEqual((self.root/'file').read_bytes(),b'incoming')
        self.assertEqual(self.show(plan.task_id)['latest_result_application']['receipt_id'],result['receipt_id'])
