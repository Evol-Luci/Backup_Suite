import hashlib
from pathlib import Path
from unittest.mock import patch
from task_fixture import CatalogCase
from solace_engine.repository import ProjectRepository

class ComparisonTests(CatalogCase):
    def test_protected_capture_and_no_object_writes_in_status(self):
        from solace_core.workspace_paths import protected_excluder
        from solace_core.workspace_trees import compare_tree
        root=self.project
        (root/'nested').mkdir();(root/'nested/.git').write_text('pointer')
        (root/'.git').mkdir();(root/'.git/config').write_text('source Git')
        (root/'.solace_vault').mkdir();(root/'.solace_vault/keep').write_text('vault')
        (root/'.env').write_text('hidden');(root/'run').write_bytes(b'abc');(root/'run').chmod(0o755)
        (root/'big').write_bytes(b'\0'*1100000);(root/'link').symlink_to('run')
        profile={'version':1,'protected':['.git','.solace_vault','solace_owned_roots'],'exclusions':[]}
        repo=self.catalog.open_existing()
        cp=repo.create_checkpoint('base',parent_id=None,excluded=protected_excluder(root,profile,[self.store]))
        self.assertEqual({e.path for e in repo.get_file_entries(cp.id)},{'.env','run','big','link'})
        (root/'run').chmod(0o644)
        before={p.relative_to(self.store):p.read_bytes() for p in (self.store/'objects').rglob('*') if p.is_file()}
        with patch.object(repo.object_store,'put_bytes',side_effect=AssertionError('write')),patch.object(repo.object_store,'put_file',side_effect=AssertionError('write')):
            report=compare_tree(repo,cp.id,root,profile)
        self.assertEqual(report['changed'],[{'path':'run','reasons':['mode']}])
        self.assertFalse(report['clean'])
        self.assertEqual(before,{p.relative_to(self.store):p.read_bytes() for p in (self.store/'objects').rglob('*') if p.is_file()})
        (root/'link').unlink();(root/'link').symlink_to('big')
        (root/'big').unlink();(root/'new').write_text('added')
        report=compare_tree(repo,cp.id,root,profile)
        self.assertEqual(report['added'],['new']);self.assertEqual(report['removed'],['big'])
        self.assertIn({'path':'link','reasons':['link_target']},report['changed'])
