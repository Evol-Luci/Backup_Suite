from test_check_selection import SelectionTests
from solace_core.project_policy import ProjectPolicy
from solace_core.task_policy import TaskPolicy
from solace_core.check_profiles import AUTHORITY_WARNING


class TaskPolicyTests(SelectionTests):
    def setUp(self):
        super().setUp()
        self.policy = TaskPolicy(self.catalog)
        self.project_policy = ProjectPolicy(self.catalog)

    def configure(self, mode='checked_queue', allow=True, profile=None):
        params = dict(mode=mode, allow_task_opt_in=allow, required_profile=profile)
        preview = self.project_policy.preview(self.request('project.policy.preview', **params))
        return self.project_policy.apply(self.request('project.policy.apply', **params, confirmed=True, plan_token=preview['plan_token']))

    def override(self, task, mode, confirmed=True):
        params = dict(task=task, mode=mode, expected_revision=self.tasks.show(task)['revision'])
        preview = self.policy.preview(self.request('task.policy.preview', **params))
        self.assertEqual(preview['authority_warning'], AUTHORITY_WARNING)
        return self.policy.apply(self.request('task.policy.apply', **params, confirmed=confirmed, plan_token=preview['plan_token']))

    def test_inheritance_manual_and_different_selected_profile(self):
        required, selected = self.approved('required'), self.approved('selected')
        self.configure(profile=required)
        task = self.create(check_profile=selected)
        effective = self.policy.show(task)
        self.assertEqual(effective['mode'], 'checked_queue')
        self.assertEqual(effective['required_profile_id'], required)
        self.assertEqual(effective['selected_profile_id'], selected)
        self.override(task, 'manual')
        self.assertEqual(self.tasks.show(task)['integration_policy']['mode'], 'manual')
        self.assertEqual(self.policy.show(task)['required_profile_id'], required)
        self.override(task, 'inherit')
        self.assertEqual(self.policy.show(task)['mode'], 'checked_queue')

    def test_confirmed_opt_in_and_disable_revocation(self):
        profile = self.approved()
        self.configure(mode='manual', profile=profile)
        task = self.create()
        self.code('POLICY_CONFIRMATION_REQUIRED', lambda: self.override(task, 'checked_queue', False))
        self.override(task, 'checked_queue')
        self.assertEqual(self.policy.show(task)['mode'], 'checked_queue')
        self.configure(mode='manual', allow=False)
        self.assertEqual(self.policy.show(task)['mode'], 'manual')
        self.code('POLICY_TASK_OPT_IN_DISABLED', lambda: self.override(task, 'checked_queue'))

    def test_ready_freeze_and_stale_preview(self):
        task = self.create()
        params = dict(task=task, mode='manual', expected_revision=1)
        preview = self.policy.preview(self.request('task.policy.preview', **params))
        self.configure(mode='manual', allow=False)
        self.code('POLICY_PREVIEW_STALE', lambda: self.policy.apply(self.request('task.policy.apply', **params, confirmed=True, plan_token=preview['plan_token'])))
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='ready' WHERE id=?", (task,))
        self.code('TASK_STATE_INVALID', lambda: self.override(task, 'manual'))

    def test_replay_and_expired_token(self):
        from datetime import timedelta
        from unittest.mock import patch
        task = self.create()
        params = dict(task=task, mode='manual', expected_revision=1)
        preview = self.policy.preview(self.request('task.policy.preview', **params))
        request = self.request('task.policy.apply', **params, confirmed=True, plan_token=preview['plan_token'])
        with patch.object(self.policy, 'now', return_value=self.policy.now() + timedelta(seconds=900)):
            self.code('POLICY_PREVIEW_STALE', lambda: self.policy.apply(request))
        result = self.policy.apply(request)
        self.assertEqual(self.policy.apply(request), result)
        self.assertEqual(self.tasks.show(task)['revision'], 2)
        self.code('REQUEST_ID_REUSED', lambda: self.policy.apply(dict(request, actor='other')))

    def test_older_schema_remains_manual_without_policy_tables(self):
        task = self.create()
        with self.catalog.connect(write=True) as db:
            db.execute('DROP TABLE task_integration_policy')
            db.execute('DROP TABLE project_integration_policy')
            db.execute('UPDATE core_metadata SET schema_version=4')
        self.assertEqual(self.tasks.show(task)['integration_policy']['mode'], 'manual')
        self.code('PROJECT_UPGRADE_REQUIRED', lambda: self.override(task, 'manual'))
