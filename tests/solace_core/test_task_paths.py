import json
from pathlib import Path
from unittest.mock import patch
from task_fixture import TaskCase
from solace_core.errors import CoreError

class PathTests(TaskCase):
    def test_placement_rejects_sources_stores_git_sync_and_overlap(self):
        from solace_core.workspace_paths import WorkspacePaths,CoreConfig
        paths=WorkspacePaths(self.registry,self.config,self.policy)
        store=Path(self.registry.get(self.project)['store'])
        git=self.base/'git';git.mkdir();(git/'.git').write_text('gitdir: anywhere')
        for root in [self.root,self.root/'nested',store,store/'workspaces',git/'nested',self.base]:
            with self.subTest(root=root),self.assertRaises(CoreError):
                paths.validate(root,creating=True)
        other=self.base/'safe';other.mkdir()
        before=other.stat();identity=paths.validate(other)
        other.rename(self.base/'old-safe');other.mkdir()
        with self.assertRaises(CoreError) as caught: paths.validate(other,expected=identity)
        self.assertEqual(caught.exception.code,'WORKSPACE_IDENTITY_MISMATCH')

    def test_missing_workspace_does_not_create_and_ancestor_swap_detected(self):
        from solace_core.workspace_paths import WorkspacePaths
        paths=WorkspacePaths(self.registry,self.config,self.policy)
        missing=self.base/'missing'
        with self.assertRaises(CoreError): paths.validate(missing)
        self.assertFalse(missing.exists())
        parent=self.base/'holding';parent.mkdir();leaf=parent/'leaf';leaf.mkdir()
        expected=paths.validate(leaf)
        with self.assertRaises(CoreError):
            with paths.open_checked(leaf,expected):
                parent.rename(self.base/'moved');parent.mkdir();(parent/'sentinel').write_text('keep')
        self.assertEqual((parent/'sentinel').read_text(),'keep')

    def test_shared_configuration_and_new_registration_guard(self):
        from solace_core.workspace_paths import CoreConfig
        data=self.base/'configdata';(data/'solace').mkdir(parents=True)
        config=data/'solace/core-config.json'
        config.write_text(json.dumps({'sync_roots':[],'workspace_root':str(self.base/'custom')}))
        self.assertEqual(CoreConfig.load(data).workspace_root,self.base/'custom')
        for value in [{'workspace_root':'relative'},{'other':1},{'workspace_root':None}]:
            config.write_text(json.dumps(value))
            with self.assertRaises(CoreError): CoreConfig.load(data)
        root=self.config.workspace_root/'new-project';root.mkdir(parents=True)
        response=self.service.handle(self.request('project.add',root=str(root)))
        self.assertFalse(response['ok'],response)
        self.assertEqual(response['error']['code'],'UNSUPPORTED_STORAGE')
