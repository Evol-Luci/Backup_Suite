from datetime import datetime,timedelta,timezone
from task_fixture import CatalogCase
from solace_core.errors import CoreError

class PromptTests(CatalogCase):
    def create(self,**params):
        return self.tasks.create(self.request('task.create',title='work',**params))['task_id']

    def test_exact_empty_missing_none_and_boundaries(self):
        from solace_core.prompts import PromptStore
        prompts=PromptStore(self.catalog)
        for supplied,status in [({},'missing'),({'prompt':''},'available'),({'prompt':'é\r\nexact'},'available'),({'prompt':'secret','prompt_policy':'none'},'discarded')]:
            with self.subTest(supplied=supplied):
                task=self.create(**supplied);read=prompts.read(task)
                self.assertEqual(read['status'],status)
                if status=='available': self.assertEqual(read['body'],supplied['prompt'])
                else: self.assertNotIn('body',read)
        self.create(prompt='é'*65536)
        for text in ['a'*131073,'é'*65537,'binary\0','\ud800']:
            with self.subTest(size=len(text)),self.assertRaises(CoreError) as caught: self.create(prompt=text)
            self.assertEqual(caught.exception.code,'PROMPT_INVALID')

    def test_policy_frozen_overrides_and_immediate_expiry(self):
        from solace_core.tasks import TaskCatalog
        from solace_core.prompts import PromptStore
        now=datetime(2026,1,1,tzinfo=timezone.utc);clock=lambda:now
        self.tasks=TaskCatalog(self.catalog,now=clock);prompts=PromptStore(self.catalog,now=clock)
        task=self.create(prompt='thirty days')
        self.assertEqual(prompts.read(task)['expires_at'],'2026-01-31T00:00:00+00:00')
        prompts.set_policy(self.request('project.prompt-policy.set',policy='retain',expected_revision=1))
        retained=self.create(prompt='retained')
        self.assertIsNone(prompts.read(retained)['expires_at'])
        override=self.create(prompt='one day',prompt_policy='days',prompt_days=1)
        now+=timedelta(days=1)
        self.assertEqual(prompts.read(override)['status'],'expired');self.assertNotIn('body',prompts.read(override))
        now+=timedelta(days=29)
        self.assertEqual(prompts.read(task)['status'],'expired')
        self.assertEqual(prompts.read(retained)['body'],'retained')
        self.assertEqual(prompts.purge_expired(),2)
        self.assertEqual(prompts.purge_expired(),0)

    def test_policy_revision_and_delete_retry_do_not_retain_body_in_receipt(self):
        from solace_core.prompts import PromptStore
        prompts=PromptStore(self.catalog);task=self.create(prompt='erase')
        req=self.request('task.prompt.delete',task=task,expected_revision=1)
        first=prompts.delete(req);self.assertEqual(first['revision'],2)
        self.assertEqual(prompts.delete(req),first)
        with self.assertRaises(CoreError) as caught:
            prompts.set_policy(self.request('project.prompt-policy.set',policy='none',expected_revision=2))
        self.assertEqual(caught.exception.code,'TASK_REVISION_CONFLICT')
        for policy,days in [('days',0),('days',True),('days',None),('retain',1)]:
            with self.subTest(policy=policy,days=days),self.assertRaises(CoreError):
                prompts.set_policy(self.request('project.prompt-policy.set',policy=policy,days=days,expected_revision=1))
