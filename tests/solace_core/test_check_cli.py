import unittest
from solace_core.cli import _parser
from solace_core.errors import CoreError
from solace_core.check_cli import prepare
P='a'*32
class Client:
    def request(self, method, params): return {'ok':True,'result':{'capabilities':['checks.v1'],'revision':2}}
class CheckCliTests(unittest.TestCase):
    def test_cli_preview_confirmation_and_run_wait(self):
        args=_parser().parse_args(['integration','check','run',P,P,'--revision','1','--profile',P,'--wait'])
        method,params=prepare(Client(),args,P)
        self.assertEqual(method,'integration.check.run')
        self.assertEqual(params,dict(project=P,plan=P,revision=1,profile=P))
        self.assertTrue(args.wait)
    def test_cli_request_id_requires_pinned_mutations(self):
        args=_parser().parse_args(['task','check-profile','select',P,P,P])
        with self.assertRaises(CoreError): prepare(Client(),args,P,'retry')
    def test_human_output_distinguishes_failed_stale_passed_and_unchecked(self):
        from solace_core.check_cli import status_message
        self.assertIn('failed',status_message('failed').lower())
        self.assertIn('stale',status_message('stale').lower())
        self.assertIn('passed',status_message('passed').lower())
        self.assertIn('unchecked',status_message('not_run_by_solace').lower())
