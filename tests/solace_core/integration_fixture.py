"""Disposable integration inputs without exposing unfinished API methods."""
from pathlib import Path
from task_fixture import TaskCase


class IntegrationCase(TaskCase):
    def ready_task(self, changes, *, parent=None):
        task = self.create_task(**({'parent': parent} if parent else {}))
        started = self.start(task)
        root = Path(started['path'])
        for name, data in changes.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        done = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        self.assertEqual(done['state'], 'succeeded', done)
        return task, done['result']['checkpoint_id'], root

    def prepare_request(self, task, result):
        row = self.show(task)
        params = dict(task=task, result=result, expected_revision=row['revision'])
        if row['parent_id']:
            params['expected_target_revision'] = self.show(row['parent_id'])['revision']
        return self.request('integration.prepare', **params)

    def prepare_direct(self, task, result, *, request=None):
        from solace_core.integration_plans import IntegrationPlans
        self.catalog = self.service.catalog(self.project)
        request = request or self.prepare_request(task, result)
        self.operation = self.catalog.accept_operation(request)['operation_id']
        self.catalog.transition(self.operation, 'running')
        self.plans = IntegrationPlans(self.service, self.catalog)
        return self.plans.prepare(self.operation, request)

    def version_bytes(self, version):
        from solace_core.integration_snapshots import SnapshotStore
        store = SnapshotStore(self.service.catalog(self.project))
        return {entry.path: store.read_bytes(entry) for entry in store.entries(version)}

    def make_binary_conflict(self, path='data.bin'):
        (self.root/path).write_bytes(b'\0base')
        task, result, _ = self.ready_task({path: b'\0incoming'})
        (self.root/path).write_bytes(b'\0current')
        plan = self.prepare_direct(task, result)
        from solace_core.integration_review import IntegrationReview
        self.review = IntegrationReview(self.service, self.catalog)
        return plan

    def export_direct(self, plan, destination):
        from solace_core.integration_review import IntegrationReview
        request = self.request('integration.export', plan=plan.id, revision=plan.revision,
                               destination=str(destination))
        self.export_request = request
        self.export_operation = self.catalog.accept_operation(request)['operation_id']
        self.catalog.transition(self.export_operation, 'running')
        return IntegrationReview(self.service, self.catalog).export(self.export_operation, request)
