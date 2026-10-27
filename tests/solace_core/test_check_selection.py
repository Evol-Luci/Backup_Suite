"""Selected approved profiles follow task revisions and ready-result lifecycle."""
from task_fixture import CatalogCase
from integration_fixture import IntegrationCase
from test_check_profiles import definition
from solace_core.check_profiles import CheckProfiles
from solace_core.check_selection import CheckSelection
from solace_core.errors import CoreError


class SelectionTests(CatalogCase):
    def setUp(self):
        super().setUp()
        self.selection = CheckSelection(self.catalog)
        self.profiles = CheckProfiles(self.catalog)

    def approved(self, name='tests'):
        params = dict(action='create', definition=definition(name))
        preview = self.profiles.preview(self.request('project.check-profile.preview', **params))
        return self.profiles.apply(self.request('project.check-profile.apply', **params, confirmed=True, plan_token=preview['plan_token']))['profile_id']

    def create(self, **params):
        return self.tasks.create(self.request('task.create', title='work', **params))['task_id']

    def select(self, task, profile, **params):
        params.setdefault('expected_revision', self.tasks.show(task)['revision'])
        return self.selection.select(self.request('task.check-profile.select', task=task, profile=profile, **params))

    def code(self, code, call):
        with self.assertRaises(CoreError) as caught: call()
        self.assertEqual(caught.exception.code, code)
        self.assertTrue(caught.exception.next_step)

    def test_root_select_and_child_inherit(self):
        plain = self.create()
        self.assertIsNone(self.tasks.show(plain)['check_profile'])
        profile = self.approved()
        parent = self.create(check_profile=profile, exclusions=['*.tmp'])
        child = self.create(parent=parent, expected_parent_revision=1)
        for task in (parent, child):
            selected = self.tasks.show(task)['check_profile']
            self.assertEqual(selected['profile_id'], profile)
            self.assertEqual(selected['selected_revision'], 1)
            self.assertTrue(selected['selected_at'])
            self.assertEqual(self.tasks.show(task)['profile']['exclusions'], ['*.tmp'])

    def test_task_can_override_with_another_approved_profile(self):
        first, second = self.approved('one'), self.approved('two')
        parent = self.create(check_profile=first)
        child = self.create(parent=parent, expected_parent_revision=1, check_profile=second)
        self.assertEqual(self.tasks.show(child)['check_profile']['profile_id'], second)
        self.select(child, first)
        self.assertEqual(self.tasks.show(child)['check_profile']['selected_revision'], 2)
        self.assertEqual(self.tasks.show(parent)['check_profile']['profile_id'], first)
        self.code('TASK_REVISION_CONFLICT', lambda: self.select(child, second, expected_revision=1))

    def test_missing_or_retired_profile_is_precise(self):
        task = self.create()
        self.code('CHECK_PROFILE_NOT_FOUND', lambda: self.select(task, 'missing'))
        self.code('CHECK_PROFILE_NOT_FOUND', lambda: self.create(check_profile='missing'))
        profile = self.approved()
        params = dict(action='retire', profile_id=profile)
        preview = self.profiles.preview(self.request('project.check-profile.preview', **params))
        self.profiles.apply(self.request('project.check-profile.apply', **params, confirmed=True, plan_token=preview['plan_token']))
        self.code('CHECK_PROFILE_RETIRED', lambda: self.select(task, profile))
        self.assertEqual(self.tasks.show(task)['revision'], 1)
        self.assertEqual(len(self.tasks.list()), 1)

    def test_selection_replay_is_idempotent(self):
        task, profile = self.create(), self.approved()
        request = self.request('task.check-profile.select', task=task, profile=profile, expected_revision=1)
        first = self.selection.select(request)
        self.assertEqual(self.selection.select(request), first)
        self.assertEqual(self.tasks.show(task)['revision'], 2)
        self.code('REQUEST_ID_REUSED', lambda: self.selection.select(dict(request, actor='other')))
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task,))
        self.assertEqual(self.selection.select(request), first)
        self.code('TASK_STATE_INVALID', lambda: self.select(task, profile))

    def test_selection_respects_task_claim(self):
        task, profile = self.create(), self.approved()
        operation = self.catalog.accept_operation(self.request('checkpoint.create', message='claim'))
        with self.catalog.connect(write=True) as db:
            self.tasks.claim(db, operation['operation_id'], {task:1})
        self.code('TASK_BUSY', lambda: self.select(task, profile))
        self.assertEqual(self.tasks.show(task)['revision'], 1)


class ReadySelectionTests(IntegrationCase):
    def test_selection_freezes_at_ready_and_requires_new_result(self):
        from solace_core.tasks import TaskCatalog
        from solace_core.integration_review import IntegrationReview
        task, old_result, _ = self.ready_task({'file':b'new'})
        self.catalog = self.service.catalog(self.project)
        profiles = CheckProfiles(self.catalog)
        params = dict(action='create', definition=definition())
        preview = profiles.preview(self.request('project.check-profile.preview', **params))
        profile = profiles.apply(self.request('project.check-profile.apply', **params, confirmed=True, plan_token=preview['plan_token']))['profile_id']
        selection = CheckSelection(self.catalog)
        def select():
            return selection.select(self.request('task.check-profile.select', task=task, profile=profile, expected_revision=self.show(task)['revision']))
        with self.assertRaises(CoreError) as caught: select()
        self.assertEqual(caught.exception.code, 'TASK_STATE_INVALID')
        self.mutate('task.resume', task)
        select()
        with self.assertRaises(CoreError): self.prepare_direct(task, old_result)
        new_result = self.wait_operation(self.mutate('task.ready', task)['operation_id'])['result']['checkpoint_id']
        self.assertNotEqual(new_result, old_result)
        plan = self.prepare_direct(task, new_result)
        self.assertEqual(IntegrationReview(self.service, self.catalog).show(plan.id)['check_profile']['profile_id'], profile)
        self.assertEqual(TaskCatalog(self.catalog).show(task)['results'], [old_result, new_result])
