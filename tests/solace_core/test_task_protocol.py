import unittest
import uuid
from solace_core.protocol import validate_request,encode_frame,MAX_FRAME
from solace_core.errors import CoreError

P='a'*32;T='b'*32;CP='c'*32;OP='d'*32
VALID={
 'project.upgrade':dict(project=P,preview=True),
 'project.prompt-policy.show':dict(project=P),
 'project.prompt-policy.set':dict(project=P,policy='days',days=30,expected_revision=1),
 'task.create':dict(project=P,title='work',prompt='exact\r\n'),
 'task.list':dict(project=P),'task.show':dict(project=P,task=T),
 'task.start':dict(project=P,task=T,expected_revision=1),
 'task.block':dict(project=P,task=T,expected_revision=1,reason='waiting'),
 'task.resume':dict(project=P,task=T,expected_revision=1),'task.cancel':dict(project=P,task=T,expected_revision=1),
 'task.ready':dict(project=P,task=T,expected_revision=1),
 'task.prompt.show':dict(project=P,task=T),'task.prompt.delete':dict(project=P,task=T,expected_revision=1),
 'workspace.status':dict(project=P,task=T,refresh=True),
 'workspace.checkpoint':dict(project=P,task=T,expected_revision=1,message='save'),
 'workspace.relink':dict(project=P,task=T,expected_revision=1,path='/var/tmp/work',confirmed_path='/var/tmp/work',checkpoint=CP,writers_paused=True),
 'workspace.recovery.show':dict(project=P,operation=OP),
 'workspace.recovery.resume':dict(project=P,operation=OP,task=T,expected_revision=1),
 'workspace.recovery.abandon':dict(project=P,operation=OP,task=T,expected_revision=1),
 'workspace.staging.cleanup':dict(project=P,operation=OP,preview=True),
}

class ProtocolTests(unittest.TestCase):
    def request(self,method,params):
        return dict(version=1,id=str(uuid.uuid4()),actor='agent',method=method,params=params)

    def test_all_methods_valid_and_each_rejects_extra_and_missing_fields(self):
        for method,params in VALID.items():
            with self.subTest(method=method):
                validate_request(self.request(method,params))
                with self.assertRaises(CoreError): validate_request(self.request(method,dict(params,extra=1)))
                with self.assertRaises(CoreError): validate_request(self.request(method,{k:v for k,v in params.items() if k!='project'}))

    def test_malformed_parameter_classes_and_cross_field_rules(self):
        invalid=[('task.show',dict(project=P,task='not-id')),
          ('task.create',dict(project=P,title='work',parent=T)),
          ('task.create',dict(project=P,title='work',parent=T,expected_parent_revision=1,exclusions=[])),
          ('task.create',dict(project=P,title='work',expected_parent_revision=1)),
          ('task.create',dict(project=P,title='work',prompt_policy='days')),
          ('task.create',dict(project=P,title='work',prompt_policy='retain',prompt_days=2)),
          ('task.create',dict(project=P,title='work',exclusions=['','x'])),
          ('task.create',dict(project=P,title='work',exclusions=['x','x'])),
          ('task.create',dict(project=P,title='x'*201)),
          ('project.upgrade',dict(project=P,preview=False)),
          ('workspace.staging.cleanup',dict(project=P,operation=OP,preview=True,plan_token='f'*64)),
          ('workspace.relink',dict(VALID['workspace.relink'],writers_paused=False)),
          ('workspace.relink',dict(VALID['workspace.relink'],path='relative')),
          ('project.prompt-policy.set',dict(project=P,policy='days',days=True,expected_revision=1)),
          ('project.prompt-policy.set',dict(project=P,policy='days',days=10**100,expected_revision=1)),
          ('project.prompt-policy.set',dict(project=P,policy='retain',days=1,expected_revision=1))]
        for method,params in invalid:
            with self.subTest(method=method,keys=list(params)),self.assertRaises(CoreError): validate_request(self.request(method,params))
        for value in [None,True,0,-1,'1']:
            with self.subTest(value=value),self.assertRaises(CoreError):
                validate_request(self.request('task.ready',dict(project=P,task=T,expected_revision=value)))

    def test_prompt_validation_errors_never_echo_body_and_frame_cap_applies(self):
        for body in ['SENSITIVE\0TEXT','SENSITIVE\ud800','SENSITIVE'+'x'*131073]:
            with self.assertRaises(CoreError) as caught:
                validate_request(self.request('task.create',dict(project=P,title='work',prompt=body)))
            self.assertNotIn('SENSITIVE',str(caught.exception))
        frame=self.request('task.create',dict(project=P,title='work',prompt='é'*65536))
        validate_request(frame);self.assertLess(len(encode_frame(frame)),MAX_FRAME)
        frame['params']['exclusions']=['x'*MAX_FRAME]
        with self.assertRaises(CoreError): encode_frame(frame)
