"""Ready publication queues immutable policy evidence without applying files."""
from task_fixture import TaskCase
import uuid
from solace_core.policy_queue import PolicyQueue, REQUEST_NAMESPACE
from solace_core.check_profiles import CheckProfiles
from solace_core.project_policy import ProjectPolicy
from test_check_profiles import definition


class PolicyQueueTests(TaskCase):
    def setUp(self):
        super().setUp()
        self.service.policy_worker.close()
        self.catalog = self.service.catalog(self.project)

    def queue_list(self, **kwargs):
        return PolicyQueue.list(self.catalog, service=self.service, **kwargs)

    def queue_show(self, intent):
        return PolicyQueue.show(self.catalog, intent, service=self.service)

    def enable(self):
        profiles = CheckProfiles(self.catalog)
        params = dict(action='create', definition=definition('required'))
        preview = profiles.preview(self.request('project.check-profile.preview', **params))
        profile = profiles.apply(self.request('project.check-profile.apply', **params, confirmed=True, plan_token=preview['plan_token']))['profile_id']
        policy = ProjectPolicy(self.catalog)
        params = dict(mode='checked_queue', required_profile=profile, allow_task_opt_in=True)
        preview = policy.preview(self.request('project.policy.preview', **params))
        policy.apply(self.request('project.policy.apply', **params, confirmed=True, plan_token=preview['plan_token']))
        return profile

    def ready(self, task):
        self.start(task)
        outcome = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        self.assertEqual(outcome['state'], 'succeeded', outcome)
        return outcome['result']['checkpoint_id']

    def test_manual_and_older_schema_have_no_intent(self):
        self.ready(self.create_task())
        self.assertEqual(self.queue_list()['intents'], [])
        self.assertEqual(self.service._dispatch(self.request('integration.queue.list'))['intents'], [])
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE core_metadata SET schema_version=4')
        self.ready(self.create_task())
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM integration_policy_intents').fetchone()[0], 0)

    def test_ready_binding_retry_and_status_staleness(self):
        profile = self.enable()
        task = self.create_task()
        checkpoint = self.ready(task)
        intent = self.queue_list()['intents'][0]
        self.assertEqual(intent['profile_id'], profile)
        self.assertEqual(intent['result_checkpoint_id'], checkpoint)
        self.assertEqual(intent['target_key'], 'source')
        self.assertFalse(intent['stale'])
        for stage in ('prepare', 'check'):
            self.assertEqual(intent[stage + '_request_id'], uuid.uuid5(REQUEST_NAMESPACE, intent['id'] + ':' + stage).hex)
        self.assertNotEqual(intent['prepare_request_id'], intent['check_request_id'])
        self.assertEqual(self.service._dispatch(self.request('integration.queue.show', intent=intent['id'])), intent)
        revision = self.show(task)['revision']
        with self.catalog.connect(write=True) as db:
            again = PolicyQueue.enqueue_ready(db, self.catalog, task, checkpoint, revision, self.catalog._now())
        self.assertEqual(again, intent['id'])
        replay = self.queue_show(again)
        self.assertEqual(replay['prepare_request_id'], intent['prepare_request_id'])
        self.assertEqual(replay['check_request_id'], intent['check_request_id'])
        self.assertEqual(len(self.queue_list()['intents']), 1)
        self.mutate('task.resume', task)
        self.assertIn('task_changed', self.queue_show(again)['stale_reasons'])
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE project_integration_policy SET revision=revision+1')
        self.assertIn('policy_changed', self.queue_show(again)['stale_reasons'])

    def test_child_target_and_bounded_pages(self):
        self.enable()
        parent = self.create_task(); self.start(parent)
        child = self.create_task(parent=parent)
        self.ready(child)
        item = self.queue_list(limit=1)['intents'][0]
        self.assertEqual(item['target_key'], 'workspace:' + self.show(parent)['workspace']['id'])
        self.assertEqual(item['target_revision'], self.show(parent)['revision'])
        self.mutate('task.block', parent, reason='waiting')
        self.assertIn('target_changed', self.queue_show(item['id'])['stale_reasons'])
        self.ready(self.create_task())
        page = self.queue_list(limit=1)
        self.assertIsNotNone(page['next_after'])
        last = self.queue_list(after=page['next_after'], limit=1)
        self.assertEqual(len(last['intents']), 1)
        self.assertIsNone(last['next_after'])
        for params in ({'limit':257}, {'after':-1}, {'limit':True}):
            with self.assertRaises(Exception): self.queue_list(**params)
        self.assertNotIn('stdout', str(page)); self.assertNotIn('prompt', str(page))

    def test_source_head_change_stales_intent_without_status_writes(self):
        self.enable()
        task = self.create_task(); checkpoint = self.ready(task)
        intent = self.queue_list()['intents'][0]
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE source_head SET checkpoint_id=?', (checkpoint,))
        self.assertIn('target_changed', self.queue_show(intent['id'])['stale_reasons'])
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT stage FROM integration_policy_intents').fetchone()[0], 'queued')

    def test_intent_and_ready_publication_roll_back_together(self):
        from unittest.mock import patch
        self.enable()
        task = self.create_task(); self.start(task)
        enqueue = PolicyQueue.enqueue_ready
        def fail_after_insert(*args):
            enqueue(*args)
            raise RuntimeError('publication fault')
        with patch.object(PolicyQueue, 'enqueue_ready', side_effect=fail_after_insert):
            outcome = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(self.show(task)['state'], 'active')
        self.assertEqual(self.show(task)['results'], [])
        self.assertEqual(self.queue_list()['intents'], [])
