import unittest
from solace_core.cli import _parser
from solace_core.policy_cli import prepare
from solace_core.errors import CoreError
P='a'*32
class Client:
    def request(self,*args):return dict(ok=True,result=dict(capabilities=['policy.v1']))
class PolicyCliTests(unittest.TestCase):
    def test_project_exact_fields(self):
        args=_parser().parse_args(['project','policy','preview',P,'--mode','checked_queue','--required-profile',P,'--no-allow-task-opt-in'])
        self.assertEqual(prepare(Client(),args,P),('project.policy.preview',dict(project=P,mode='checked_queue',required_profile=P,allow_task_opt_in=False)))
    def test_task_requires_revision_and_apply_confirmation(self):
        for argv in (['task','policy','preview',P,P,'--mode','manual'],['project','policy','apply',P,'--mode','manual','--no-allow-task-opt-in','--plan-token','x']):
            with self.assertRaises(CoreError):_parser().parse_args(argv)
    def test_queue_paging(self):
        args=_parser().parse_args(['integration','queue','list',P,'--after','4','--limit','2'])
        self.assertEqual(prepare(Client(),args,P),('integration.queue.list',dict(project=P,after=4,limit=2)))
