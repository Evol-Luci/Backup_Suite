"""Real abrupt process exits with exact durable barriers and non-writing restart.

These tests model process death on the local test filesystem, not power loss.
Unknown created inodes intentionally remain conflicts with retained evidence.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from integration_fixture import IntegrationCase
import test_integration_publish as publishing
from solace_core.integration_claims import IntegrationClaims
from solace_core.integration_review import IntegrationReview
from solace_core.service import CoreService


# Exact ordered actions of the mixed add/delete/file<->directory fixture.
ACTIONS = (
    ('move_old', 'c-node/leaf'), ('move_old', 'a-delete'),
    ('move_old', 'b-node'), ('rmdir', 'c-node'), ('mkdir', 'b-node'),
    ('publish_new', 'b-node/leaf'), ('publish_new', 'c-node'),
    ('publish_new', 'd-add'),
)


def snapshot_tree(path):
    """lstat/nofollow snapshot: never read a FIFO or follow a substituted link."""
    result = {}
    def visit(parent, name, relative):
        try:
            meta = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            result[relative] = None
            return
        data = None
        if stat.S_ISLNK(meta.st_mode):
            data = os.readlink(name, dir_fd=parent)
        elif stat.S_ISREG(meta.st_mode):
            fd = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=parent)
            try:
                assert os.path.samestat(meta, os.fstat(fd))
                chunks = []
                while block := os.read(fd, 65536): chunks.append(block)
                data = b''.join(chunks)
            finally: os.close(fd)
        result[relative] = (meta.st_dev, meta.st_ino, meta.st_mode, data)
        if stat.S_ISDIR(meta.st_mode):
            fd = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=parent)
            try:
                assert os.path.samestat(meta, os.fstat(fd))
                for child in sorted(os.listdir(fd)):
                    visit(fd, child, relative+'/'+child)
            finally: os.close(fd)
    # Fixture ancestors are private, stable parents; only recorded roots/slots
    # are traversed, using descriptor-relative nofollow operations below them.
    parent = os.open(path.parent, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: visit(parent, path.name, '.')
    finally: os.close(parent)
    return result


class CrashTests(IntegrationCase):
    plan_with = publishing.PublishTests.plan_with

    def fixture(self, *, simple=False):
        if simple:
            (self.root/'file').write_bytes(b'original')
            plan = self.plan_with(lambda root: (root/'file').write_bytes(b'incoming'))
        else:
            (self.root/'a-delete').write_bytes(b'deleted C')
            (self.root/'b-node').write_bytes(b'file C')
            (self.root/'c-node').mkdir()
            (self.root/'c-node/leaf').write_bytes(b'directory child C')
            (self.root/'keep').write_bytes(b'unchanged')
            (self.root/'.git').write_bytes(b'protected metadata')
            def edit(root):
                (root/'a-delete').unlink()
                (root/'b-node').unlink(); (root/'b-node').mkdir()
                (root/'b-node/leaf').write_bytes(b'directory child M')
                (root/'c-node/leaf').unlink(); (root/'c-node').rmdir()
                (root/'c-node').write_bytes(b'file M')
                (root/'d-add').write_bytes(b'added M')
            plan = self.plan_with(edit)
        self.plan = plan
        shown = IntegrationReview(self.service,self.catalog).show(plan.id)
        approved = IntegrationClaims(self.service,self.catalog).approve(self.request(
            'integration.approve',plan=plan.id,revision=plan.revision,
            token=shown['review_token'],accept_unchecked=True))
        self.apply_request = self.request('integration.apply',plan=plan.id,revision=plan.revision,
                                          approval=approved['approval'],writers_paused=True)
        self.expected_before = self.version_bytes(plan.current)
        self.expected_after = self.version_bytes(plan.candidate)
        self.initial_tree = snapshot_tree(self.root)
        self.initial_metadata = self.publication_metadata()
        self.fixture_path = self.base/'crash-fixture.json'
        self.fixture_data = dict(project=self.project, registry=str(self.registry.path),
            workspace_root=str(self.config.workspace_root), sync_roots=[],
            mountinfo='1 0 0:1 / / rw - ext4 x rw\n', requests={'apply':self.apply_request})
        return plan

    def publication_metadata(self):
        with self.catalog.connect() as db:
            value = {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('source_head','checkpoint_context','integration_receipts',
                                  'integration_refs','tasks','workspaces','task_checkpoint_refs')}
            value['applied_events']=[tuple(row) for row in db.execute("SELECT * FROM events WHERE kind='integration.applied'")]
            return value

    def snapshot_recorded_paths(self):
        paths = [self.root]
        with self.catalog.connect() as db:
            paths += [Path(r[0]) for r in db.execute('SELECT journal_path FROM integration_journals')]
        return {str(p):snapshot_tree(p) for p in paths}

    def crash_at(self, point, mode='apply'):
        self.service.close()
        barrier = self.base/('barrier-'+str(len(list(self.base.glob('barrier-*'))))+'.json')
        self.fixture_data['barrier'] = str(barrier)
        self.fixture_path.write_text(json.dumps(self.fixture_data));self.fixture_path.chmod(0o600)
        run = subprocess.run([sys.executable,str(Path(__file__).with_name('integration_crash_worker.py')),
                              str(self.fixture_path),point,mode],
                             cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=20)
        self.assertLessEqual(len(run.stdout),65536,run.stdout[:1000])
        self.assertEqual(run.returncode,91,run.stdout.decode(errors='replace'))
        self.assertTrue(barrier.is_file(),'Exit 91 without durable reached-point barrier')
        reached = json.loads(barrier.read_text())
        self.assertEqual((reached['point'],reached['mode']),(point,mode))
        self.assertNotEqual(reached['pid'],os.getpid())
        if mode=='apply' and point in ('final_sql_uncommitted','reply_lost'):
            self.assertEqual([p for p in reached['seen'] if p.startswith('final_sql_statement:')],
                             ['final_sql_statement:'+str(i) for i in range(1,15)],
                             'Update the per-statement crash matrix when publication SQL changes')
        # Optional evidence output belongs solely to this test harness. The
        # production service has no environment failpoint or recovery bypass.
        evidence_dir=os.environ.get('SOLACE_TEST_CRASH_EVIDENCE_DIR')
        if evidence_dir:
            destination=Path(evidence_dir)
            self.assertTrue(destination.is_absolute() and destination.is_dir())
            saved=destination/(mode+'-'+str(reached['pid'])+'-'+point.replace(':','_')+'.json')
            saved.write_bytes(barrier.read_bytes());saved.chmod(0o600)
        if 'rename_before_' in point:
            self.assertTrue(reached['evidence']['source_absent'])
            self.assertGreater(reached['evidence']['destination_inode'],0)
            self.assertEqual(reached['evidence']['sync_number'],2 if 'receiving' in point else 1)
        self.last_barrier = reached
        return run

    def restart_service(self):
        self.service.close()
        before = self.snapshot_recorded_paths()
        with self.catalog.connect() as db:
            claims=[tuple(row) for row in db.execute('SELECT * FROM integration_claims ORDER BY path')]
        self.service = CoreService(self.registry,config=self.config)
        self.addCleanup(self.service.close)
        self.catalog = self.service.catalog(self.project)
        self.assertEqual(self.snapshot_recorded_paths(),before,'Startup wrote target/journal evidence')
        with self.catalog.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM integration_claims ORDER BY path')],claims)
            rows = list(db.execute('SELECT operation_id,journal_path FROM integration_journals'))
        if rows:
            self.original_operation,self.journal_path = rows[0][0],Path(rows[0][1])
        return before

    def recovery_show(self):
        return self.call('integration.recovery.show',project=self.project,operation=self.original_operation)

    def assert_claims(self, count=2, *, source_available=True):
        with self.catalog.connect() as db:
            claims = [tuple(row) for row in db.execute('SELECT kind,path FROM integration_claims WHERE operation_id=? ORDER BY kind',(self.original_operation,))]
            task_claims = db.execute('SELECT COUNT(*) FROM task_claims WHERE operation_id=?',(self.original_operation,)).fetchone()[0]
        self.assertEqual(len(claims),count)
        self.assertEqual(task_claims,1 if count else 0)
        if count:
            self.assertEqual({kind for kind,_ in claims},{'target','journal'})
            denied = self.service.handle(self.request('checkpoint.create',message='blocked by interrupted publication'))
            # Availability validation precedes the ownership guard. Both refuse
            # mutation, while the durable rows above must survive either state.
            expected = 'INTEGRATION_TARGET_BUSY' if source_available else 'PROJECT_UNAVAILABLE'
            self.assertEqual(denied['error']['code'],expected,denied)

    def assert_versions_retained(self):
        # Immutable CAS retains all C/M even before staging; physical copies must
        # additionally survive every mutation once staging is complete.
        self.assertEqual(self.version_bytes(self.plan.current),self.expected_before)
        self.assertEqual(self.version_bytes(self.plan.candidate),self.expected_after)
        if 'journal_staged' in self.last_barrier['seen'] or self.last_barrier['mode'] != 'apply':
            contents = [row[3] for tree in self.snapshot_recorded_paths().values()
                        for row in tree.values() if row and stat.S_ISREG(row[2])]
            for data in set(self.expected_before.values())|set(self.expected_after.values()):
                self.assertIn(data,contents,'Lost a staged or displaced version')

    def recover(self, mode='resume', **mapping):
        request = self.request('integration.recovery.'+mode,operation=self.original_operation,
                               writers_paused=True,**mapping)
        response = self.service.handle(request)
        self.assertTrue(response['ok'],response)
        outcome = self.wait_operation(response['result']['operation_id'])
        return request,response['result'],outcome

    def assert_finished(self, outcome, mode='resume'):
        self.assertEqual(outcome['state'],'succeeded',outcome)
        expected = self.expected_after if mode == 'resume' else self.expected_before
        actual = {name.removeprefix('./'):row[3] for name,row in snapshot_tree(self.root).items()
                  if row and stat.S_ISREG(row[2]) and name != './.git'}
        self.assertEqual(actual,expected)
        self.assert_versions_retained()
        self.assertEqual(self.root.stat().st_ino,self.initial_tree['.'][1])
        if './.git' in self.initial_tree:
            self.assertEqual(snapshot_tree(self.root)['./.git'],self.initial_tree['./.git'])
            self.assertEqual(snapshot_tree(self.root)['./keep'],self.initial_tree['./keep'])
        self.assert_claims(0)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_receipts').fetchone()[0],1 if mode=='resume' else 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='integration.applied'").fetchone()[0],1 if mode=='resume' else 0)
            self.assertEqual(db.execute('SELECT checkpoint_id FROM source_head').fetchone()[0],
                             self.plan.candidate.checkpoint_id if mode=='resume' else self.initial_metadata['source_head'][0][1])

    def run_apply_boundary(self, point, *, conflict=False):
        self.fixture()
        self.crash_at(point)
        self.restart_service()
        if point == 'before_reservation':
            self.assertEqual(self.snapshot_recorded_paths(),{str(self.root):self.initial_tree})
            self.assertEqual(self.publication_metadata(),self.initial_metadata)
            with self.catalog.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],0)
            self.call('integration.abandon',project=self.project,plan=self.plan.id,revision=1)
            return
        if point == 'reply_lost':
            outcome = self.catalog.get_operation(self.original_operation)
            self.assert_finished(outcome)
            snapshot = self.snapshot_recorded_paths(); metadata = self.publication_metadata()
            self.assertEqual(self.service.handle(self.apply_request)['result'],dict(operation_id=self.original_operation))
            self.assertEqual(self.snapshot_recorded_paths(),snapshot)
            self.assertEqual(self.publication_metadata(),metadata)
            return
        self.assertEqual(self.publication_metadata(),self.initial_metadata,'Uncommitted SQL escaped transaction')
        self.assertEqual(self.recovery_show()['state'],'recovery_required')
        self.assert_claims();self.assert_versions_retained()
        original = self.catalog.get_operation(self.original_operation)
        replay_before=self.snapshot_recorded_paths()
        self.assertEqual(self.service.handle(self.apply_request)['result'],dict(operation_id=self.original_operation))
        self.assertEqual(self.snapshot_recorded_paths(),replay_before)
        if point not in ('journal_reserved','journal_created','stage_marker') and not point.startswith('stage_file_synced:'):
            self.assertEqual([(r['kind'],r['path']) for r in self.recovery_show()['entries']],list(ACTIONS))
        before = self.snapshot_recorded_paths()
        request,accepted,outcome = self.recover()
        if conflict:
            self.assertEqual(outcome['state'],'interrupted',outcome)
            self.assertIsNotNone(outcome['error'])
            self.assertEqual(self.snapshot_recorded_paths(),before)
            self.assert_claims();self.assert_versions_retained()
        else:
            self.assert_finished(outcome)
        self.assertEqual(self.catalog.get_operation(self.original_operation),original)
        settled = self.snapshot_recorded_paths()
        self.assertEqual(self.service.handle(request)['result'],accepted)
        self.assertEqual(self.snapshot_recorded_paths(),settled)

    def run_recovery_boundary(self, point, mode, *, mixed=False, conflict=False):
        self.fixture(simple=not mixed)
        self.crash_at('final_sql_before' if mixed else 'new_published:1' if mode=='restore-before' else 'old_moved:0')
        self.restart_service()
        original = self.catalog.get_operation(self.original_operation)
        request = self.request('integration.recovery.'+mode,operation=self.original_operation,writers_paused=True)
        self.fixture_data['requests'][mode] = request
        self.crash_at(point,mode)
        self.restart_service()
        self.assert_versions_retained()
        interrupted = self.catalog.get_operation(self.last_barrier['operation_id'])
        if point == 'reply_lost':
            self.assert_finished(interrupted,mode)
        else:
            self.assert_claims()
            self.assertEqual(interrupted['state'],'interrupted')
            before=self.snapshot_recorded_paths()
            _,_,outcome = self.recover(mode)
            if conflict:
                self.assertEqual(outcome['state'],'interrupted',outcome)
                self.assertEqual(self.snapshot_recorded_paths(),before)
                self.assert_claims();self.assert_versions_retained()
            else: self.assert_finished(outcome,mode)
        self.assertEqual(self.catalog.get_operation(self.original_operation),original)
        before = self.snapshot_recorded_paths()
        self.assertEqual(self.service.handle(request)['result'],dict(operation_id=interrupted['id']))
        self.assertEqual(self.catalog.get_operation(interrupted['id']),interrupted)
        self.assertEqual(self.snapshot_recorded_paths(),before)

    def unavailable_after_crash(self, component):
        self.fixture(simple=True);self.crash_at('old_moved:0');self.restart_service()
        target = {'source':self.root,'journal':self.journal_path,'store':self.catalog.store}[component]
        moved = self.base/('offline-'+component)
        self.service.close();target.rename(moved)
        preserved={str(p):snapshot_tree(p) for p in (self.root,self.journal_path)}
        if component=='store':
            with self.assertLogs('solace_core.maintenance',level='WARNING') as diagnostics:
                self.service=CoreService(self.registry,config=self.config)
            self.assertEqual([r.getMessage() for r in diagnostics.records],
                             ['prompt maintenance project='+self.project+' exception=CoreError'])
        else: self.service=CoreService(self.registry,config=self.config)
        self.addCleanup(self.service.close)
        self.assertFalse(target.exists(),'Startup recreated an unavailable recorded location')
        self.assertEqual({str(p):snapshot_tree(p) for p in (self.root,self.journal_path)},preserved)
        if component=='store':
            response=self.service.handle(self.request('integration.recovery.show',operation=self.original_operation))
            self.assertFalse(response['ok'],response)
            self.assertEqual(response['error']['code'],'STORE_UNAVAILABLE')
        else:
            self.catalog=self.service.catalog(self.project)
            self.assertEqual(self.recovery_show()['validation'],'deferred')
            self.assert_claims(source_available=component!='source')
        moved.rename(target)
        self.catalog=self.service.catalog(self.project)
        self.assertFalse((self.root/'file').exists())
        self.assert_versions_retained();self.assert_claims()
        _,_,outcome=self.recover()
        self.assert_finished(outcome)

    def run_handoff_boundary(self, point):
        self.fixture(simple=True);self.crash_at('old_moved:0');self.restart_service()
        old = self.registry.get(self.project)
        moved = self.base/'moved-source';store=self.base/'moved-store';journal=self.base/'moved-journal'
        self.service.close()
        self.root.rename(moved);Path(old['store']).rename(store);self.journal_path.rename(journal)
        with self.assertLogs('solace_core.maintenance',level='WARNING') as diagnostics:
            self.service=CoreService(self.registry,config=self.config)
        self.addCleanup(self.service.close)
        self.assertEqual([r.getMessage() for r in diagnostics.records],
                         ['prompt maintenance project='+self.project+' exception=CoreError'])
        self.call('project.relink',project=self.project,root=str(moved),store=str(store),confirmed_root=str(moved))
        self.root=moved
        self.service.close()
        self.service=CoreService(self.registry,config=self.config);self.addCleanup(self.service.close)
        self.catalog=self.service.catalog(self.project)
        mapping=dict(target_path=str(moved),journal_path=str(journal),confirmed_target=str(moved),confirmed_journal=str(journal))
        self.fixture_data['requests']['resume']=self.request('integration.recovery.resume',operation=self.original_operation,writers_paused=True,**mapping)
        self.crash_at(point,'resume')
        before_journal=snapshot_tree(journal)
        self.restart_service()
        self.assertEqual(snapshot_tree(journal),before_journal)
        self.assert_claims()
        _,_,outcome=self.recover()
        self.assert_finished(outcome)


# Separate unittest cases preserve the exact reached-point matrix in -v logs.
def install_case(name, function):
    function.__name__ = name
    setattr(CrashTests,name,function)


def apply_case(point, conflict=False):
    def test(self): self.run_apply_boundary(point,conflict=conflict)
    install_case('test_apply_'+point.replace(':','_'),test)


for point in ('before_reservation','journal_reserved','journal_staged','files_verified',
              'final_sql_before','final_sql_uncommitted','reply_lost'):
    apply_case(point)
for point in ('journal_created','stage_marker',*(f'stage_file_synced:{i}' for i in (5,6,7))):
    apply_case(point,conflict=True)
for ordinal,(kind,path) in enumerate(ACTIONS):
    changed = 'old_moved' if kind=='move_old' else 'new_published' if kind=='publish_new' else 'dir_changed'
    for prefix in ('marker_intent','intent',changed,'synced','marker_done','progress'):
        apply_case(f'{prefix}:{ordinal}',conflict=kind=='mkdir' and prefix in ('dir_changed','synced'))
    if kind in ('move_old','publish_new'):
        for edge in ('removing','receiving'):
            apply_case(f'rename_before_{edge}_sync:{ordinal}')
# Root publication: every SQL mutation, including terminal outcome and releases.
for ordinal in range(1,15):
    apply_case('final_sql_statement:'+str(ordinal))
for mode,points in (
    ('resume',('intent:1','rename_before_removing_sync:1','rename_before_receiving_sync:1',
               'new_published:1','marker_done:1','progress:1','final_sql_uncommitted','reply_lost')),
    ('restore-before',tuple(f'{prefix}:{ordinal}' for ordinal in (0,1)
        for prefix in ('restore_marker_intent','restore_intent','restore_rename_before_removing_sync',
                       'restore_rename_before_receiving_sync','restore_changed','restore_marker_done','restore_progress'))+
        ('restore_verified','restore_sql_uncommitted','reply_lost')),
):
    for point in points:
        def test(self,point=point,mode=mode): self.run_recovery_boundary(point,mode)
        install_case('test_'+mode.replace('-','_')+'_'+point.replace(':','_'),test)
for point in ('location_intent','location_committed'):
    def test(self,point=point): self.run_handoff_boundary(point)
    install_case('test_handoff_'+point,test)
for component in ('source','store','journal'):
    def test(self,component=component): self.unavailable_after_crash(component)
    install_case('test_unavailable_'+component+'_after_process_crash',test)
# Reverse all eight actions, including removal/recreation of directories. The
# recreated C directory has unknown ownership until its done marker is durable.
for ordinal in range(8):
    prefixes=['restore_marker_intent','restore_intent','restore_changed','restore_marker_done','restore_progress']
    if ordinal not in (3,4):
        prefixes+=['restore_rename_before_removing_sync','restore_rename_before_receiving_sync']
    for prefix in prefixes:
        point=f'{prefix}:{ordinal}'
        conflict=ordinal==4 and prefix=='restore_changed'
        def test(self,point=point,conflict=conflict):
            self.run_recovery_boundary(point,'restore-before',mixed=True,conflict=conflict)
        install_case('test_mixed_'+point.replace(':','_'),test)
