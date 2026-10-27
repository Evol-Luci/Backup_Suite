"""Owner-confirmed project policy binds profile and policy revisions."""
import unittest
from datetime import timedelta
from unittest.mock import patch
import test_check_profiles
from test_check_profiles import definition
from solace_core.project_policy import ProjectPolicy
from solace_core.check_profiles import AUTHORITY_WARNING


class ProjectPolicyTests(unittest.TestCase):
    request = test_check_profiles.CheckProfileTests.request
    create_profile = test_check_profiles.CheckProfileTests.create_profile
    assert_code = test_check_profiles.CheckProfileTests.assert_code
    def setUp(self):
        test_check_profiles.CheckProfileTests.setUp(self)
        self.policy = ProjectPolicy(self.catalog)

    def policy_request(self, method='preview', **params):
        request = self.request(method, **params)
        request['method'] = 'project.policy.' + method
        return request

    def preview(self, **params):
        return self.policy.preview(self.policy_request(**params))

    def apply(self, preview, **params):
        return self.policy.apply(self.policy_request('apply', confirmed=True, plan_token=preview['plan_token'], **params))

    def test_default_and_confirmed_opt_in(self):
        self.assertEqual(self.policy.show()['mode'], 'manual')
        self.assertIsNone(self.policy.show()['required_profile_id'])
        profile = self.create_profile()
        params = dict(mode='checked_queue', required_profile=profile['profile_id'], allow_task_opt_in=True)
        preview = self.preview(**params)
        self.assertEqual(preview['authority_warning'], AUTHORITY_WARNING)
        self.assert_code('POLICY_CONFIRMATION_REQUIRED', lambda: self.policy.apply(self.policy_request('apply', **params, plan_token=preview['plan_token'])))
        result = self.apply(preview, **params)
        self.assertEqual((result['revision'], result['required_profile_revision'], result['required_profile_digest']), (2, 1, profile['digest']))
        self.assert_code('POLICY_PREVIEW_STALE', lambda: self.apply(preview, **params))

    def test_profile_changes_stale_preview(self):
        for action in ('update', 'retire'):
            with self.subTest(action=action):
                profile = self.create_profile(action)
                params = dict(mode='checked_queue', required_profile=profile['profile_id'], allow_task_opt_in=False)
                preview = self.preview(**params)
                change = dict(action=action, profile_id=profile['profile_id'])
                if action == 'update': change['definition'] = definition(action + '-updated')
                approved = self.profiles.preview(self.request(**change))
                self.profiles.apply(self.request('apply', **change, confirmed=True, plan_token=approved['plan_token']))
                self.assert_code('POLICY_PREVIEW_STALE', lambda: self.apply(preview, **params))

    def test_invalid_and_expired_preview(self):
        for params in (dict(mode='checked_queue', required_profile=None, allow_task_opt_in=False), dict(mode='manual', required_profile=None, allow_task_opt_in=1), dict(mode='manual', required_profile=None, allow_task_opt_in=True)):
            self.assert_code('INVALID_REQUEST', lambda: self.preview(**params))
        params = dict(mode='manual', required_profile=None, allow_task_opt_in=False)
        preview = self.preview(**params)
        with patch.object(self.policy, 'now', return_value=self.policy.now() + timedelta(seconds=900)):
            self.assert_code('POLICY_PREVIEW_STALE', lambda: self.apply(preview, **params))

    def test_disable_preserves_retained_profile_evidence_and_receipts(self):
        profile = self.create_profile()
        params = dict(mode='checked_queue', required_profile=profile['profile_id'], allow_task_opt_in=True)
        self.apply(self.preview(**params), **params)
        with self.catalog.connect() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM check_profile_revisions')]
            receipts = db.execute('SELECT count(*) FROM task_requests').fetchone()[0]
        params = dict(mode='manual', required_profile=None, allow_task_opt_in=False)
        preview = self.preview(**params)
        request = self.policy_request('apply', confirmed=True, plan_token=preview['plan_token'], **params)
        result = self.policy.apply(request)
        self.assertEqual(self.policy.apply(request), result)
        self.assertEqual(result['mode'], 'manual')
        with self.catalog.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM check_profile_revisions')], before)
            self.assertEqual(db.execute('SELECT count(*) FROM task_requests').fetchone()[0], receipts + 2)

    def test_manual_profile_binding_identity_and_request_reuse(self):
        profile = self.create_profile()
        params = dict(mode='manual', required_profile=profile['profile_id'], allow_task_opt_in=True)
        request = self.policy_request(**params)
        preview = self.policy.preview(request)
        self.assertEqual(self.policy.preview(request), preview)
        with patch.object(self.policy.profiles, '_identity', return_value={'changed': True}):
            self.assert_code('POLICY_PREVIEW_STALE', lambda: self.apply(preview, **params))
        result = self.apply(preview, **params)
        self.assertTrue(result['allow_task_opt_in'])
        self.assertEqual(result['required_profile_id'], profile['profile_id'])
        reused = dict(request, actor='other')
        self.assert_code('REQUEST_ID_REUSED', lambda: self.policy.preview(reused))
