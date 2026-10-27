import unittest
from solace_core.protocol import validate_request, encode_frame
from solace_core.errors import CoreError

P='a'*32

def frame(method, params):
    return dict(version=1,id='01234567-89ab-cdef-0123-456789abcdef',actor='test',method=method,params=params)

class CheckProtocolTests(unittest.TestCase):
    def test_protocol_rejects_extra_fields_bool_ints_bad_ids_and_unbounded_profiles(self):
        good=dict(project=P,plan=P,revision=1,profile=P)
        validate_request(frame('integration.check.run',good))
        for change in ({'argv':['echo']},{'environment':{}},{'revision':True},{'profile':'bad'}):
            with self.assertRaises(CoreError): validate_request(frame('integration.check.run',dict(good,**change)))
        with self.assertRaises(CoreError): validate_request(frame('project.check-profile.list',dict(project=P,limit=257)))
    def test_preview_token_and_exactly_one_approval_mode(self):
        base=dict(project=P,action='retire',profile_id=P,confirmed=True)
        with self.assertRaises(CoreError): validate_request(frame('project.check-profile.apply',dict(base,plan_token='bad')))
        validate_request(frame('project.check-profile.apply',dict(base,plan_token='a'*32+':01234567-89ab-cdef-0123-456789abcdef')))
        approval=dict(project=P,plan=P,revision=1,token='a'*64)
        validate_request(frame('integration.approve',dict(approval,check_run=P)))
        for modes in ({}, {'check_run':P,'accept_unchecked':True}):
            with self.assertRaises(CoreError): validate_request(frame('integration.approve',dict(approval,**modes)))
    def test_one_megabyte_frame_and_bounded_pages(self):
        with self.assertRaises(CoreError): encode_frame({'data':'x'*(1024*1024)})
        with self.assertRaises(CoreError): validate_request(frame('integration.check.list',dict(project=P,plan=P,limit=257)))

from integration_fixture import IntegrationCase
class CheckServiceTests(IntegrationCase):
    def test_profile_preview_apply_and_selection_through_dispatch(self):
        definition=dict(version=1,name='smoke',environment={},steps=[dict(name='ok',argv=['true'],timeout_seconds=2,output_limit_bytes=1024)])
        params=dict(action='create',definition=definition)
        preview=self.service.handle(self.request('project.check-profile.preview',**params))
        self.assertTrue(preview['ok'],preview)
        applied=self.service.handle(self.request('project.check-profile.apply',**params,confirmed=True,plan_token=preview['result']['plan_token']))
        self.assertTrue(applied['ok'],applied)
        task=self.create_task()
        selected=self.service.handle(self.request('task.check-profile.select',task=task,profile=applied['result']['profile_id'],expected_revision=self.show(task)['revision']))
        self.assertTrue(selected['ok'],selected)
