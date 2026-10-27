"""Required project checks apply equally to manual integration plans."""
import test_check_approvals as fixtures
from integration_fixture import IntegrationCase
from solace_core.project_policy import ProjectPolicy
from solace_core.integration_claims import IntegrationClaims
from solace_core.integration_review import IntegrationReview
from solace_core.integration_operations import IntegrationOperations
from solace_core.integration_commit import IntegrationCommit
from solace_core.errors import CoreError


class PolicyApprovalTests(IntegrationCase):
    prepared_run = fixtures.CheckApprovalTests.prepared_run
    finish = fixtures.CheckApprovalTests.finish
    approval = fixtures.CheckApprovalTests.approval
    test_profile_edit_revokes_pending_approval = fixtures.CheckApprovalTests.test_profile_edit_revokes_pending_approval
    def set_policy(self, profile=None):
        policy = ProjectPolicy(self.catalog)
        params = dict(mode='manual', allow_task_opt_in=False, required_profile=profile)
        preview = policy.preview(self.request('project.integration-policy.preview', **params))
        return policy.apply(self.request('project.integration-policy.apply', **params,
            confirmed=True, plan_token=preview['plan_token']))

    def unchecked(self):
        review = IntegrationReview(self.service,self.catalog).show(self.plan.id)
        return IntegrationClaims(self.service,self.catalog).approve(self.request('integration.approve',
            plan=self.plan.id, revision=self.plan.revision, token=review['review_token'], accept_unchecked=True))

    def apply_request(self, approval):
        return self.request('integration.apply', plan=self.plan.id, revision=self.plan.revision,
            approval=approval['approval'], writers_paused=True)

    def test_required_refuses_unchecked_manual_plan(self):
        self.prepared_run()
        self.set_policy(self.profile)
        with self.assertRaises(CoreError): self.unchecked()

    def test_required_pass_receipt_and_policy_change(self):
        self.prepared_run()
        policy = self.set_policy(self.profile)
        run = self.finish(self.runs.start(self.req))
        approval = self.approval(run['id'])
        accepted = IntegrationOperations(self.service,self.catalog).accept(self.apply_request(approval))
        result = self.wait_operation(accepted['operation_id'])
        self.assertEqual(result['state'],'succeeded',result)
        receipt = IntegrationCommit(self.service,self.catalog).receipt(result['result']['receipt_id'])
        self.assertEqual(receipt['policy']['project_policy_revision'], policy['revision'])
        self.assertEqual(receipt['policy']['required_profile_id'],self.profile)
        self.assertTrue(receipt['policy']['required_checks_met'])
        self.set_policy(None)
        replay = IntegrationOperations(self.service,self.catalog).accept(self.apply_request(approval))
        replayed = self.wait_operation(replay['operation_id'])
        self.assertEqual(replayed['state'],'succeeded',replayed)
        self.assertEqual(replayed['result']['receipt_id'],receipt['receipt_id'])

    def test_changed_policy_revokes_pending_checked_approval(self):
        self.prepared_run()
        self.set_policy(self.profile)
        run = self.finish(self.runs.start(self.req))
        approval = self.approval(run['id'])
        self.set_policy(self.profile)
        with self.assertRaises(CoreError):
            IntegrationOperations(self.service,self.catalog).accept(self.apply_request(approval))

    def test_new_requirement_revokes_unchecked_approval(self):
        self.prepared_run()
        approval = self.unchecked()
        self.set_policy(self.profile)
        with self.assertRaises(CoreError):
            IntegrationOperations(self.service,self.catalog).accept(self.apply_request(approval))

    def test_different_selected_profile_cannot_satisfy_requirement(self):
        from solace_core.check_profiles import CheckProfiles
        self.prepared_run()
        profiles = CheckProfiles(self.catalog)
        params = dict(action='create', definition=dict(self.definition, name='required'))
        preview = profiles.preview(self.request('project.check-profile.preview', **params))
        required = profiles.apply(self.request('project.check-profile.apply', **params,
            confirmed=True, plan_token=preview['plan_token']))['profile_id']
        self.set_policy(required)
        run = self.finish(self.runs.start(self.req))
        with self.assertRaises(CoreError): self.approval(run['id'])

    def test_schema_four_unchecked_behavior_and_receipt(self):
        self.prepared_run()
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE core_metadata SET schema_version=4')
        approval = self.unchecked()
        accepted = IntegrationOperations(self.service,self.catalog).accept(self.apply_request(approval))
        result = self.wait_operation(accepted['operation_id'])
        self.assertEqual(result['state'],'succeeded',result)
        receipt = IntegrationCommit(self.service,self.catalog).receipt(result['result']['receipt_id'])
        self.assertNotIn('policy',receipt)

    def test_policy_changed_after_reservation_blocks_worker_publication(self):
        from unittest.mock import patch
        from solace_core.integration_publish import IntegrationPublisher
        self.prepared_run()
        self.set_policy(self.profile)
        run = self.finish(self.runs.start(self.req))
        approval = self.approval(run['id'])
        original = IntegrationPublisher.stage
        def changed(publisher, operation, plan):
            journal = original(publisher, operation, plan)
            with self.catalog.connect(write=True) as db:
                db.execute('UPDATE project_integration_policy SET revision=revision+1')
            return journal
        with patch.object(IntegrationPublisher, 'stage', changed):
            accepted = IntegrationOperations(self.service,self.catalog).accept(self.apply_request(approval))
            result = self.wait_operation(accepted['operation_id'])
        self.assertNotEqual(result['state'],'succeeded',result)
        self.assertFalse((self.root/'candidate').exists())
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM integration_receipts').fetchone()[0],0)

    def test_policy_change_at_commit_cannot_publish_receipt(self):
        from unittest.mock import patch
        self.prepared_run()
        self.set_policy(self.profile)
        run = self.finish(self.runs.start(self.req))
        approval = self.approval(run['id'])
        original = IntegrationCommit.finish
        def changed(committer, db, journal, operation):
            db.execute('UPDATE project_integration_policy SET revision=revision+1')
            return original(committer,db,journal,operation)
        with patch.object(IntegrationCommit,'finish',changed):
            accepted = IntegrationOperations(self.service,self.catalog).accept(self.apply_request(approval))
            result = self.wait_operation(accepted['operation_id'])
        self.assertNotEqual(result['state'],'succeeded',result)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM integration_receipts').fetchone()[0],0)

    def test_policy_mutation_refused_while_reserved(self):
        from unittest.mock import patch
        from solace_core.integration_publish import IntegrationPublisher
        self.prepared_run()
        approval = self.unchecked()
        original = IntegrationPublisher.stage
        observed = []
        def guarded(publisher, operation, plan):
            with self.assertRaises(CoreError) as caught:
                self.set_policy(self.profile)
            observed.append(caught.exception.code)
            return original(publisher,operation,plan)
        with patch.object(IntegrationPublisher,'stage',guarded):
            accepted = IntegrationOperations(self.service,self.catalog).accept(self.apply_request(approval))
            result = self.wait_operation(accepted['operation_id'])
        self.assertEqual(observed,['POLICY_BUSY'])
        self.assertEqual(result['state'],'succeeded',result)
