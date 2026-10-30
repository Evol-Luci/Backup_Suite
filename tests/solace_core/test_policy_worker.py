"""Policy reconciliation uses durable operations, never application authority."""
import time
from pathlib import Path
import sys
from unittest.mock import patch
from task_fixture import TaskCase
from test_check_profiles import definition
import test_policy_queue as queue_fixtures
from solace_core.policy_queue import PolicyQueue
from solace_core.check_selection import CheckSelection


class PolicyWorkerTests(TaskCase):
    ready = queue_fixtures.PolicyQueueTests.ready

    def enable(self):
        value = definition()
        value["steps"][0]["argv"] = [sys.executable, "-c", getattr(self, "script", "print(123)")]
        with patch("test_policy_queue.definition", return_value=value):
            return queue_fixtures.PolicyQueueTests.enable(self)

    def setUp(self):
        super().setUp()
        self.catalog = self.service.catalog(self.project)
        worker = getattr(self.service, 'policy_worker', None)
        if worker: worker.close()

    def queue_list(self, **kwargs):
        return PolicyQueue.list(self.catalog, service=self.service, **kwargs)

    def queue_show(self, intent):
        return PolicyQueue.show(self.catalog, intent, service=self.service)

    def queued(self, selected=True):
        profile = self.enable()
        task = self.create_task()
        if selected:
            CheckSelection(self.catalog).select(self.request('task.check-profile.select', task=task,
                profile=profile, expected_revision=self.show(task)['revision']))
        self.ready(task)
        return self.queue_list()['intents'][0]

    def advance(self):
        for _ in range(300):
            PolicyQueue.tick(self.service, self.project)
            row = self.queue_list()['intents'][0]
            if row['stage'] in ('pending', 'pending_apply', 'done'): return row
            time.sleep(.01)
        self.fail('policy queue did not settle')

    def test_pass_stops_before_apply(self):
        self.queued()
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        row = self.advance()
        self.assertEqual(row['stage'], 'pending_apply', row)
        self.assertIsNotNone(row['run_id'])
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM integration_receipts').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE actor='policy' AND kind NOT IN ('integration.prepare','integration.check.run')").fetchone()[0], 0)

    def test_root_destination_edit_revokes_pending_apply_without_rerun(self):
        self.queued()
        row = self.advance()
        self.assertEqual(row['stage'], 'pending_apply', row)
        (self.root / 'external-change.txt').write_text('outside writer')
        shown = self.service._dispatch(self.request('integration.queue.show', intent=row['id']))
        self.assertTrue(shown['stale'], shown)
        self.assertIn('target_changed', shown['stale_reasons'])
        PolicyQueue.tick(self.service, self.project)
        settled = self.service._dispatch(self.request('integration.queue.show', intent=row['id']))
        self.assertEqual((settled['stage'], settled['reason']), ('pending', 'target_changed'))
        self.assertEqual((self.root / 'external-change.txt').read_text(), 'outside writer')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM check_runs').fetchone()[0], 1)

    def test_pending_apply_status_opens_store_read_only_without_sidecars(self):
        import sqlite3
        self.queued()
        row = self.advance()
        self.assertEqual(row['stage'], 'pending_apply', row)
        sidecars = [Path(str(self.catalog.database) + suffix) for suffix in ('-wal', '-shm')]
        self.assertFalse(any(path.exists() for path in sidecars))
        original = sqlite3.connect
        opens = []
        def observe(database, *args, **kwargs):
            if str(self.catalog.database) in str(database):
                opens.append(str(database))
                self.assertIn('mode=ro', str(database))
                self.assertTrue(kwargs.get('uri'))
            connection = original(database, *args, **kwargs)
            self.assertFalse(any(path.exists() for path in sidecars))
            return connection
        with patch('sqlite3.connect', side_effect=observe):
            shown = self.service._dispatch(self.request('integration.queue.show', intent=row['id']))
        self.assertEqual(shown['stage'], 'pending_apply')
        self.assertFalse(shown['stale'])
        self.assertTrue(opens)
        self.assertFalse(any(path.exists() for path in sidecars))

    def test_child_destination_edit_revokes_pending_apply_without_rerun(self):
        profile = self.enable()
        parent = self.create_task(); parent_path = Path(self.start(parent)['path'])
        child = self.create_task(parent=parent)
        CheckSelection(self.catalog).select(self.request('task.check-profile.select', task=child,
            profile=profile, expected_revision=self.show(child)['revision']))
        self.ready(child)
        row = self.advance()
        self.assertEqual(row['stage'], 'pending_apply', row)
        (parent_path / 'external-change.txt').write_text('outside writer')
        shown = self.service._dispatch(self.request('integration.queue.show', intent=row['id']))
        self.assertTrue(shown['stale'], shown)
        self.assertIn('target_changed', shown['stale_reasons'])
        PolicyQueue.tick(self.service, self.project)
        settled = self.service._dispatch(self.request('integration.queue.show', intent=row['id']))
        self.assertEqual((settled['stage'], settled['reason']), ('pending', 'target_changed'))
        self.assertEqual((parent_path / 'external-change.txt').read_text(), 'outside writer')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM check_runs').fetchone()[0], 1)

    def test_missing_selection_does_not_execute(self):
        self.queued(selected=False)
        row = self.advance()
        self.assertEqual((row['stage'], row['reason']), ('pending', 'check_profile_missing'))
        self.assertIsNone(row['run_id'])

    def test_stale_policy_stops(self):
        self.queued()
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE project_integration_policy SET revision=revision+1')
        row = self.advance()
        self.assertEqual((row['stage'], row['reason']), ('pending', 'policy_changed'))

    def test_target_head_stops(self):
        item = self.queued()
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE source_head SET checkpoint_id=?', (item['result_checkpoint_id'],))
        self.assertEqual(self.advance()['reason'], 'target_changed')

    def test_failed_run_stays_pending(self):
        self.script = 'raise SystemExit(3)'
        self.queued()
        self.assertEqual(self.advance()['reason'], 'check_failed')

    def test_check_completion_between_status_reads_does_not_report_running_as_failed(self):
        self.script = 'import time; time.sleep(.5); raise SystemExit(3)'
        self.queued()
        for _ in range(300):
            PolicyQueue.tick(self.service, self.project)
            row = self.queue_list()['intents'][0]
            if row['stage'] == 'checking': break
            time.sleep(.01)
        self.assertEqual(row['stage'], 'checking', row)
        original = self.catalog.get_operation

        def finish_between_reads(operation_id):
            if operation_id == row['check_operation_id']:
                deadline = time.monotonic() + 5
                while original(operation_id)['state'] in ('queued', 'running') and time.monotonic() < deadline:
                    time.sleep(.01)
            return original(operation_id)

        with patch.object(self.catalog, 'get_operation', side_effect=finish_between_reads):
            PolicyQueue.tick(self.service, self.project)
        observed = self.queue_list()['intents'][0]
        self.assertNotIn((observed['stage'], observed['reason']),
                         (('pending', 'check_reserved'), ('pending', 'check_running')), observed)
        self.wait_operation(row['check_operation_id'])
        self.assertEqual(self.advance()['reason'], 'check_failed')

    def test_acceptance_gap_replays_same_check(self):
        item = self.queued()
        row = self.advance()
        self.assertEqual(row['stage'], 'pending_apply', row)
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE integration_policy_intents SET stage='preparing',run_id=NULL,check_operation_id=NULL WHERE id=?", (item['id'],))
        replay = self.advance()
        self.assertEqual(replay['run_id'], row['run_id'])
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM check_runs').fetchone()[0], 1)

    def test_restart_reserved_check_is_not_executed_again(self):
        from solace_core.service import CoreService
        self.queued()
        PolicyQueue.tick(self.service, self.project)
        row = self.queue_list()['intents'][0]
        self.wait_operation(row['prepare_operation_id'])
        with patch.object(self.service.coordinator(self.project), 'submit'):
            PolicyQueue.tick(self.service, self.project)
        row = self.queue_list()['intents'][0]
        self.assertEqual(row['stage'], 'checking', row)
        self.service.close()
        self.service = CoreService(self.registry, config=self.config)
        self.addCleanup(self.service.close)
        self.service.policy_worker.close()
        self.catalog = self.service.catalog(self.project)
        row = self.advance()
        self.assertEqual(row['reason'], 'check_interrupted', row)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM check_runs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM check_steps').fetchone()[0], 0)

    def test_conflict_stays_pending(self):
        (self.root/'binary').write_bytes(b'\0base')
        profile = self.enable()
        task = self.create_task()
        CheckSelection(self.catalog).select(self.request('task.check-profile.select', task=task,
            profile=profile, expected_revision=self.show(task)['revision']))
        workspace = Path(self.start(task)['path'])
        (workspace/'binary').write_bytes(b'\0incoming')
        self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        (self.root/'binary').write_bytes(b'\0current')
        self.assertEqual(self.advance()['reason'], 'prepare_conflicted')

    def test_scheduler_wakes_and_reconciles_without_waiting_in_fifo(self):
        from solace_core.policy_worker import PolicyWorker
        self.service.policy_worker = PolicyWorker(self.service, interval_seconds=.02)
        self.service.policy_worker.start()
        self.queued()
        for _ in range(300):
            row = self.queue_list()['intents'][0]
            if row['stage'] == 'pending_apply': break
            time.sleep(.01)
        self.assertEqual(row['stage'], 'pending_apply', row)

    def test_prepare_rechecks_policy_at_acceptance(self):
        self.queued()
        original = self.service._dispatch
        def changed(request):
            with self.catalog.connect(write=True) as db:
                db.execute('UPDATE project_integration_policy SET revision=revision+1')
            return original(request)
        with patch.object(self.service, '_dispatch', side_effect=changed):
            PolicyQueue.tick(self.service, self.project)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE actor='policy'").fetchone()[0], 0)
        self.assertEqual(self.advance()['stage'], 'pending')

    def test_check_rechecks_target_at_acceptance(self):
        item = self.queued()
        PolicyQueue.tick(self.service, self.project)
        row = self.queue_list()['intents'][0]
        self.wait_operation(row['prepare_operation_id'])
        original = self.service._dispatch
        def changed(request):
            with self.catalog.connect(write=True) as db:
                db.execute('UPDATE source_head SET checkpoint_id=?', (item['result_checkpoint_id'],))
            return original(request)
        with patch.object(self.service, '_dispatch', side_effect=changed):
            PolicyQueue.tick(self.service, self.project)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM check_runs').fetchone()[0], 0)

    def test_applied_historical_receipt_is_done(self):
        from solace_core.integration_review import IntegrationReview
        self.queued()
        row = self.advance()
        review = IntegrationReview(self.service, self.catalog).show(row['plan_id'])
        approved = self.service._dispatch(self.request('integration.approve', plan=row['plan_id'], revision=row['plan_revision'],
            token=review['review_token'], check_run=row['run_id']))
        accepted = self.service._dispatch(self.request('integration.apply', plan=row['plan_id'], revision=row['plan_revision'],
            approval=approved['approval'], writers_paused=True))
        self.assertEqual(self.wait_operation(accepted['operation_id'])['state'], 'succeeded')
        row = self.advance()
        self.assertEqual(row['stage'], 'done', row)
        self.assertFalse(row['stale'])
        self.assertIsNotNone(row['receipt_id'])

    def test_check_rechecks_policy_before_execution(self):
        from solace_core.check_process import CheckExecutor
        self.queued()
        PolicyQueue.tick(self.service, self.project)
        row = self.queue_list()['intents'][0]
        self.wait_operation(row['prepare_operation_id'])
        with patch.object(self.service.coordinator(self.project), 'submit') as submit:
            PolicyQueue.tick(self.service, self.project)
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE project_integration_policy SET revision=revision+1')
        with patch.object(CheckExecutor, 'execute') as execute:
            submit.call_args.args[1]()
            execute.assert_not_called()
        self.assertEqual(self.advance()['reason'], 'policy_changed')

    def test_child_pass_preserves_parent(self):
        profile = self.enable()
        parent = self.create_task(); parent_path = Path(self.start(parent)['path'])
        task = self.create_task(parent=parent)
        CheckSelection(self.catalog).select(self.request('task.check-profile.select', task=task,
            profile=profile, expected_revision=self.show(task)['revision']))
        workspace = Path(self.start(task)['path'])
        (workspace/'child-file').write_text('new')
        self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        row = self.advance()
        self.assertEqual(row['stage'], 'pending_apply', row)
        self.assertFalse((parent_path/'child-file').exists())
        self.assertFalse((self.root/'child-file').exists())

    def test_changed_plan_cannot_be_reported_pending_apply(self):
        self.queued()
        row = self.advance()
        self.service._dispatch(self.request('integration.abandon', plan=row['plan_id'], revision=row['plan_revision']))
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE integration_policy_intents SET stage='checking'")
        row = self.advance()
        self.assertEqual(row['stage'], 'pending', row)

    def test_small_tick_page_does_not_starve_new_intents(self):
        self.queued(selected=False)
        first = self.advance()
        task = self.create_task(); self.ready(task)
        for _ in range(4): PolicyQueue.tick(self.service, self.project, limit=1)
        rows = self.queue_list()['intents']
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['stage'] for r in rows], ['pending', 'pending'])
        timestamps = [r['updated_at'] for r in rows]
        for _ in range(4): PolicyQueue.tick(self.service, self.project, limit=1)
        self.assertEqual(timestamps, [r['updated_at'] for r in self.queue_list()['intents']])

    def test_busy_coordinator_tick_returns_without_waiting(self):
        import threading
        self.queued()
        entered = threading.Event(); release = threading.Event()
        def hold():
            with self.service.coordinator(self.project).lock:
                entered.set(); release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start(); self.assertTrue(entered.wait(1))
        try:
            before = time.monotonic()
            PolicyQueue.tick(self.service, self.project)
            self.assertLess(time.monotonic()-before, .5)
        finally:
            release.set(); thread.join(2)

    def test_lost_check_acceptance_reply_retains_run_evidence(self):
        from solace_core.check_runs import CheckRuns
        self.queued()
        PolicyQueue.tick(self.service, self.project)
        row = self.queue_list()['intents'][0]
        self.wait_operation(row['prepare_operation_id'])
        original = CheckRuns.start
        def lost(runs, request):
            original(runs, request)
            raise RuntimeError('lost acceptance reply')
        with patch.object(CheckRuns, 'start', lost):
            PolicyQueue.tick(self.service, self.project)
        row = self.advance()
        self.assertEqual(row['stage'], 'pending_apply', row)
        self.assertIsNotNone(row['run_id'])
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM check_runs').fetchone()[0], 1)

    def test_different_selected_profile_cannot_run(self):
        from solace_core.check_profiles import CheckProfiles
        self.enable()
        profiles = CheckProfiles(self.catalog)
        params = dict(action='create', definition=definition('different'))
        preview = profiles.preview(self.request('project.check-profile.preview', **params))
        other = profiles.apply(self.request('project.check-profile.apply', **params, confirmed=True,
            plan_token=preview['plan_token']))['profile_id']
        task = self.create_task()
        CheckSelection(self.catalog).select(self.request('task.check-profile.select', task=task,
            profile=other, expected_revision=self.show(task)['revision']))
        self.ready(task)
        row = self.advance()
        self.assertEqual(row['reason'], 'check_profile_mismatch')
        self.assertIsNone(row['check_operation_id'])

    def test_child_head_change_stops_pending_intent(self):
        self.test_child_pass_preserves_parent()
        row = self.queue_list()['intents'][0]
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE workspaces SET head_checkpoint=? WHERE id=?',
                (row['result_checkpoint_id'], row['target_key'].split(':', 1)[1]))
        row = self.advance()
        self.assertEqual((row['stage'], row['reason']), ('pending', 'target_changed'))

    def test_contention_after_catalog_lookup_never_waits_for_acceptance(self):
        import threading
        self.queued()
        original = PolicyQueue._status
        for stage in ('queued', 'preparing'):
            for name, lock in (('registry', self.service.registry.lock),
                               ('coordinator', self.service.coordinator(self.project).lock)):
                with self.subTest(stage=stage, lock=name):
                    enter = threading.Event(); acquired = threading.Event(); release = threading.Event()
                    holder_done = threading.Event()
                    def holder():
                        if not enter.wait(2): return
                        with lock:
                            acquired.set(); release.wait(2)
                        holder_done.set()
                    thread = threading.Thread(target=holder)
                    thread.start()
                    def status(*args):
                        result = original(*args)
                        enter.set()
                        self.assertTrue(acquired.wait(1))
                        return result
                    try:
                        before = time.monotonic()
                        with patch.object(PolicyQueue, '_status', side_effect=status):
                            PolicyQueue.tick(self.service, self.project)
                        self.assertFalse(holder_done.is_set(), "acceptance waited for the lock holder")
                        self.assertLess(time.monotonic()-before, 1.0)
                    finally:
                        release.set(); thread.join(3)
                    row = self.queue_list()['intents'][0]
                    self.assertEqual(row['stage'], stage)
                    self.assertIsNone(row['prepare_operation_id'] if stage == 'queued' else row['check_operation_id'])
            if stage == 'queued':
                PolicyQueue.tick(self.service, self.project)
                row = self.queue_list()['intents'][0]
                self.wait_operation(row['prepare_operation_id'])
