import unittest
import uuid
from solace_core.protocol import validate_request
from solace_core.errors import CoreError
P='a'*32
TOKEN=P+':'+str(uuid.uuid4())
class PolicyProtocolTests(unittest.TestCase):
    def validate(self, method, **params):
        return validate_request(dict(version=1,id=str(uuid.uuid4()),actor='owner',method=method,params=dict(project=P,**params)))
    def test_valid_interfaces(self):
        for scope in ('project','task'):
            extra=dict(task=P) if scope=='task' else {}
            self.validate(scope+'.policy.show',**extra)
            fields=dict(mode='manual',expected_revision=1) if scope=='task' else dict(mode='manual',allow_task_opt_in=False)
            self.validate(scope+'.policy.preview',**extra,**fields)
            self.validate(scope+'.policy.apply',**extra,**fields,plan_token=TOKEN,confirmed=True)
        self.validate('integration.queue.list',after=0,limit=256)
        self.validate('integration.queue.show',intent=P)
    def test_rejects_invalid_fields(self):
        cases=[('project.policy.preview',dict(mode='automatic',allow_task_opt_in=False)),('project.policy.preview',dict(mode='manual',allow_task_opt_in=0)),('project.policy.preview',dict(mode='checked_queue',allow_task_opt_in=False)),('project.policy.preview',dict(mode='manual',allow_task_opt_in=False,required_profile=None)),('project.policy.apply',dict(mode='manual',allow_task_opt_in=False,plan_token=TOKEN,confirmed=False)),('task.policy.preview',dict(task=P,mode='manual',expected_revision=True)),('integration.queue.list',dict(after=True)),('integration.queue.show',dict(intent='bad')),('project.policy.show',dict(extra=True))]
        for method,params in cases:
            with self.subTest(method=method,params=params),self.assertRaises(CoreError):self.validate(method,**params)
