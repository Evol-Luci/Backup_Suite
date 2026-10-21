"""Disposable real Unix socket/CLI result integration workflows."""
import base64
import json
import io
import logging
import os
from pathlib import Path
import uuid
import threading
from unittest.mock import patch
from integration_fixture import IntegrationCase
import test_task_acceptance
from solace_core.server import SocketServer, bind_private_socket


class IntegrationAcceptanceTests(IntegrationCase):
    result = test_task_acceptance.AcceptanceTests.result
    stop_server = test_task_acceptance.AcceptanceTests.stop_server

    def run_cli(self,*args,success=True):
        result=test_task_acceptance.AcceptanceTests.run_cli(self,*args,success=False)
        if success:self.assertEqual(result.returncode,0,(result.stdout+result.stderr).decode())
        self.assertNotIn(b'INTEGRATION PRIVATE CANARY',result.stdout+result.stderr)
        return result

    def setUp(self):
        super().setUp();self.path=self.base/'core.sock'
        self.listener=bind_private_socket(self.path,self.base);self.server=SocketServer(self.listener,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)
        self.env=dict(os.environ,XDG_RUNTIME_DIR=str(self.base),XDG_DATA_HOME=str(self.base/'xdg'),HOME=str(self.base/'home'))
        self.logs=io.StringIO();handler=logging.StreamHandler(self.logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler,handler)

    def completed(self,*args):
        result=self.result(*args,'--wait')
        self.assertEqual(result['state'],'succeeded',result)
        return result['result']

    def plan(self, task, result, *extra):
        prepared=self.completed('integration','prepare',self.project,task,'--result',result,*extra)
        return self.result('integration','show',self.project,prepared['plan_id'])

    def approval(self, plan, *extra):
        return self.result('integration','approve',self.project,plan['plan_id'],'--revision',str(plan['revision']),
                           '--token',plan['review_token'],'--accept-unchecked',*extra)['approval']

    def apply(self, plan, approval=None, *extra):
        return self.completed('integration','apply',self.project,plan['plan_id'],'--revision',str(plan['revision']),
                              '--approval',approval or self.approval(plan),'--writers-paused',*extra)

    def failed(self, *args):
        result=self.run_cli(*args,'--json',success=False)
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertEqual(result.stderr,b'')
        self.assertEqual(len(result.stdout.splitlines()),1)
        return json.loads(result.stdout)

    def test_prepare_does_not_apply_and_requires_reviewed_revision(self):
        task,result,_=self.ready_cli_task({'a.txt':b'incoming\n'})
        plan=self.plan(task,result)
        self.assertEqual(plan['state'],'prepared');self.assertFalse((self.root/'a.txt').exists())
        denied=self.failed('integration','approve',self.project,plan['plan_id'],'--revision',str(plan['revision']),'--token',plan['review_token'])
        self.assertIn('--accept-unchecked',denied['error']['message'])
        stale=self.failed('integration','approve',self.project,plan['plan_id'],'--revision','99','--token',plan['review_token'],'--accept-unchecked')
        self.assertEqual(stale['error']['code'],'INTEGRATION_REVISION_CONFLICT')
        output=self.run_cli('integration','show',self.project,plan['plan_id']).stdout.decode()
        self.assertIn('Solace did not run checks',output);self.assertIn('solace integration diff',output)

    def test_root_child_source_bytes_ancestry_protection_and_prompt_privacy(self):
        (self.root/'base').write_bytes(b'base\r\nno-final-newline')
        protected={'.git':b'gitdir: private', 'nested/.git':b'nested-git', 'nested/.solace_vault/history':b'vault\0bytes'}
        for name,data in protected.items():
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        source=self.wait_operation(self.result('checkpoint','create',self.project,'source')['operation_id'])['result']['checkpoint_id']
        prompt=self.base/'prompt';prompt.write_text('INTEGRATION PRIVATE CANARY')
        parent=self.result('task','create',self.project,'root','--prompt-file',str(prompt))['task_id']
        parent_root=Path(self.completed('task','start',self.project,parent)['path'])
        child,child_result,_=self.ready_cli_task({'child':b'child\0bytes'},parent=parent)
        (parent_root/'independent-parent').write_bytes(b'parent only')
        old_parent=self.completed('task','ready',self.project,parent)['checkpoint_id']
        root_inode=self.root.stat().st_ino;parent_inode=parent_root.stat().st_ino
        child_plan=self.plan(child,child_result);child_receipt=self.apply(child_plan)
        self.assertEqual(parent_root.stat().st_ino,parent_inode)
        self.assertEqual(self.result('task','show',self.project,parent)['state'],'active')
        self.assertEqual(self.result('task','show',self.project,parent)['results'],[old_parent])
        parent_result=self.completed('task','ready',self.project,parent)['checkpoint_id']
        (self.root/'independent-source').write_bytes(b'source only')
        root_plan=self.plan(parent,parent_result);receipt=self.apply(root_plan)
        self.assertEqual(self.root.stat().st_ino,root_inode)
        for name,data in dict(base=b'base\r\nno-final-newline',child=b'child\0bytes',**{'independent-parent':b'parent only','independent-source':b'source only'},**protected).items():
            self.assertEqual((self.root/name).read_bytes(),data)
        self.assertEqual(receipt['checks'],'not_run_by_solace')
        received=self.result('integration','receipt',self.project,receipt['receipt_id'],'--limit','1')
        self.assertEqual(received['incoming']['checkpoint_id'],parent_result)
        checkpoints={c['id']:c for c in self.result('checkpoint','list',self.project,'--all-workspaces')['checkpoints']}
        self.assertEqual(checkpoints[receipt['after_id']]['parent_id'],receipt['before_id'])
        self.assertEqual(checkpoints[receipt['before_id']]['parent_id'],source)
        self.assertEqual(checkpoints[child_receipt['after_id']]['parent_id'],child_receipt['before_id'])
        with self.service.catalog(self.project).connect() as db:
            for table in ('operations','events','task_requests','integration_approvals','integration_receipts','integration_revisions'):
                self.assertNotIn('INTEGRATION PRIVATE CANARY',str([tuple(r) for r in db.execute('SELECT * FROM '+table)]))
        self.assertFalse((self.base/'home/.local/share/solace').exists())
        self.assertTrue(self.service.catalog(self.project).store.is_relative_to(self.base))
        self.assertTrue(self.registry.path.is_relative_to(self.base))
        self.assertNotIn('INTEGRATION PRIVATE CANARY',self.logs.getvalue())

    def test_binary_import_export_delete_and_byte_metadata_receipt_pages(self):
        (self.root/'binary').write_bytes(b'\0base')
        (self.root/'delete').write_bytes(b'\0base delete')
        payload=bytes(range(256))*500
        task,result,_=self.ready_cli_task({'binary':payload,'delete':b'delete me','other':b'other'})
        (self.root/'binary').write_bytes(b'\0current');(self.root/'delete').write_bytes(b'\0current delete');plan=self.plan(task,result)
        self.assertEqual(plan['state'],'conflicted')
        denied=self.failed('integration','apply',self.project,plan['plan_id'],'--revision','1','--approval','f'*64,'--writers-paused','--wait')
        self.assertFalse(denied['ok'])
        output=self.run_cli('integration','show',self.project,plan['plan_id']).stdout.decode();self.assertIn('conflicted',output)
        data=bytearray();offset=0
        while True:
            page=self.result('integration','diff',self.project,plan['plan_id'],'--revision','1','--path','binary','--version','incoming','--offset',str(offset))
            block=base64.b64decode(page['data']);self.assertLessEqual(len(block),48*1024);data.extend(block)
            if page['next_offset'] is None:break
            self.assertGreater(page['next_offset'],offset);offset=page['next_offset']
        self.assertEqual(bytes(data),payload)
        imported=self.base/'edited';imported.write_bytes(b'\0resolved\xff')
        plan=self.result('integration','resolve',self.project,plan['plan_id'],'--revision','1','--path','binary','--from-file',str(imported))
        plan=self.result('integration','resolve',self.project,plan['plan_id'],'--revision',str(plan['revision']),'--path','delete','--take','delete')
        export=self.base/'review-copy';self.completed('integration','export',self.project,plan['plan_id'],'--revision',str(plan['revision']),'--to',str(export))
        self.assertEqual((export/'candidate/binary').read_bytes(),imported.read_bytes())
        seen=[];after=0
        while True:
            page=self.result('integration','diff',self.project,plan['plan_id'],'--revision',str(plan['revision']),'--after',str(after),'--limit','1')
            seen += [row['path'] for row in page['entries']]
            if page['next_after'] is None:break
            after=page['next_after']
        self.assertEqual(seen,['binary','delete','other'])
        receipt=self.apply(plan);self.assertEqual((self.root/'binary').read_bytes(),imported.read_bytes());self.assertFalse((self.root/'delete').exists())
        seen=[];after=0
        while True:
            page=self.result('integration','receipt',self.project,receipt['receipt_id'],'--after',str(after),'--limit','1')
            seen += [row['path'] for row in page['entries']]
            if page['next_after'] is None:break
            after=page['next_after']
        self.assertEqual(seen,['binary','delete','other'])

    def test_r1_r2_contributes_incrementally_and_uuid_result_replay(self):
        task,r1,workspace=self.ready_cli_task({'first':b'R1'})
        revision=str(self.result('task','show',self.project,task)['revision']);request=str(uuid.uuid4())
        first_args=('integration','prepare',self.project,task,'--result',r1,'--expected-revision',revision,'--request-id',request)
        first=self.result(*first_args);self.assertEqual(self.result(*first_args),first)
        done=self.result('operation','show',self.project,first['operation_id'])
        if done['state']!='succeeded':done=self.wait_operation(first['operation_id'])
        plan=self.result('integration','show',self.project,done['result']['plan_id'])
        approval_request=str(uuid.uuid4());approval=self.approval(plan,'--request-id',approval_request)
        apply_request=str(uuid.uuid4());receipt=self.apply(plan,approval,'--request-id',apply_request)
        self.assertEqual(self.approval(plan,'--request-id',approval_request),approval)
        self.assertEqual(self.apply(plan,approval,'--request-id',apply_request),receipt)
        replay=self.apply(plan,approval);self.assertTrue(replay['already_applied']);self.assertEqual(replay['receipt_id'],receipt['receipt_id'])
        self.result('task','resume',self.project,task);(workspace/'second').write_bytes(b'R2')
        r2=self.completed('task','ready',self.project,task)['checkpoint_id']
        self.assertEqual(self.result(*first_args),first)
        (self.root/'first').write_bytes(b'current keeps changed R1')
        plan2=self.plan(task,r2);receipt2=self.apply(plan2)
        self.assertEqual((self.root/'first').read_bytes(),b'current keeps changed R1');self.assertEqual((self.root/'second').read_bytes(),b'R2')
        shown=self.result('integration','receipt',self.project,receipt2['receipt_id'])
        self.assertEqual(shown['base']['checkpoint_id'],r1);self.assertEqual(shown['previous_receipt_id'],receipt['receipt_id'])
        self.assertEqual(self.result('task','show',self.project,task)['results'],[r1,r2])

    def test_stale_parent_plan_after_second_child_and_abandon(self):
        parent=self.result('task','create',self.project,'parent')['task_id'];self.completed('task','start',self.project,parent)
        a,ar,_=self.ready_cli_task({'a':b'a'},parent=parent);b,br,_=self.ready_cli_task({'b':b'b'},parent=parent)
        pa=self.plan(a,ar);pb=self.plan(b,br);approval=self.approval(pb)
        self.apply(pa)
        old_parent=self.completed('task','ready',self.project,parent)['checkpoint_id']
        pending=self.failed('integration','prepare',self.project,parent,'--result',old_parent,'--wait')
        self.assertFalse(pending['ok'])
        self.assertEqual(pending['error']['code'],'TASK_CHILDREN_PENDING')
        failed=self.failed('integration','apply',self.project,pb['plan_id'],'--revision',str(pb['revision']),'--approval',approval,'--writers-paused','--wait')
        # Admission or the durable worker may detect the stale parent revision.
        self.assertIn('STALE',json.dumps(failed))
        self.result('integration','abandon',self.project,pb['plan_id'],'--revision',str(pb['revision']))
        fresh=self.plan(b,br);self.apply(fresh)
        parent_state=self.result('task','show',self.project,parent)
        self.assertEqual(parent_state['state'],'active');self.assertEqual(parent_state['results'],[old_parent])

    def test_interrupted_apply_explicit_recovery_and_replay(self):
        from solace_core.integration_publish import IntegrationPublisher
        for mode in ('resume','restore-before'):
            with self.subTest(mode=mode):
                task,result,_=self.ready_cli_task({'file':('incoming-'+mode).encode()})
                plan=self.plan(task,result);approval=self.approval(plan);original=IntegrationPublisher.publish
                before=(self.root/'file').read_bytes() if (self.root/'file').exists() else None
                def interrupted(publisher,journal):
                    def failpoint(name):
                        if name.startswith(('old_moved:','new_published:')):raise OSError('disposable interruption')
                    publisher.failpoint=failpoint;return original(publisher,journal)
                with patch.object(IntegrationPublisher,'publish',interrupted):
                    failed=self.failed('integration','apply',self.project,plan['plan_id'],'--revision',str(plan['revision']),'--approval',approval,'--writers-paused','--wait')
                self.assertTrue(failed['ok']);self.assertEqual(failed['result']['state'],'interrupted')
                operation=failed['result']['id'];history=self.result('operation','show',self.project,operation)
                page=self.result('integration','recovery','show',self.project,operation,'--limit','1')
                self.assertEqual(page['state'],'recovery_required');self.assertLessEqual(len(page['entries']),1)
                entries=list(page['entries'])
                while page['next_after'] is not None:
                    page=self.result('integration','recovery','show',self.project,operation,'--after',str(page['next_after']),'--limit','1')
                    entries.extend(page['entries'])
                self.assertEqual(len(entries),page['total'])
                request=str(uuid.uuid4());args=('integration','recovery',mode,self.project,operation,'--writers-paused','--request-id',request)
                recovered=self.completed(*args);self.assertEqual(self.completed(*args),recovered)
                self.assertEqual(self.result('operation','show',self.project,operation),history)
                if mode=='resume':self.assertEqual((self.root/'file').read_bytes(),('incoming-'+mode).encode())
                elif before is not None:self.assertEqual((self.root/'file').read_bytes(),before)
                else:self.assertFalse((self.root/'file').exists())

    def test_missing_capability_is_one_json_upgrade_error(self):
        original=self.service._dispatch
        def old(request):
            result=original(request)
            if request['method']=='core.status':result.pop('capabilities',None)
            return result
        with patch.object(self.service,'_dispatch',side_effect=old):
            denied=self.failed('integration','show',self.project,'a'*32)
        self.assertEqual(denied['error']['code'],'CORE_UPGRADE_REQUIRED')
