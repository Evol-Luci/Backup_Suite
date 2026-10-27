"""Pinned command construction; the socket workflow lives in acceptance tests."""
import unittest
import shlex
from contextlib import redirect_stdout
from io import StringIO
from solace_core.cli import _parser
from solace_core.errors import CoreError


ID = 'a'*32
TOKEN = 'b'*64


class Client:
    def __init__(self, capabilities=('integrations.v1',), task=None):
        self.calls = []
        self.capabilities = capabilities
        self.task = task or dict(revision=7, parent_id=None)

    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'core.status': result = dict(capabilities=self.capabilities)
        elif method == 'task.show':
            result = self.task if params['task'] == ID else dict(revision=9)
        else: raise AssertionError('Unexpected hidden read: '+method)
        return dict(ok=True, result=result)


class IntegrationCLITests(unittest.TestCase):
    def build(self, *args, client=None, request_id=None):
        from solace_core.integration_cli import prepare
        return prepare(client or Client(), _parser().parse_args(['integration', *args]), ID, request_id)

    def test_all_api_rows_and_exact_fields(self):
        cases = [
            (['prepare', ID, ID, '--result', ID, '--expected-revision', '7', '--expected-target-revision', '9'], 'prepare', dict(task=ID,result=ID,expected_revision=7,expected_target_revision=9)),
            (['show', ID, ID], 'show', dict(plan=ID)),
            (['diff', ID, ID, '--revision','2','--after','1','--limit','5'], 'diff', dict(plan=ID,revision=2,after=1,limit=5)),
            (['diff', ID, ID, '--revision','2','--path',r'literal\name','--version','incoming','--offset','49152'], 'diff', dict(plan=ID,revision=2,path=r'literal\name',version='incoming',offset=49152)),
            (['resolve',ID,ID,'--revision','2','--path','x','--take','delete'], 'resolve', dict(plan=ID,revision=2,path='x',take='delete')),
            (['resolve',ID,ID,'--revision','2','--path','x','--from-file','/tmp/edited'], 'resolve', dict(plan=ID,revision=2,path='x',from_file='/tmp/edited')),
            (['export',ID,ID,'--revision','2','--to','/tmp/export','--wait'], 'export', dict(plan=ID,revision=2,destination='/tmp/export')),
            (['approve',ID,ID,'--revision','2','--token',TOKEN,'--accept-unchecked'], 'approve', dict(plan=ID,revision=2,token=TOKEN,accept_unchecked=True)),
            (['apply',ID,ID,'--revision','2','--approval',TOKEN,'--writers-paused','--wait'], 'apply', dict(plan=ID,revision=2,approval=TOKEN,writers_paused=True)),
            (['receipt',ID,ID,'--after','1','--limit','5'], 'receipt', dict(receipt=ID,after=1,limit=5)),
            (['abandon',ID,ID,'--revision','2'], 'abandon', dict(plan=ID,revision=2)),
            (['recovery','show',ID,ID,'--after','1','--limit','5'], 'recovery.show', dict(operation=ID,after=1,limit=5)),
            (['recovery','resume',ID,ID,'--writers-paused','--wait'], 'recovery.resume', dict(operation=ID,writers_paused=True)),
            (['recovery','restore-before',ID,ID,'--writers-paused','--target-path','/tmp/t','--journal-path','/tmp/j','--confirm-target','/tmp/t','--confirm-journal','/tmp/j'], 'recovery.restore-before', dict(operation=ID,writers_paused=True,target_path='/tmp/t',journal_path='/tmp/j',confirmed_target='/tmp/t',confirmed_journal='/tmp/j')),
        ]
        from solace_core.integration_protocol import validate_integration
        for args, suffix, fields in cases:
            with self.subTest(command=suffix):
                method, params = self.build(*args, request_id='explicit')
                self.assertEqual((method,params), ('integration.'+suffix,dict(project=ID,**fields)))
                validate_integration(method,params)

    def test_convenience_preparation_reads_only_exact_task_and_parent_revisions(self):
        client = Client(task=dict(revision=7,parent_id='c'*32))
        _, params = self.build('prepare',ID,ID,'--result',ID,client=client)
        self.assertEqual(params,dict(project=ID,task=ID,result=ID,expected_revision=7,expected_target_revision=9))
        self.assertEqual([p['task'] for m,p in client.calls if m=='task.show'],[ID,'c'*32])

    def test_explicit_request_never_reads_or_replaces_revisions(self):
        client = Client()
        with self.assertRaises(CoreError) as caught:
            self.build('prepare',ID,ID,'--result',ID,client=client,request_id='explicit')
        self.assertIn('--expected-revision',str(caught.exception))
        _, params = self.build('prepare',ID,ID,'--result',ID,'--expected-revision','3',client=client,request_id='explicit')
        self.assertEqual(params['expected_revision'],3)
        self.assertTrue(all(m=='core.status' for m,_ in client.calls))

    def test_missing_capability_explains_core_upgrade(self):
        with self.assertRaises(CoreError) as caught:
            self.build('show',ID,ID,client=Client(capabilities=[]))
        self.assertEqual(caught.exception.code,'CORE_UPGRADE_REQUIRED')
        self.assertIn('core',caught.exception.next_step)

    def test_missing_pins_acknowledgements_and_invalid_modes_are_actionable(self):
        cases = [
            ['approve',ID,ID,'--revision','1','--token',TOKEN],
            ['apply',ID,ID,'--revision','1','--approval',TOKEN],
            ['export',ID,ID,'--to','/tmp/export'], ['abandon',ID,ID],
            ['resolve',ID,ID,'--revision','1','--path','x','--take','base'],
            ['recovery','resume',ID,ID],
            ['recovery','resume',ID,ID,'--writers-paused','--target-path','/tmp/t'],
            ['recovery','resume',ID,ID,'--writers-paused','--target-path','/tmp/t','--journal-path','/tmp/j','--confirm-target','/tmp/wrong','--confirm-journal','/tmp/j'],
            ['diff',ID,ID,'--after','1'],
            ['diff',ID,ID,'--path','x','--version','incoming'],
            ['diff',ID,ID,'--revision','1','--path','x','--version','incoming','--after','0'],
            ['apply',ID,ID,'--revision','1','--approval',TOKEN,'--writers-paused','--to','/tmp/other'],
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(CoreError): self.build(*args)

    def test_human_guidance_preserves_receipt_page_cursor_and_reports_interruption(self):
        from solace_core.integration_cli import emit
        output=StringIO()
        with redirect_stdout(output):
            emit(dict(ok=True,result=dict(receipt_id=ID,operation_id='c'*32,next_after=1)),
                 'integration.receipt',dict(project=ID,receipt=ID,limit=1))
        self.assertIn('--after 1 --limit 1 --json',output.getvalue())
        self.assertIn('Solace did not run checks',output.getvalue())
        output=StringIO()
        with redirect_stdout(output):
            emit(dict(ok=True,result=dict(id='c'*32,state='interrupted',error=dict(code='INTEGRATION_RECOVERY_REQUIRED',message='Interrupted publication.',next_step='Inspect retained recovery.'))),
                 'integration.apply',dict(project=ID,plan=ID))
        self.assertIn('INTEGRATION_RECOVERY_REQUIRED',output.getvalue())
        self.assertIn('solace integration recovery show '+ID+' '+'c'*32,output.getvalue())

    def test_human_content_continuation_round_trips_leading_option_filename(self):
        from solace_core.integration_cli import emit
        path = '--literal'
        params = dict(project=ID, plan=ID, revision=2, path=path,
                      version='incoming', offset=0)
        output = StringIO()
        with redirect_stdout(output):
            emit(dict(ok=True, result=dict(revision=2, next_offset=49152)),
                 'integration.diff', params)
        line = next(line for line in output.getvalue().splitlines()
                    if line.startswith('Next command: '))
        command = shlex.split(line.removeprefix('Next command: '))
        self.assertEqual(command[:2], ['solace', 'integration'])
        self.assertEqual(command[-1], '--json')
        method, actual = self.build(*command[2:-1])
        self.assertEqual((method, actual), ('integration.diff', dict(params, offset=49152)))


if __name__=='__main__': unittest.main()
