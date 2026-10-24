"""Strict integration envelopes and real socket orchestration on disposable projects."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import socket
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from integration_fixture import IntegrationCase
from solace_core.errors import CoreError
from solace_core.protocol import decode_frame, encode_frame, validate_request
from solace_core.server import SocketServer

ID = 'a' * 32
TOKEN = 'b' * 64
VALID = {
    'integration.prepare': dict(task=ID, result=ID, expected_revision=1, expected_target_revision=1),
    'integration.show': dict(plan=ID),
    'integration.diff': dict(plan=ID, revision=1, after=0, limit=128),
    'integration.resolve': dict(plan=ID, revision=1, path='literal\\name/file', take='incoming'),
    'integration.export': dict(plan=ID, revision=1, destination='/tmp/review'),
    'integration.approve': dict(plan=ID, revision=1, token=TOKEN, accept_unchecked=True),
    'integration.apply': dict(plan=ID, revision=1, approval=TOKEN, writers_paused=True),
    'integration.receipt': dict(receipt=ID, after=0, limit=128),
    'integration.abandon': dict(plan=ID, revision=1),
    'integration.recovery.show': dict(operation=ID, after=0, limit=128),
    'integration.recovery.resume': dict(operation=ID, writers_paused=True, target_path='/tmp/target', journal_path='/tmp/journal', confirmed_target='/tmp/target', confirmed_journal='/tmp/journal'),
    'integration.recovery.restore-before': dict(operation=ID, writers_paused=True),
}

VARIANTS = list(VALID.items()) + [
    ('integration.diff',dict(plan=ID,path='literal\\name/file',revision=1,version='candidate',offset=0)),
    ('integration.resolve',dict(plan=ID,path='literal\\name/file',revision=1,from_file='/tmp/edit')),
    ('integration.recovery.restore-before',dict(VALID['integration.recovery.resume'])),
]


def envelope(method, params):
    return dict(version=1, id=str(uuid.uuid4()), method=method, params=dict(project=ID, **params), actor='test')


class ValidationTests(unittest.TestCase):
    def test_every_api_row_accepts_only_known_typed_fields(self):
        from solace_core.integration_protocol import INTEGRATION_PARAMS
        self.assertEqual(set(INTEGRATION_PARAMS), set(VALID))
        for method, params in VARIANTS:
            request = envelope(method, params)
            with self.subTest(method=method): validate_request(request)
            for field in request['params']:
                for bad in (None, [], {}, False, 1.5):
                    # False is a valid type but an invalid acknowledgement.
                    changed = copy.deepcopy(request); changed['params'][field] = bad
                    with self.subTest(method=method, field=field, bad=bad), self.assertRaises(CoreError): validate_request(changed)
            for field, value in request['params'].items():
                invalid_values = []
                if type(value) is int:
                    invalid_values = [-1, True]
                    if field not in ('offset','after'): invalid_values.append(0)
                    if field=='limit': invalid_values.append(257)
                elif isinstance(value,str): invalid_values = ['']
                for bad in invalid_values:
                    changed=copy.deepcopy(request);changed['params'][field]=bad
                    with self.subTest(method=method,field=field,out_of_range=bad), self.assertRaises(CoreError):validate_request(changed)
            required, _ = INTEGRATION_PARAMS[method]
            for field in required:
                changed=copy.deepcopy(request); del changed['params'][field]
                with self.subTest(method=method, missing=field), self.assertRaises(CoreError): validate_request(changed)
            changed=copy.deepcopy(request);changed['params']['extra']='unexpected'
            with self.assertRaises(CoreError): validate_request(changed)

    def test_ranges_ids_tokens_modes_and_paths(self):
        cases = [
            ('integration.diff', dict(plan=ID, after=1)),
            ('integration.diff', dict(plan=ID, revision=0)),
            ('integration.diff', dict(plan=ID, after=-1)),
            ('integration.diff', dict(plan=ID, limit=257)),
            ('integration.diff', dict(plan=ID, limit=0)),
            ('integration.diff', dict(plan=ID, version='base')),
            ('integration.diff', dict(plan=ID, path='a', revision=1, version='invalid')),
            ('integration.diff', dict(plan=ID, path='a', version='base')),
            ('integration.diff', dict(plan=ID, path='a', revision=1, version='base', after=0)),
            ('integration.diff', dict(plan=ID, path='a', revision=1, version='base', limit=1)),
            ('integration.diff', dict(plan=ID, offset=0)),
            ('integration.diff', dict(plan=ID, path='a', revision=1, version='base', offset=-1)),
            ('integration.resolve', dict(plan=ID, revision=1, path='a')),
            ('integration.resolve', dict(plan=ID, revision=1, path='a', take='incoming', from_file='/tmp/a')),
            ('integration.resolve', dict(plan=ID, revision=1, path='a', take='base')),
            ('integration.export', dict(plan=ID, revision=1, destination='relative')),
            ('integration.export', dict(plan=ID, revision=1, destination='/tmp/../a')),
        ]
        for bad in ('', '.', '..', 'a//b', 'a/./b', 'a/../b', '/absolute', 'a\0b'):
            cases.append(('integration.resolve', dict(plan=ID, revision=1, path=bad, take='current')))
        for bad in ('A'*32, 'a'*31, 'a'*33, str(uuid.uuid4())):
            cases.append(('integration.show', dict(plan=bad)))
        for bad in ('A'*64, 'a'*63, 'a'*65, 'x'*64):
            cases.append(('integration.apply', dict(plan=ID, revision=1, approval=bad, writers_paused=True)))
        for method, params in cases:
            with self.subTest(method=method, params=params), self.assertRaises(CoreError): validate_request(envelope(method,params))
        content=envelope('integration.diff',dict(plan=ID,path='literal\\name',revision=1,version='base',offset=0))
        validate_request(content)
        edited=envelope('integration.resolve',dict(plan=ID,path='a',revision=1,from_file='/tmp/a')); validate_request(edited)
        for key in ('target_path','journal_path','confirmed_target','confirmed_journal'):
            request=envelope('integration.recovery.resume',VALID['integration.recovery.resume']);del request['params'][key]
            with self.assertRaises(CoreError): validate_request(request)
        request=envelope('integration.recovery.resume',VALID['integration.recovery.resume']);request['params']['confirmed_target']='/tmp/other'
        with self.assertRaises(CoreError): validate_request(request)


class SocketTests(IntegrationCase):
    def setUp(self):
        super().setUp()
        self.socket_path=self.base/'api.sock'
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.listener.bind(str(self.socket_path));self.listener.listen()
        self.server=SocketServer(self.listener,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.close();self.listener.close();self.thread.join(2)

    def wire(self, request):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.connect(str(self.socket_path));client.sendall(encode_frame(request))
            with client.makefile('rb') as stream: return decode_frame(stream.readline(1024*1024+1))

    def call(self, method, request_id=None, **params):
        if not hasattr(self,'server'): return super().call(method,request_id,**params)
        request=dict(version=1,id=request_id or str(uuid.uuid4()),method=method,params=params,actor='test')
        result=self.wire(request);self.assertTrue(result['ok'],result);return result['result']

    def prepared(self, changes=None):
        task,result,_=self.ready_task(changes or {'file':b'incoming'})
        request=self.prepare_request(task,result);accepted=self.wire(request);self.assertTrue(accepted['ok'],accepted)
        operation=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(operation['state'],'succeeded',operation)
        plan=self.call('integration.show',project=self.project,plan=operation['result']['plan_id'])
        return task,result,plan

    def approve(self, plan):
        request=self.request('integration.approve',plan=plan['plan_id'],revision=plan['revision'],token=plan['review_token'],accept_unchecked=True)
        result=self.wire(request);self.assertTrue(result['ok'],result);return request,result

    def test_socket_prepare_review_approve_apply_receipt_and_replay(self):
        _,_,plan=self.prepared();approval_request,approved=self.approve(plan)
        apply=self.request('integration.apply',plan=plan['plan_id'],revision=plan['revision'],approval=approved['result']['approval'],writers_paused=True)
        accepted=self.wire(apply);self.assertTrue(accepted['ok'],accepted)
        outcome=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(outcome['state'],'succeeded',outcome)
        self.assertEqual((self.root/'file').read_bytes(),b'incoming')
        receipt=self.call('integration.receipt',project=self.project,receipt=outcome['result']['receipt_id'])
        self.assertEqual(receipt['checks'],'not_run_by_solace');self.assertEqual(receipt['total'],1)
        self.assertEqual(self.wire(approval_request),approved);self.assertEqual(self.wire(apply),accepted)
        with self.service.catalog(self.project).connect() as db:
            intent=json.loads(db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?',(outcome['id'],)).fetchone()[0])
        self.assertEqual(intent['approval']['token_sha256'],hashlib.sha256(approved['result']['approval'].encode()).hexdigest())
        self.assertNotIn(approved['result']['approval'],json.dumps(intent))
        self.assertIn('integrations.v1',self.call('core.status')['capabilities'])

    def test_socket_resolve_export_abandon_and_replay_stale_approval(self):
        (self.root/'file').write_bytes(b'\0base');task,result,_=self.ready_task({'file':b'\0incoming'});(self.root/'file').write_bytes(b'\0current')
        accepted=self.wire(self.prepare_request(task,result));self.assertTrue(accepted['ok'],accepted)
        outcome=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(outcome['state'],'succeeded',outcome)
        plan=self.call('integration.show',project=self.project,plan=outcome['result']['plan_id'])
        resolve=self.request('integration.resolve',plan=plan['plan_id'],revision=1,path='file',take='incoming')
        resolved=self.wire(resolve);self.assertTrue(resolved['ok'],resolved)
        plan=resolved['result'];approval,approved=self.approve(plan)
        again=self.call('integration.resolve',project=self.project,plan=plan['plan_id'],revision=plan['revision'],path='file',take='current')
        self.assertEqual(self.wire(approval),approved)
        changed=dict(approval,actor='another actor');self.assertEqual(self.wire(changed)['error']['code'],'REQUEST_ID_REUSED')
        export=self.call('integration.export',project=self.project,plan=plan['plan_id'],revision=again['revision'],destination=str(self.base/'export'))
        self.assertEqual(self.wait_operation(export['operation_id'])['state'],'succeeded')
        self.assertEqual((self.base/'export/incoming/file').read_bytes(),b'\0incoming')
        abandoned=self.call('integration.abandon',project=self.project,plan=plan['plan_id'],revision=again['revision'])
        self.assertEqual(abandoned['state'],'abandoned')

    def test_socket_malformed_complete_frames(self):
        for frame in (b'{"a":1,"a":2}\n',b'{"a":NaN}\n',b'x'*(1024*1024)+b'\n',b'{"a":1}'):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.connect(str(self.socket_path))
                try:client.sendall(frame)
                except BrokenPipeError:pass
                if not frame.endswith(b'\n'):client.shutdown(socket.SHUT_WR)
                with client.makefile('rb') as stream: result=decode_frame(stream.readline(1024*1024+1))
                self.assertEqual(result['error']['code'],'INVALID_REQUEST')

    def restart(self):
        from solace_core.service import CoreService
        self.service.close();self.service=CoreService(self.registry,config=self.config)
        self.server.service=self.service;self.addCleanup(self.service.close)

    def apply_request(self, plan, approval):
        return self.request('integration.apply',plan=plan['plan_id'],revision=plan['revision'],approval=approval['result']['approval'],writers_paused=True)

    def test_lost_reply_restart_unregister_replays_before_source_and_cas(self):
        task,_,plan=self.prepared();approval,approved=self.approve(plan);request=self.apply_request(plan,approved)
        # Send the accepted mutation and deliberately discard its reply.
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.connect(str(self.socket_path));client.sendall(encode_frame(request));client.shutdown(socket.SHUT_RDWR)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with self.service.catalog(self.project).connect() as db:
                row=db.execute('SELECT id FROM operations WHERE request_id=?',(request['id'],)).fetchone()
            if row:break
            time.sleep(.01)
        self.assertIsNotNone(row)
        done=self.wait_operation(row[0]);self.assertEqual(done['state'],'succeeded',done)
        accepted=self.wire(request);self.mutate('task.cancel',task)
        catalog=self.service.catalog(self.project)
        self.call('project.unregister',project=self.project)
        self.root.rename(self.base/'disconnected-source')
        (catalog.store/'objects').rename(catalog.store/'disconnected-objects')
        self.restart()
        self.assertEqual(self.wire(request),accepted);self.assertEqual(self.wire(approval),approved)
        receipt=self.call('integration.receipt',project=self.project,receipt=done['result']['receipt_id'])
        self.assertEqual(receipt['after_id'],done['result']['after_id'])
        self.assertEqual(self.call('integration.show',project=self.project,plan=plan['plan_id'])['state'],'applied')
        self.assertEqual(self.call('integration.diff',project=self.project,plan=plan['plan_id'])['total'],1)
        self.assertEqual(self.call('operation.show',project=self.project,operation=done['id'])['state'],'succeeded')
        fresh=self.request('integration.abandon',plan=plan['plan_id'],revision=plan['revision'])
        self.assertEqual(self.wire(fresh)['error']['code'],'PROJECT_NOT_FOUND')

    def test_missing_cas_allows_metadata_but_fresh_io_refuses_without_mutation(self):
        _,_,plan=self.prepared();approval,approved=self.approve(plan)
        catalog=self.service.catalog(self.project);original=(catalog.store/'objects');original.rename(catalog.store/'saved-objects')
        self.assertEqual(self.wire(approval),approved)
        self.assertTrue(self.wire(self.request('integration.show',plan=plan['plan_id']))['ok'])
        self.assertTrue(self.wire(self.request('integration.diff',plan=plan['plan_id']))['ok'])
        for request in (
            self.apply_request(plan,approved),
            self.request('integration.diff',plan=plan['plan_id'],revision=1,path='file',version='incoming'),
            self.request('integration.export',plan=plan['plan_id'],revision=1,destination=str(self.base/'refused-export')),
            self.request('checkpoint.create',message='refused'),
        ):
            response=self.wire(request);self.assertFalse(response['ok'],response)
            self.assertEqual(response['error']['code'],'STORE_UNAVAILABLE')
        self.assertFalse((self.root/'file').exists());self.assertFalse(original.exists());self.assertFalse((self.base/'refused-export').exists())
        with catalog.connect(metadata=True) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],0)

    def test_request_id_namespace_collisions_cover_sync_and_async_and_actor(self):
        _,_,plan=self.prepared();approval,approved=self.approve(plan)
        collision=dict(approval,method='integration.apply',params=self.apply_request(plan,approved)['params'])
        self.assertEqual(self.wire(collision)['error']['code'],'REQUEST_ID_REUSED')
        apply=self.apply_request(plan,approved);accepted=self.wire(apply);self.assertTrue(accepted['ok'],accepted)
        done=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(done['state'],'succeeded',done)
        for changed in (dict(apply,actor='other'),dict(apply,method='integration.abandon',params=dict(project=self.project,plan=plan['plan_id'],revision=1))):
            self.assertEqual(self.wire(changed)['error']['code'],'REQUEST_ID_REUSED')
        task_create=self.request('task.create',title='collision');task_create['id']=apply['id']
        self.assertEqual(self.wire(task_create)['error']['code'],'REQUEST_ID_REUSED')

    def test_acceptance_claims_precede_scheduling_and_worker_rechecks(self):
        from solace_core.integration_operations import IntegrationOperations
        task,result,_=self.ready_task({'file':b'accepted'})
        catalog=self.service.catalog(self.project);coordinator=self.service.coordinator(self.project)
        queued=[]
        with patch.object(coordinator,'submit',side_effect=lambda op,fn:queued.append((op,fn))):
            request=self.prepare_request(task,result);accepted=self.wire(request);self.assertTrue(accepted['ok'],accepted)
        operation=accepted['result']['operation_id']
        with catalog.connect() as db:
            self.assertEqual(db.execute('SELECT operation_id FROM task_claims WHERE task_id=?',(task,)).fetchone()[0],operation)
            self.assertEqual(db.execute('SELECT path FROM integration_claims WHERE operation_id=?',(operation,)).fetchone()[0],str(self.root))
        self.assertFalse(self.wire(self.request('checkpoint.create',message='blocked'))['ok'])
        with catalog.connect(write=True) as db:db.execute('UPDATE tasks SET revision=revision+1 WHERE id=?',(task,))
        queued[0][1]()
        done=catalog.get_operation(operation);self.assertEqual(done['state'],'failed',done)
        with catalog.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM task_claims WHERE operation_id=?',(operation,)).fetchone()[0],0)

    def test_apply_selects_supplied_approval_and_retains_uncertain_claims(self):
        from solace_core.integration_publish import IntegrationPublisher
        _,_,plan=self.prepared();_,approved=self.approve(plan);catalog=self.service.catalog(self.project)
        selected_hash=hashlib.sha256(approved['result']['approval'].encode()).hexdigest()
        with catalog.connect(write=True) as db:
            selected=dict(db.execute('SELECT * FROM integration_approvals WHERE token_sha256=?',(selected_hash,)).fetchone())
            db.execute('INSERT INTO integration_approvals VALUES (?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,selected['plan_id'],selected['revision'],hashlib.sha256(b'different').hexdigest(),selected['binding_json'],'later',1,catalog._now()))
        def stage(publisher,operation,retained):
            with catalog.connect() as db:
                intent=json.loads(db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?',(operation,)).fetchone()[0])
            self.assertEqual(intent['approval'],dict(id=selected['id'],token_sha256=selected_hash,binding=json.loads(selected['binding_json'])))
            raise OSError('uncertain publication canary-free')
        with patch.object(IntegrationPublisher,'stage',stage):
            accepted=self.wire(self.apply_request(plan,approved));self.assertTrue(accepted['ok'],accepted)
            done=self.wait_operation(accepted['result']['operation_id'])
        self.assertEqual(done['state'],'interrupted',done)
        with catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?',(done['id'],)).fetchone()[0],2)
            self.assertEqual(db.execute('SELECT state FROM integration_plans WHERE id=?',(plan['plan_id'],)).fetchone()[0],'recovery_required')
        self.assertEqual(self.call('integration.recovery.show',project=self.project,operation=done['id'])['state'],'recovery_required')

    def test_invalid_or_changed_approval_binding_cannot_reserve(self):
        _,_,plan=self.prepared();_,approved=self.approve(plan);catalog=self.service.catalog(self.project)
        request=self.apply_request(plan,approved);wrong=copy.deepcopy(request);wrong['params']['approval']='0'*64
        self.assertEqual(self.wire(wrong)['error']['code'],'INTEGRATION_PLAN_STALE')
        with catalog.connect(write=True) as db:db.execute("UPDATE integration_approvals SET binding_json='{}'")
        self.assertEqual(self.wire(request)['error']['code'],'INTEGRATION_PLAN_STALE')
        with catalog.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],0)

    def test_notify_exceptions_preserve_committed_approval_export_and_apply(self):
        _,_,plan=self.prepared();catalog=self.service.catalog(self.project)
        with self.assertLogs('solace_core.integration_operations',level='ERROR') as diagnostics, patch.object(catalog,'notify',side_effect=RuntimeError('notification failed')):
            approval,approved=self.approve(plan)
            request=self.apply_request(plan,approved);accepted=self.wire(request);self.assertTrue(accepted['ok'],accepted)
            done=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(done['state'],'succeeded',done)
            export=self.call('integration.export',project=self.project,plan=plan['plan_id'],revision=1,destination=str(self.base/'notify-export'))
            self.assertEqual(self.wait_operation(export['operation_id'])['state'],'succeeded')
            drained=threading.Event()
            self.service.coordinator(self.project).submit(uuid.uuid4().hex,drained.set)
            self.assertTrue(drained.wait(5),'Notification workers did not finish')
        self.assertEqual([record.getMessage() for record in diagnostics.records],
                         ['integration notification unavailable exception=RuntimeError']*4)
        self.assertEqual(self.wire(approval),approved);self.assertEqual(self.wire(request),accepted)
        with catalog.connect() as db:self.assertEqual(db.execute('SELECT phase FROM integration_exports').fetchone()[0],'published')

    def test_paused_integration_keeps_other_project_checkpoint_available(self):
        from solace_core.integration_publish import IntegrationPublisher
        other_root=self.base/'other-project';other_root.mkdir();(other_root/'keep').write_bytes(b'other project')
        other=self.call('project.add',root=str(other_root))['id']
        _,_,plan=self.prepared();_,approved=self.approve(plan)
        entered,release=threading.Event(),threading.Event()
        original_init=IntegrationPublisher.__init__
        def initialize(publisher,service,catalog,**kwargs):
            original_init(publisher,service,catalog,**kwargs)
            if catalog.registration['id']==self.project:
                def pause(point):
                    if point=='intent:0':
                        entered.set()
                        if not release.wait(10): raise RuntimeError('Disposable integration pause was not released')
                publisher.failpoint=pause
        with patch.object(IntegrationPublisher,'__init__',initialize):
            try:
                accepted=self.wire(self.apply_request(plan,approved));self.assertTrue(accepted['ok'],accepted)
                self.assertTrue(entered.wait(5),'Integration did not reach its durable action intent')
                self.assertFalse((self.root/'file').exists())
                blocked=self.wire(self.request('checkpoint.create',message='claimed target'))
                self.assertEqual(blocked['error']['code'],'INTEGRATION_TARGET_BUSY')
                operation=self.call('checkpoint.create',project=other,message='independent capture')['operation_id']
                outcome=self.wait_operation(operation,project=other)
                self.assertEqual(outcome['state'],'succeeded',outcome)
                self.assertEqual(self.service.catalog(self.project).get_operation(accepted['result']['operation_id'])['state'],'running')
                self.assertEqual((other_root/'keep').read_bytes(),b'other project')
            finally: release.set()
            self.assertEqual(self.wait_operation(accepted['result']['operation_id'])['state'],'succeeded')

    def test_recovery_resume_and_restore_before_over_socket_preserve_history(self):
        from solace_core.integration_publish import IntegrationPublisher
        for mode in ('resume','restore-before'):
            # Distinct project fixture target state for each explicit recovery.
            _,_,plan=self.prepared({'file':('incoming-'+mode).encode()});_,approved=self.approve(plan)
            original_publish=IntegrationPublisher.publish
            def fail_after_move(publisher,journal):
                def failpoint(name):
                    if name.startswith('old_moved:') or name.startswith('new_published:'): raise OSError('lost progress')
                publisher.failpoint=failpoint;return original_publish(publisher,journal)
            before=(self.root/'file').read_bytes() if (self.root/'file').exists() else None
            with patch.object(IntegrationPublisher,'publish',fail_after_move):
                accepted=self.wire(self.apply_request(plan,approved));self.assertTrue(accepted['ok'],accepted)
                interrupted=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(interrupted['state'],'interrupted',interrupted)
            original=self.call('operation.show',project=self.project,operation=interrupted['id'])
            self.restart()
            shown=self.call('integration.recovery.show',project=self.project,operation=interrupted['id'],limit=1)
            self.assertEqual(shown['state'],'recovery_required')
            request=self.request('integration.recovery.'+mode,operation=interrupted['id'],writers_paused=True)
            recovered=self.wire(request);self.assertTrue(recovered['ok'],recovered)
            done=self.wait_operation(recovered['result']['operation_id']);self.assertEqual(done['state'],'succeeded',done)
            self.assertEqual(self.wire(request),recovered)
            self.assertEqual(self.call('operation.show',project=self.project,operation=interrupted['id']),original)
            if mode=='resume':self.assertEqual((self.root/'file').read_bytes(),b'incoming-resume')
            elif before is not None:self.assertEqual((self.root/'file').read_bytes(),before)
            else:self.assertFalse((self.root/'file').exists())

    def test_large_metadata_pages_and_explicit_content_bound_actual_frames(self):
        from solace_core.observations import encoded_size,PAGE_BYTES,SUMMARY_BYTES
        data=b'\0file-content'+bytes(range(256))*500
        _,_,plan=self.prepared({'large':data});catalog=self.service.catalog(self.project)
        metadata=json.dumps(dict(base=None,current=None,incoming=None,candidate=None))
        with catalog.connect(write=True) as db:
            for index in range(180):
                db.execute('INSERT INTO integration_decisions VALUES (?,?,?,?,?,?,?)',(plan['plan_id'],1,index+1,'escaped-'+str(index)+'é'*250,'keep_current',None,metadata))
        cursor=0;count=0
        while True:
            response=self.wire(self.request('integration.diff',plan=plan['plan_id'],revision=1,after=cursor,limit=256));self.assertTrue(response['ok'],response)
            page=response['result'];self.assertLessEqual(encoded_size(page),PAGE_BYTES);self.assertLessEqual(len(encode_frame(response)),1024*1024)
            self.assertNotIn('data',json.dumps(page));count+=len(page['entries'])
            if page['next_after'] is None:break
            self.assertGreater(page['next_after'],cursor);cursor=page['next_after']
        self.assertEqual(count,181)
        content=bytearray();offset=0
        while True:
            page=self.call('integration.diff',project=self.project,plan=plan['plan_id'],revision=1,path='large',version='incoming',offset=offset)
            block=base64.b64decode(page['data']);self.assertLessEqual(len(block),48*1024);self.assertLessEqual(encoded_size(page),PAGE_BYTES);content.extend(block)
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.assertEqual(content,data);self.assertLessEqual(encoded_size(self.call('integration.show',project=self.project,plan=plan['plan_id'])),SUMMARY_BYTES)
        _,approved=self.approve(plan)
        applied=self.wire(self.apply_request(plan,approved));self.assertTrue(applied['ok'],applied)
        done=self.wait_operation(applied['result']['operation_id']);self.assertEqual(done['state'],'succeeded',done)
        cursor=0;received=0
        while True:
            response=self.wire(self.request('integration.receipt',receipt=done['result']['receipt_id'],after=cursor,limit=256));self.assertTrue(response['ok'],response)
            self.assertLessEqual(encoded_size(response['result']),PAGE_BYTES);self.assertLessEqual(len(encode_frame(response)),1024*1024)
            received+=len(response['result']['entries'])
            if response['result']['next_after'] is None:break
            self.assertGreater(response['result']['next_after'],cursor);cursor=response['result']['next_after']
        self.assertEqual(received,181)
        with catalog.connect(write=True) as db:db.execute('UPDATE integration_decisions SET path=? WHERE plan_id=? AND ordinal=1',('é'*25000,plan['plan_id']))
        oversized=self.wire(self.request('integration.diff',plan=plan['plan_id'],limit=256))
        # Sort order places the giant row last; page until it is reached.
        while oversized['ok'] and oversized['result']['next_after'] is not None:
            oversized=self.wire(self.request('integration.diff',plan=plan['plan_id'],revision=1,after=oversized['result']['next_after'],limit=256))
        self.assertFalse(oversized['ok']);self.assertEqual(oversized['error']['code'],'RESPONSE_TOO_LARGE')

    def test_prompt_canary_never_copied_to_durable_integration_or_logs(self):
        import logging
        canary='PRIVATE-PROMPT-'+uuid.uuid4().hex
        records=[]
        class Capture(logging.Handler):
            def emit(self,record):records.append(record.getMessage())
        handler=Capture();logging.getLogger().addHandler(handler);self.addCleanup(logging.getLogger().removeHandler,handler)
        task=self.create_task(prompt=canary);started=self.start(task);(Path(started['path'])/'file').write_bytes(b'incoming')
        result=self.wait_operation(self.mutate('task.ready',task)['operation_id'])['result']['checkpoint_id']
        prepare=self.wire(self.prepare_request(task,result));self.assertTrue(prepare['ok'],prepare)
        done=self.wait_operation(prepare['result']['operation_id']);plan=self.call('integration.show',project=self.project,plan=done['result']['plan_id'])
        _,approved=self.approve(plan);accepted=self.wire(self.apply_request(plan,approved));self.assertTrue(accepted['ok'],accepted)
        self.assertEqual(self.wait_operation(accepted['result']['operation_id'])['state'],'succeeded')
        self.mutate('task.prompt.delete',task)
        catalog=self.service.catalog(self.project)
        with catalog.connect() as db:
            tables=['operations','events','task_requests']+[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'integration_%'")]
            for table in tables:self.assertNotIn(canary,str([tuple(r) for r in db.execute('SELECT * FROM '+table)]),table)
        self.assertNotIn(canary,'\n'.join(records))

    def prompt_change_after_approval(self, mode):
        from datetime import datetime, timedelta, timezone
        from solace_core.prompts import PromptStore
        canary='REDACTED-INTEGRATION-CANARY-'+uuid.uuid4().hex
        task=self.create_task(prompt=canary,prompt_policy='days',prompt_days=1)
        workspace=Path(self.start(task)['path']);(workspace/'file').write_bytes(b'incoming')
        result=self.wait_operation(self.mutate('task.ready',task)['operation_id'])['result']['checkpoint_id']
        prepared=self.wire(self.prepare_request(task,result))
        done=self.wait_operation(prepared['result']['operation_id'])
        plan=self.call('integration.show',project=self.project,plan=done['result']['plan_id'])
        approval,approved=self.approve(plan)
        catalog=self.service.catalog(self.project)
        if mode=='deleted': self.mutate('task.prompt.delete',task)
        else:
            future=datetime.now(timezone.utc)+timedelta(days=2)
            # At-rest status/body still say available while the read-time
            # expiry clock says expired: that mismatch must not expose bytes.
            with catalog.connect() as db:
                self.assertEqual(db.execute('SELECT status,body FROM task_prompts WHERE task_id=?',(task,)).fetchone()[:],('available',canary))
            with patch('solace_core.prompts.utcnow',return_value=future):
                expired=self.call('task.prompt.show',project=self.project,task=task)
                self.assertEqual(expired['status'],'expired');self.assertNotIn('body',expired)
                self.assertEqual(self.wire(approval),approved)
                self.assertNotIn(canary,json.dumps(self.call('integration.show',project=self.project,plan=plan['plan_id'])))
            self.assertEqual(PromptStore(catalog,now=lambda:future).purge_expired(),1)
        prompt=self.call('task.prompt.show',project=self.project,task=task)
        self.assertEqual(prompt['status'],mode);self.assertNotIn('body',prompt)
        # Historic approval replay stays exact, but its pinned task revision
        # cannot authorize fresh application after redaction or expiry.
        self.assertEqual(self.wire(approval),approved)
        rejected=self.wire(self.apply_request(plan,approved))
        self.assertFalse(rejected['ok'],rejected)
        self.assertEqual(rejected['error']['code'],'INTEGRATION_PLAN_STALE')
        self.assertFalse((self.root/'file').exists())
        self.assertNotIn(canary,json.dumps([approved,rejected,prompt]))
        with catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],0)
            for table in ('integration_approvals','integration_receipts','integration_revisions','operations','events','task_requests'):
                self.assertNotIn(canary,str([tuple(row) for row in db.execute('SELECT * FROM '+table)]),table)

    def test_redacted_prompt_revision_cannot_reuse_approval_or_leak_body(self):
        self.prompt_change_after_approval('deleted')

    def test_expired_prompt_revision_cannot_reuse_approval_or_leak_body(self):
        self.prompt_change_after_approval('expired')

    def test_wrong_typed_fields_rejected_over_socket_before_dispatch(self):
        for method,params in VARIANTS:
            for field in dict(project=ID,**params):
                bad=envelope(method,params);bad['params'][field]=[]
                response=self.wire(bad)
                self.assertEqual(response['error']['code'],'INVALID_REQUEST',(method,field,response))

    def test_export_reserves_both_paths_before_queue_and_rechecks_parent(self):
        _,_,plan=self.prepared();catalog=self.service.catalog(self.project);coordinator=self.service.coordinator(self.project)
        queued=[];parent=self.base/'review-parent';parent.mkdir();destination=parent/'export'
        with patch.object(coordinator,'submit',side_effect=lambda op,fn:queued.append((op,fn))):
            accepted=self.wire(self.request('integration.export',plan=plan['plan_id'],revision=1,destination=str(destination)));self.assertTrue(accepted['ok'],accepted)
        operation=accepted['result']['operation_id']
        with catalog.connect() as db:
            paths={r[0] for r in db.execute('SELECT path FROM integration_claims WHERE operation_id=?',(operation,))}
        self.assertEqual(paths,{str(destination),str(parent/('.solace-export-'+operation))})
        blocked=self.wire(self.request('project.add',root=str(parent)));self.assertFalse(blocked['ok'],blocked)
        parent.rename(self.base/'old-review-parent');parent.mkdir()
        queued[0][1]()
        self.assertIn(catalog.get_operation(operation)['state'],('failed','interrupted'))
        self.assertFalse(destination.exists())
        with catalog.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?',(operation,)).fetchone()[0],2)

    def test_apply_worker_rechecks_claims_and_preserves_reserved_recovery(self):
        _,_,plan=self.prepared();_,approved=self.approve(plan);catalog=self.service.catalog(self.project);coordinator=self.service.coordinator(self.project)
        queued=[]
        with patch.object(coordinator,'submit',side_effect=lambda op,fn:queued.append((op,fn))):
            accepted=self.wire(self.apply_request(plan,approved));self.assertTrue(accepted['ok'],accepted)
        operation=accepted['result']['operation_id']
        with catalog.connect(write=True) as db:db.execute('DELETE FROM task_claims WHERE operation_id=?',(operation,))
        queued[0][1]()
        done=catalog.get_operation(operation);self.assertEqual(done['state'],'interrupted',done)
        self.assertFalse((self.root/'file').exists())
        with catalog.connect() as db:
            row=db.execute('SELECT journal_path,identity_json,phase FROM integration_journals WHERE operation_id=?',(operation,)).fetchone()
            self.assertEqual(row['phase'],'reserved');self.assertIsNone(row['identity_json']);self.assertFalse(Path(row['journal_path']).exists())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?',(operation,)).fetchone()[0],2)

    def test_stale_revision_approval_and_root_child_revision_contract(self):
        (self.root/'file').write_bytes(b'\0base');task,result,_=self.ready_task({'file':b'\0incoming'});(self.root/'file').write_bytes(b'\0current')
        wrong=self.prepare_request(task,result);wrong['params']['expected_target_revision']=1
        self.assertEqual(self.wire(wrong)['error']['code'],'INVALID_REQUEST')
        prepared=self.wire(self.prepare_request(task,result));done=self.wait_operation(prepared['result']['operation_id'])
        plan=self.call('integration.resolve',project=self.project,plan=done['result']['plan_id'],revision=1,path='file',take='incoming')
        _,approved=self.approve(plan)
        self.call('integration.resolve',project=self.project,plan=plan['plan_id'],revision=plan['revision'],path='file',take='current')
        rejected=self.wire(self.apply_request(plan,approved));self.assertFalse(rejected['ok']);self.assertEqual(rejected['error']['code'],'INTEGRATION_PLAN_STALE')
        parent=self.create_task();self.start(parent)
        child,result,_=self.ready_task({'child':b'child'},parent=parent)
        request=self.prepare_request(child,result);del request['params']['expected_target_revision']
        self.assertEqual(self.wire(request)['error']['code'],'INVALID_REQUEST')
        accepted=self.wire(self.prepare_request(child,result));self.assertTrue(accepted['ok'],accepted)
        child_plan=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(child_plan['state'],'succeeded',child_plan)

    def test_schema_two_task_reads_remain_supported_and_integration_refuses(self):
        task=self.create_task();catalog=self.service.catalog(self.project)
        with catalog.connect(write=True) as db:db.execute('UPDATE core_metadata SET schema_version=2')
        self.assertEqual(self.show(task)['id'],task)
        response=self.wire(self.request('integration.show',plan=ID));self.assertFalse(response['ok'])
        self.assertEqual(response['error']['code'],'PROJECT_UPGRADE_REQUIRED')
        with catalog.connect() as db:self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0],2)

    def test_corrupt_cas_refuses_apply_and_content_without_live_mutation(self):
        _,_,plan=self.prepared();_,approved=self.approve(plan);catalog=self.service.catalog(self.project)
        from solace_core.integration_snapshots import SnapshotStore
        store=SnapshotStore(catalog)
        with catalog.connect() as db:
            checkpoint=db.execute('SELECT incoming_id FROM integration_revisions WHERE plan_id=?',(plan['plan_id'],)).fetchone()[0]
            digest=db.execute('SELECT digest FROM chunk_refs WHERE checkpoint_id=?',(checkpoint,)).fetchone()[0]
        obj=store.objects._object_path(digest);obj.write_bytes(b'corrupt!')
        content=self.wire(self.request('integration.diff',plan=plan['plan_id'],revision=1,path='file',version='incoming'))
        self.assertFalse(content['ok'],content)
        response=self.wire(self.apply_request(plan,approved));self.assertFalse(response['ok'],response)
        self.assertFalse((self.root/'file').exists())
        with catalog.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],0)

    def test_historical_new_request_apply_does_not_recapture_or_republish(self):
        _,_,plan=self.prepared();_,approved=self.approve(plan);request=self.apply_request(plan,approved)
        first=self.wire(request);done=self.wait_operation(first['result']['operation_id']);self.assertEqual(done['state'],'succeeded',done)
        (self.root/'file').write_bytes(b'later destination-only edits')
        catalog=self.service.catalog(self.project);(catalog.store/'objects').rename(catalog.store/'offline-objects')
        another=dict(request,id=str(uuid.uuid4()));accepted=self.wire(another);self.assertTrue(accepted['ok'],accepted)
        historical=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(historical['state'],'succeeded',historical)
        self.assertEqual(historical['result']['receipt_id'],done['result']['receipt_id']);self.assertTrue(historical['result']['already_applied'])
        self.assertEqual((self.root/'file').read_bytes(),b'later destination-only edits')
        with catalog.connect(metadata=True) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],1)

    def test_recovery_acceptance_and_success_survive_notify_failure(self):
        from solace_core.integration_publish import IntegrationPublisher
        _,_,plan=self.prepared();_,approved=self.approve(plan);catalog=self.service.catalog(self.project)
        with patch.object(IntegrationPublisher,'stage',side_effect=OSError('disposable pause')):
            accepted=self.wire(self.apply_request(plan,approved));done=self.wait_operation(accepted['result']['operation_id'])
        self.assertEqual(done['state'],'interrupted',done)
        original=catalog.get_operation(done['id'])
        with patch.object(catalog,'notify',side_effect=OSError('delivery only')):
            request=self.request('integration.recovery.resume',operation=done['id'],writers_paused=True)
            recovery=self.wire(request);self.assertTrue(recovery['ok'],recovery)
            succeeded=self.wait_operation(recovery['result']['operation_id']);self.assertEqual(succeeded['state'],'succeeded',succeeded)
        self.assertEqual(catalog.get_operation(done['id']),original)
        self.assertEqual(self.wire(request),recovery)

    def test_relink_restart_offline_cas_replay_precedes_pending_reconciliation(self):
        _,_,plan=self.prepared();approval,approved=self.approve(plan)
        catalog=self.service.catalog(self.project)
        # An explicit same-location relink creates the durable receipt that
        # startup uses when deciding whether cache reconciliation is necessary.
        self.call('project.relink',project=self.project,root=str(self.root),store=str(catalog.store),confirmed_root=str(self.root))
        (catalog.store/'objects').rename(catalog.store/'offline-objects')
        with self.assertLogs('solace_core.maintenance',level='WARNING') as diagnostics:
            self.restart()
        self.assertEqual([record.getMessage() for record in diagnostics.records],
                         ['prompt maintenance project='+self.project+' exception=CoreError'])
        self.assertEqual(self.wire(approval),approved)
        self.assertEqual(self.call('integration.show',project=self.project,plan=plan['plan_id'])['state'],'approved')
        # Force the stopped-core reconciliation condition. Replay and metadata
        # may not try to finish a handoff; fresh mutations must take that gate.
        self.service._pending_recovery.add(self.project)
        with patch.object(self.service,'_reconcile_location',side_effect=CoreError('STORE_UNAVAILABLE','Offline content prevents reconciliation.','Reconnect storage.')) as reconcile:
            self.assertEqual(self.wire(approval),approved)
            self.assertTrue(self.wire(self.request('integration.diff',plan=plan['plan_id']))['ok'])
            self.assertFalse(reconcile.called)
            fresh=self.wire(self.request('integration.abandon',plan=plan['plan_id'],revision=1))
            self.assertEqual(fresh['error']['code'],'STORE_UNAVAILABLE');reconcile.assert_called_once()
        self.assertFalse((catalog.store/'objects').exists())
        with catalog.connect(metadata=True) as db:self.assertEqual(db.execute('SELECT state FROM integration_plans WHERE id=?',(plan['plan_id'],)).fetchone()[0],'approved')

    def test_stale_catalog_is_refused_at_acceptance_and_synchronous_read_entry(self):
        from solace_core.integration_operations import IntegrationOperations
        from solace_core.integration_claims import IntegrationClaims
        _,_,plan=self.prepared();_,approved=self.approve(plan)
        catalog=self.service.catalog(self.project)
        operations=IntegrationOperations(self.service,catalog)
        new_root=self.base/'relinked-source';new_root.mkdir()
        self.call('project.relink',project=self.project,root=str(new_root),store=str(catalog.store),confirmed_root=str(new_root))
        with self.assertRaises(CoreError) as failure:operations.accept(self.apply_request(plan,approved))
        self.assertEqual(failure.exception.code,'INTEGRATION_PLAN_STALE')
        with self.assertRaises(CoreError) as failure:
            with IntegrationClaims(self.service,catalog).read_access([self.root]):self.fail('Stale source read was admitted.')
        self.assertEqual(failure.exception.code,'INTEGRATION_PLAN_STALE')
        with catalog.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],0)
        self.assertFalse((self.root/'file').exists());self.assertFalse((new_root/'file').exists())

    def test_historical_root_prepare_enforces_shape_after_exact_replay_without_cas(self):
        task,result,_=self.ready_task({'file':b'incoming'})
        request=self.prepare_request(task,result);accepted=self.wire(request);self.assertTrue(accepted['ok'],accepted)
        done=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(done['state'],'succeeded',done)
        plan=self.call('integration.show',project=self.project,plan=done['result']['plan_id']);_,approved=self.approve(plan)
        apply=self.wire(self.apply_request(plan,approved));applied=self.wait_operation(apply['result']['operation_id']);self.assertEqual(applied['state'],'succeeded',applied)
        catalog=self.service.catalog(self.project);(catalog.store/'objects').rename(catalog.store/'offline-objects')
        self.assertEqual(self.wire(request),accepted)
        wrong=self.request('integration.prepare',task=task,result=result,expected_revision=1,expected_target_revision=1)
        rejected=self.wire(wrong);self.assertFalse(rejected['ok'],rejected)
        self.assertEqual(rejected['error']['code'],'INVALID_REQUEST')
        historical=self.wire(self.request('integration.prepare',task=task,result=result,expected_revision=1));self.assertTrue(historical['ok'],historical)
        replay=self.wait_operation(historical['result']['operation_id']);self.assertEqual(replay['state'],'succeeded',replay)
        self.assertEqual(replay['result']['receipt_id'],applied['result']['receipt_id'])
        self.assertTrue(replay['result']['already_applied']);self.assertFalse((catalog.store/'objects').exists())

    def test_historical_child_prepare_requires_target_revision_but_not_freshness_or_cas(self):
        parent=self.create_task();started=self.start(parent)
        task,result,_=self.ready_task({'child':b'incoming child'},parent=parent)
        request=self.prepare_request(task,result);accepted=self.wire(request);self.assertTrue(accepted['ok'],accepted)
        done=self.wait_operation(accepted['result']['operation_id']);self.assertEqual(done['state'],'succeeded',done)
        plan=self.call('integration.show',project=self.project,plan=done['result']['plan_id']);_,approved=self.approve(plan)
        apply=self.wire(self.apply_request(plan,approved));self.assertTrue(apply['ok'],apply)
        applied=self.wait_operation(apply['result']['operation_id']);self.assertEqual(applied['state'],'succeeded',applied)
        self.assertEqual((Path(started['path'])/'child').read_bytes(),b'incoming child');self.assertFalse((self.root/'child').exists())
        catalog=self.service.catalog(self.project);(catalog.store/'objects').rename(catalog.store/'offline-objects')
        self.assertEqual(self.wire(request),accepted)
        wrong=self.request('integration.prepare',task=task,result=result,expected_revision=1)
        rejected=self.wire(wrong);self.assertFalse(rejected['ok'],rejected)
        self.assertEqual(rejected['error']['code'],'INVALID_REQUEST')
        historical=self.wire(self.request('integration.prepare',task=task,result=result,expected_revision=1,expected_target_revision=1));self.assertTrue(historical['ok'],historical)
        replay=self.wait_operation(historical['result']['operation_id']);self.assertEqual(replay['state'],'succeeded',replay)
        self.assertEqual(replay['result']['receipt_id'],applied['result']['receipt_id'])
        self.assertTrue(replay['result']['already_applied']);self.assertFalse((catalog.store/'objects').exists())
