"""Explicit heads must not inherit the globally most recent sibling."""
import unittest
from unittest.mock import patch
import test_repository
from solace_engine.errors import CheckpointNotFound

class LineageTests(unittest.TestCase):
    setUp = test_repository.RepositoryTests.setUp

    def test_explicit_parent_root_and_legacy_default(self):
        a=self.repo.create_checkpoint('source', parent_id=None)
        b=self.repo.create_checkpoint('child', parent_id=a.id)
        c=self.repo.create_checkpoint('sibling', parent_id=a.id)
        self.assertEqual((b.parent_id,c.parent_id),(a.id,a.id))
        self.assertIsNone(self.repo.create_checkpoint('root',parent_id=None).parent_id)
        self.assertEqual(self.repo.create_checkpoint('legacy').parent_id,self.repo.list_checkpoints()[-2].id)

    def test_missing_parent_refused_before_scan(self):
        with patch.object(self.repo,'_scan',side_effect=AssertionError('must not scan')):
            with self.assertRaises(CheckpointNotFound):
                self.repo.create_checkpoint('wrong',parent_id='0'*32)

    def test_protected_predicate_skips_nested_git(self):
        (self.project/'nested').mkdir();(self.project/'nested/.git').write_text('pointer')
        (self.project/'.env').write_text('keep')
        cp=self.repo.create_checkpoint('task',excluded=lambda p,m: '.git' in p.split('/'))
        self.assertEqual([e.path for e in self.repo.get_file_entries(cp.id)],['.env'])
