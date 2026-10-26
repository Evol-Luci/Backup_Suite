import unittest
import test_check_runs as run_fixtures
from integration_fixture import IntegrationCase
from solace_core.integration_claims import IntegrationClaims
from solace_core.integration_review import IntegrationReview
from solace_core.errors import CoreError

class CheckApprovalTests(IntegrationCase):
    prepared_run = run_fixtures.RunTests.prepared_run
    finish = run_fixtures.RunTests.finish
    def approval(self, run, token=None):
        review = IntegrationReview(self.service,self.catalog).show(self.plan.id)
        return IntegrationClaims(self.service,self.catalog).approve(self.request('integration.approve', plan=self.plan.id,
            revision=self.plan.revision, token=token or review['review_token'], check_run=run))

    def test_checked_approval_requires_current_pass_and_review_token(self):
        self.prepared_run()
        passed = self.finish(self.runs.start(self.req))
        with self.assertRaises(CoreError): self.approval(passed['id'], '0'*64)
        result = self.approval(passed['id'])
        self.assertEqual(result['checks']['status'], 'passed')

    def test_failed_and_stale_runs_refused(self):
        self.prepared_run(['raise SystemExit(4)'])
        failed = self.finish(self.runs.start(self.req))
        self.assertEqual(failed['state'],'failed')
        with self.assertRaises(CoreError): self.approval(failed['id'])

    def test_changed_target_refuses_passed_run(self):
        self.prepared_run()
        run = self.finish(self.runs.start(self.req))
        (self.root/'changed').write_text('new')
        with self.assertRaises(CoreError): self.approval(run['id'])

    def test_missing_run_refused(self):
        self.prepared_run()
        with self.assertRaises(CoreError): self.approval('0'*32)

    def test_checked_receipt_retains_step_summary_and_candidate_digest(self):
        from solace_core.integration_operations import IntegrationOperations
        from solace_core.integration_commit import IntegrationCommit
        self.prepared_run()
        run = self.finish(self.runs.start(self.req))
        approval = self.approval(run['id'])
        operations = IntegrationOperations(self.service,self.catalog)
        request = self.request('integration.apply',plan=self.plan.id,revision=self.plan.revision,
                               approval=approval['approval'],writers_paused=True)
        accepted = operations.accept(request)
        result = self.wait_operation(accepted['operation_id'])
        self.assertEqual(result['state'],'succeeded',result)
        receipt = IntegrationCommit(self.service,self.catalog).receipt(result['result']['receipt_id'])
        self.assertEqual(receipt['checks']['candidate_digest'],self.plan.candidate.manifest_sha256)
        self.assertEqual(len(receipt['checks']['steps']),2)

    def test_profile_edit_revokes_pending_approval(self):
        from solace_core.check_profiles import CheckProfiles
        from solace_core.integration_operations import IntegrationOperations
        self.prepared_run()
        run = self.finish(self.runs.start(self.req))
        approval = self.approval(run['id'])
        profiles = CheckProfiles(self.catalog)
        params = dict(action='retire',profile_id=self.profile)
        preview = profiles.preview(self.request('project.check-profile.preview',**params))
        profiles.apply(self.request('project.check-profile.apply',**params,confirmed=True,plan_token=preview['plan_token']))
        with self.assertRaises(CoreError):
            IntegrationOperations(self.service,self.catalog).accept(self.request('integration.apply',plan=self.plan.id,
                revision=self.plan.revision,approval=approval['approval'],writers_paused=True))

    def test_tampered_run_or_binding_blocks_receipt_commit(self):
        import json
        from solace_core.check_approvals import CheckApprovals
        from solace_core.integration_publish import IntegrationPublisher
        from solace_core.integration_commit import IntegrationCommit
        self.prepared_run()
        run = self.finish(self.runs.start(self.req))
        approval = self.approval(run['id'])
        claims = IntegrationClaims(self.service,self.catalog)
        request = self.request('integration.apply',plan=self.plan.id,revision=self.plan.revision,approval=approval['approval'],writers_paused=True)
        operation = self.catalog.accept_operation(request)['operation_id']
        self.catalog.transition(operation,'running')
        with self.registry.lock, self.service.coordinator(self.project).lock, self.catalog.connect(write=True) as db:
            journal = claims.reserve(db,operation,self.plan)
            intent = json.loads(db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?',(operation,)).fetchone()[0])
            intent['approval'] = CheckApprovals.lookup(db,self.plan.id,self.plan.revision,approval['approval'])
            db.execute('UPDATE integration_journals SET intent_json=? WHERE operation_id=?',(json.dumps(intent),operation))
        publisher = IntegrationPublisher(self.service,self.catalog)
        journal = publisher.stage(operation,self.plan)
        publisher.publish(journal)
        publisher.verify_final(journal)
        with self.catalog.connect(write=True) as db:
            db.execute('DROP TRIGGER check_steps_terminal_update')
            db.execute("UPDATE check_steps SET stdout_bytes=stdout_bytes+1 WHERE run_id=? AND ordinal=0",(run['id'],))
            with self.assertRaises(CoreError): IntegrationCommit(self.service,self.catalog).finish(db,journal,operation)
            self.assertEqual(db.execute('SELECT count(*) FROM integration_receipts').fetchone()[0],0)

    def test_unchecked_approval_replay_stays_unchecked(self):
        self.prepared_run()
        review = IntegrationReview(self.service,self.catalog).show(self.plan.id)
        claims = IntegrationClaims(self.service,self.catalog)
        request = self.request('integration.approve',plan=self.plan.id,revision=self.plan.revision,
                               token=review['review_token'],accept_unchecked=True)
        result = claims.approve(request)
        self.assertEqual(result['checks'],'not_run_by_solace')
        self.assertEqual(claims.approve(request),result)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT accepted_unchecked FROM integration_approvals').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM integration_checked_approvals').fetchone()[0],0)
