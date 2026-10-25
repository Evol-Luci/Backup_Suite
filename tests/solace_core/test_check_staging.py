"""Private candidate copies and conservative retained ownership."""
import json
import os
from pathlib import Path
from integration_fixture import IntegrationCase
from solace_core.check_staging import CheckStaging
from solace_core.check_profiles import CheckProfiles
from test_check_profiles import definition
from solace_core.errors import CoreError

class StagingTests(IntegrationCase):
    def prepared(self):
        (self.root/'script').write_bytes(b'base'); (self.root/'script').chmod(0o751)
        (self.root/'link').symlink_to('script')
        task,result,workspace=self.ready_task({'script':b'candidate'})
        plan=self.prepare_direct(task,result)
        profiles=CheckProfiles(self.catalog)
        params=dict(action='create',definition=definition())
        preview=profiles.preview(self.request('project.check-profile.preview',**params))
        profile=profiles.apply(self.request('project.check-profile.apply',**params,confirmed=True,plan_token=preview['plan_token']))
        self.staging=CheckStaging(self.service,self.catalog)
        self.run='run-one'
        with self.catalog.connect(write=True) as db:
            rev=db.execute('SELECT * FROM integration_revisions WHERE plan_id=?',(plan.id,)).fetchone()
            db.execute('INSERT INTO check_runs(id,plan_id,revision,task_id,candidate_id,manifest_sha256,capture_profile_json,expected_json,profile_id,profile_revision,profile_digest,state,staging_path,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.run,plan.id,plan.revision,task,plan.candidate.checkpoint_id,plan.candidate.manifest_sha256,rev['profile_json'],rev['expected_json'],profile['profile_id'],1,profile['digest'],'reserved',str(self.catalog.store/'check-runs'/self.run),'now','now'))
        return plan,workspace

    def test_stage_matches_m_without_touching_source_or_workspace(self):
        plan,workspace=self.prepared(); staged=self.staging.stage(self.run,plan)
        self.staging.verify(staged,plan)
        self.assertEqual((staged.root/'script').read_bytes(),b'candidate')
        self.assertEqual((staged.root/'script').stat().st_mode & 0o777,0o751)
        self.assertEqual(os.readlink(staged.root/'link'),'script')
        self.assertEqual((self.root/'script').read_bytes(),b'base')
        self.assertEqual((workspace/'script').read_bytes(),b'candidate')
        for other in (self.root,workspace):
            self.assertNotEqual((staged.root/'script').stat().st_ino,(other/'script').stat().st_ino)
        for private in (staged.home,staged.tmpdir):
            self.assertFalse(private.is_relative_to(staged.root))
            self.assertEqual(private.stat().st_mode & 0o777,0o700)
        claims=self.service.workspace_paths.active_claims()
        self.assertIn(str(staged.evidence),[claim['path'] for claim in claims])

    def test_corrupt_cas_or_substituted_ancestor_refuses_launch(self):
        plan,_=self.prepared(); staged=self.staging.stage(self.run,plan)
        (staged.root/'script').write_bytes(b'corrupt')
        with self.assertRaises(CoreError): self.staging.verify(staged,plan)
        (staged.root/'script').write_bytes(b'candidate')
        entry=next(e for e in self.plans.snapshots.entries(plan.candidate) if e.path=='script')
        object_path=self.plans.snapshots.objects._object_path(entry.chunk_digests[0])
        saved=object_path.read_bytes(); object_path.write_bytes(b'corrupt CAS')
        with self.assertRaises(CoreError): self.staging.verify(staged,plan)
        object_path.write_bytes(saved)
        parent=staged.root.parent.parent; moved=parent.with_name('moved-runs')
        parent.rename(moved); parent.symlink_to(moved)
        with self.assertRaises(CoreError): self.staging.verify(staged,plan)

    def test_stage_identity_change_blocks_cleanup(self):
        plan,_=self.prepared(); staged=self.staging.stage(self.run,plan)
        container=staged.root.parent; moved=container.with_name('moved'); container.rename(moved)
        container.symlink_to(moved)
        with self.catalog.connect(write=True) as db: db.execute("UPDATE check_runs SET state='interrupted' WHERE id=?",(self.run,))
        self.assertEqual(self.staging.cleanup(self.run)['cleanup_state'],'unsafe')
        self.assertTrue((moved/'candidate'/'script').exists())

    def test_command_artifact_is_not_candidate(self):
        plan,_=self.prepared(); staged=self.staging.stage(self.run,plan)
        (staged.root/'artifact').write_text('generated')
        self.assertNotIn('artifact',self.version_bytes(plan.candidate))
        (staged.evidence/'stdout').write_text('retained')
        with self.catalog.connect(write=True) as db: db.execute("UPDATE check_runs SET state='passed' WHERE id=?",(self.run,))
        self.assertEqual(self.staging.cleanup(self.run)['cleanup_state'],'cleaned')
        self.assertFalse(staged.root.exists())
        self.assertEqual((staged.evidence/'stdout').read_text(),'retained')

    def test_cleanup_refuses_active_run_and_changed_candidate_identity(self):
        plan,_=self.prepared(); staged=self.staging.stage(self.run,plan)
        with self.assertRaises(CoreError): self.staging.cleanup(self.run)
        staged.root.rename(staged.root.with_name('original'))
        staged.root.mkdir(); (staged.root/'keep').write_text('replacement')
        with self.catalog.connect(write=True) as db: db.execute("UPDATE check_runs SET state='interrupted' WHERE id=?",(self.run,))
        self.assertEqual(self.staging.cleanup(self.run)['cleanup_state'],'unsafe')
        self.assertEqual((staged.root/'keep').read_text(),'replacement')
