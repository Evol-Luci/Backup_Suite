"""Disposable catalogs and API helpers, never the installed runtime."""
import unittest
import uuid
import test_catalog
import test_service

class CatalogCase(unittest.TestCase):
    def setUp(self):
        from solace_core.tasks import TaskCatalog
        test_catalog.CatalogTests.setUp(self)
        self.catalog=test_catalog.CatalogTests.create(self)
        self.tasks=TaskCatalog(self.catalog)

    def request(self,method,**params):
        if method not in ('core.status','project.add','project.list'):
            params.setdefault('project',self.project if isinstance(self.project,str) else self.registration['id'])
        return dict(version=1,id=str(uuid.uuid4()),method=method,params=params,actor='test')

class TaskCase(unittest.TestCase):
    temporary_parent="/var/tmp"
    call=test_service.ServiceTests.call
    wait_operation=test_service.ServiceTests.wait_operation
    request=CatalogCase.request

    def setUp(self):
        from solace_core.workspace_paths import CoreConfig
        from solace_core.service import CoreService
        test_service.ServiceTests.setUp(self)
        self.service.close()
        self.config=CoreConfig(sync_roots=(),workspace_root=self.base/'workspaces')
        self.service=CoreService(self.registry,config=self.config)
        self.addCleanup(self.service.close)

    def show(self,task):
        return self.call('task.show',project=self.project,task=task)

    def create_task(self,title='work',**params):
        if 'parent' in params:
            params.setdefault('expected_parent_revision',self.show(params['parent'])['revision'])
        return self.call('task.create',project=self.project,title=title,**params)['task_id']

    def mutate(self,method,task,**params):
        params.setdefault('expected_revision',self.show(task)['revision'])
        return self.call(method,project=self.project,task=task,**params)

    def start(self,task,**params):
        parent=self.show(task)['parent_id']
        if parent: params.setdefault('expected_parent_revision',self.show(parent)['revision'])
        accepted=self.mutate('task.start',task,**params)
        outcome=self.wait_operation(accepted['operation_id'])
        self.assertEqual(outcome['state'],'succeeded',outcome)
        return outcome['result']
