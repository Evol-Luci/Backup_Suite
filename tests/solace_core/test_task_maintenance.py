from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from task_fixture import TaskCase
from solace_core.tasks import TaskCatalog
from solace_core.prompts import PromptStore
from solace_core.service import CoreService

class MaintenanceTests(TaskCase):
    def test_read_expiry_boundaries_and_bounded_purge_defer_claimed_tasks(self):
        from solace_core.maintenance import PromptMaintenance
        catalog=self.service.catalog(self.project)
        now=datetime(2026,1,1,tzinfo=timezone.utc);clock=lambda:now
        tasks=TaskCatalog(catalog,now=clock);prompts=PromptStore(catalog,now=clock)
        ids=[tasks.create(self.request('task.create',title='private',prompt='MAINTENANCE CANARY',prompt_policy='days',prompt_days=1))['task_id'] for unused in range(102)]
        expiry=now+timedelta(days=1)
        now=expiry-timedelta(microseconds=1);self.assertEqual(prompts.read(ids[0])['body'],'MAINTENANCE CANARY')
        now=expiry;self.assertEqual(prompts.read(ids[0])['status'],'expired');self.assertNotIn('body',prompts.read(ids[0]))
        op=catalog.accept_operation(self.request('checkpoint.create',message='claim'))['operation_id']
        with catalog.connect(write=True) as db: tasks.claim(db,op,{ids[0]:1})
        maintenance=PromptMaintenance(self.service,batch_size=100)
        with patch('solace_core.prompts.utcnow',clock): maintenance.run_once()
        with catalog.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_prompts WHERE body IS NOT NULL").fetchone()[0],2)
            self.assertEqual(db.execute('SELECT revision FROM tasks WHERE id=?',(ids[0],)).fetchone()[0],1)
        self.assertNotIn('MAINTENANCE CANARY',str(catalog.events_after(0)))
        with patch('solace_core.prompts.utcnow',clock): maintenance.run_once()
        with catalog.connect(write=True) as db: tasks.release(db,op)
        with patch('solace_core.prompts.utcnow',clock): maintenance.run_once();maintenance.run_once()
        with catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_prompts WHERE body IS NOT NULL').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='task.prompt.redacted'").fetchone()[0],102)

    def test_service_startup_purges_expired_prompts_and_closes_maintenance(self):
        catalog=self.service.catalog(self.project)
        old=datetime.now(timezone.utc)-timedelta(days=31)
        task=TaskCatalog(catalog,now=lambda:old).create(self.request('task.create',title='expired',prompt='STARTUP SECRET'))['task_id']
        self.service.close();self.service=CoreService(self.registry,config=self.config);self.addCleanup(self.service.close)
        with self.service.catalog(self.project).connect() as db:
            self.assertIsNone(db.execute('SELECT body FROM task_prompts WHERE task_id=?',(task,)).fetchone()[0])
        self.assertEqual(self.show(task)['prompt']['status'],'expired')
        maintenance=self.service.maintenance;self.service.close()
        self.assertFalse(maintenance.thread.is_alive())

    def test_unavailable_project_does_not_stop_other_purges_and_periodic_thread_runs(self):
        from solace_core.maintenance import PromptMaintenance
        import time
        catalog=self.service.catalog(self.project)
        old=datetime.now(timezone.utc)-timedelta(days=31)
        task=TaskCatalog(catalog,now=lambda:old).create(self.request('task.create',title='expired',prompt='OTHER PRIVATE'))['task_id']
        root=self.base/'second-source';root.mkdir()
        other=self.call('project.add',root=str(root))['id']
        store=__import__('pathlib').Path(self.registry.get(other)['store']);store.rename(store.with_name('offline'))
        maintenance=PromptMaintenance(self.service,interval_seconds=0.01)
        self.addCleanup(maintenance.close)
        with self.assertLogs('solace_core.maintenance',level='WARNING') as logs:
            maintenance.start()
        self.assertNotIn('OTHER PRIVATE',''.join(logs.output))
        with catalog.connect() as db:
            self.assertIsNone(db.execute('SELECT body FROM task_prompts WHERE task_id=?',(task,)).fetchone()[0])
        new=TaskCatalog(catalog,now=lambda:old).create(self.request('task.create',title='periodic',prompt='PERIODIC SECRET'))['task_id']
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with catalog.connect() as db: body=db.execute('SELECT body FROM task_prompts WHERE task_id=?',(new,)).fetchone()[0]
            if body is None: break
            time.sleep(0.01)
        self.assertIsNone(body);maintenance.close();self.assertFalse(maintenance.thread.is_alive())

    def test_unavailable_registry_does_not_kill_future_maintenance_turns(self):
        from solace_core.maintenance import PromptMaintenance
        maintenance=PromptMaintenance(self.service)
        original=self.registry.path.parent;moved=original.with_name('data-offline');original.rename(moved)
        try:
            with self.assertLogs('solace_core.maintenance',level='WARNING'):
                maintenance.run_once()
            self.assertIn('_registry',maintenance.failures)
        finally: moved.rename(original)
        maintenance.run_once();self.assertNotIn('_registry',maintenance.failures)
