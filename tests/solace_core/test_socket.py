import os
import socket
import threading
import unittest
import uuid
import test_service
from solace_core.server import SocketServer, bind_private_socket
from solace_core.client import CoreClient
from solace_core.protocol import encode_frame,decode_frame


class SocketTests(unittest.TestCase):
    setUpService = test_service.ServiceTests.setUp
    call = test_service.ServiceTests.call
    wait_operation = test_service.ServiceTests.wait_operation

    def setUp(self):
        self.setUpService()
        self.path=self.base/'core.sock'
        self.listener=bind_private_socket(self.path,self.base)
        self.server=SocketServer(self.listener,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)
        self.client=CoreClient(self.path)

    def stop_server(self):
        self.server.close();self.thread.join(3)
        self.listener.close()

    def test_roundtrip_and_fragmented_coalesced_frames(self):
        self.assertTrue(self.client.request('core.status',{})['ok'])
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        first=dict(version=1,id=str(uuid.uuid4()),method='project.list',params={},actor='test')
        second=dict(first,id=str(uuid.uuid4()))
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.path));stream=s.makefile('rb')
            try:
                encoded=encode_frame(first)
                s.sendall(encoded[:4]);s.sendall(encoded[4:]+encode_frame(second))
                self.assertEqual(decode_frame(stream.readline())['id'],first['id'])
                self.assertEqual(decode_frame(stream.readline())['id'],second['id'])
            finally: stream.close()

    def test_wrong_uid_refused_before_request(self):
        self.server.credential_reader=lambda s: os.getuid()+1
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2);s.connect(str(self.path))
            data=s.recv(4096)
            self.assertEqual(decode_frame(data)['error']['code'],'PERMISSION_DENIED')

    def test_malformed_and_oversized_frames_close_connection(self):
        for data in [b'[]\n',b'x'*1048577]:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(3);s.connect(str(self.path))
                try: s.sendall(data)
                except BrokenPipeError: pass
                response=decode_frame(s.makefile('rb').readline())
                self.assertEqual(response['error']['code'],'INVALID_REQUEST')

    def test_subscription_replay_resume_and_unregister(self):
        operation=self.call('checkpoint.create',project=self.project,message='first')['operation_id']
        self.wait_operation(operation)
        events=self.client.subscribe(self.project,0)
        self.addCleanup(events.close)
        first=[next(events) for _ in range(3)]
        self.assertEqual([e['seq'] for e in first],[1,2,3])
        events.close()
        second=self.call('checkpoint.create',project=self.project,message='second')['operation_id']
        self.wait_operation(second)
        resumed=self.client.subscribe(self.project,3);self.addCleanup(resumed.close)
        self.assertEqual([next(resumed)['seq'] for _ in range(3)],[4,5,6])
        self.call('project.unregister',project=self.project)
        with self.assertRaises(Exception): next(resumed)

    def test_commit_between_replay_and_wait_is_delivered(self):
        from unittest.mock import patch
        catalog=self.service.catalog(self.project)
        original=catalog.events_after
        committed=threading.Event()
        def replay(cursor,limit=256):
            rows=original(cursor,limit)
            if not rows and not committed.is_set():
                committed.set()
                # A commit just before the subscription waits must survive lost notification.
                catalog.accept_operation(dict(version=1,id=str(uuid.uuid4()),method='checkpoint.create',
                    params={'project':self.project,'message':'boundary'},actor='test'))
            return rows
        with patch.object(catalog,'events_after',side_effect=replay):
            events=self.client.subscribe(self.project,0)
            try:
                event=next(events)
                self.assertEqual(event['seq'],1)
                self.assertEqual(event['state'],'queued')
            finally: events.close()

    def test_concurrent_socket_clients_and_idle_subscriber_do_not_block(self):
        from concurrent.futures import ThreadPoolExecutor
        with socket.socket(socket.AF_UNIX) as idle:
            idle.connect(str(self.path))
            idle.sendall(encode_frame(dict(version=1,id=str(uuid.uuid4()),method='event.subscribe',
                params={'project':self.project,'after':0},actor='idle')))
            with ThreadPoolExecutor(max_workers=8) as pool:
                results=list(pool.map(lambda _: CoreClient(self.path).request('project.list',{}),range(16)))
            self.assertTrue(all(r['ok'] for r in results))
            operation=self.call('checkpoint.create',project=self.project,message='live')['operation_id']
            self.assertEqual(self.wait_operation(operation)['state'],'succeeded')

    def test_lost_response_retry_does_not_duplicate_registration(self):
        root=self.base/'new';root.mkdir()
        request_id=str(uuid.uuid4())
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.path))
            s.sendall(encode_frame(dict(version=1,id=request_id,method='project.add',params={'root':str(root)},actor='cli')))
        result=self.client.request('project.add',{'root':str(root)},request_id=request_id)
        self.assertTrue(result['ok'],result)
        projects=self.client.request('project.list',{})['result']['projects']
        self.assertEqual(len(projects),2)
