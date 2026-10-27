import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from solace_core.check_process import CheckExecutor, process_start_time
from solace_core.check_staging import StagedCheck


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ('candidate', 'home', 'tmp', 'evidence'): (root/name).mkdir()
        self.stage = StagedCheck('run', root/'candidate', {}, 'a'*64, root/'home', root/'tmp', root/'evidence')

    def run_step(self, code, *, timeout=3, cancel=None, on_start=lambda pid,start: None):
        return CheckExecutor().execute(dict(name='step', argv=[sys.executable, '-c', code], timeout_seconds=timeout, output_limit_bytes=1024), self.stage, cancel or threading.Event(), on_start)

    def test_large_dual_streams_are_drained_hashed_and_capped(self):
        result = self.run_step("import os\nfor i in range(100):\n os.write(1,b'a'*4096); os.write(2,b'b'*4096)")
        self.assertEqual(result.status, 'passed')
        for name, char in [('stdout', b'a'), ('stderr', b'b')]:
            self.assertEqual(getattr(result,name+'_bytes'),409600)
            self.assertEqual(getattr(result,name+'_sha256'),hashlib.sha256(char*409600).hexdigest())
            self.assertEqual(getattr(result,name),char*1024)
            self.assertTrue(getattr(result,name+'_truncated'))

    def child_code(self):
        return "import subprocess,sys,time,signal\np=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])\nprint(p.pid,flush=True)\ntime.sleep(60)"

    def assert_child_dead(self, result):
        pid = int(result.stdout.strip())
        path = Path('/proc')/str(pid)/'stat'
        deadline=time.monotonic()+2
        while True:
            try:
                state = path.read_text().split(') ', 1)[1][0]
            except (FileNotFoundError, ProcessLookupError):
                return  # The process was reaped while its proc entry was inspected.
            if state == 'Z':
                return
            if time.monotonic() >= deadline:
                self.fail(f'child {pid} is still alive (state {state})')
            time.sleep(.01)

    def test_timeout_kills_process_group(self):
        identities=[]
        result=self.run_step(self.child_code(),timeout=.2,on_start=lambda p,s: identities.append((p,s,process_start_time(p))))
        self.assertEqual(result.status,'timed_out'); self.assertTrue(result.timed_out)
        self.assertEqual(identities[0][1],identities[0][2]); self.assert_child_dead(result)

    def test_cancel_kills_process_group_and_does_not_pass(self):
        cancel=threading.Event(); timer=threading.Timer(.2,cancel.set); timer.start(); self.addCleanup(timer.join)
        result=self.run_step(self.child_code(),cancel=cancel)
        self.assertEqual(result.status,'cancelled'); self.assertTrue(result.cancelled); self.assert_child_dead(result)

    def test_launch_error_and_signal_have_distinct_status(self):
        result=CheckExecutor().execute(dict(argv=['/no/such/command'],timeout_seconds=1,output_limit_bytes=1024),self.stage,threading.Event(),lambda p,s:None)
        self.assertEqual(result.status,'launch_error'); self.assertIsNotNone(result.launch_error)
        result=self.run_step('import os,signal; os.kill(os.getpid(),signal.SIGTERM)')
        self.assertEqual(result.status,'failed'); self.assertEqual(result.signal,signal.SIGTERM); self.assertIsNone(result.exit_code)
        self.assertLessEqual(result.started_at,result.finished_at)

    def test_environment_omits_core_secret_and_runs_fixed_argv(self):
        with patch.dict(os.environ,{'SOLACE_BACKUP_API_KEY':'secret','EXTRA_CORE_VAR':'hidden'}):
            result=self.run_step('import os,json; print(json.dumps(dict(os.environ)))')
        env=json.loads(result.stdout)
        self.assertNotIn('SOLACE_BACKUP_API_KEY',env); self.assertNotIn('EXTRA_CORE_VAR',env)
        self.assertEqual(env['HOME'],str(self.stage.home)); self.assertEqual(env['TMPDIR'],str(self.stage.tmpdir))
        step=dict(argv=[sys.executable,'-c','import sys; print(sys.argv[1])','$(touch shell-ran); literal'],timeout_seconds=1,output_limit_bytes=1024)
        result=CheckExecutor({'LANG':'C.UTF-8','PATH':'/usr/bin:/bin','FIXED':'yes'}).execute(step,self.stage,threading.Event(),lambda p,s:None)
        self.assertEqual(result.stdout,b'$(touch shell-ran); literal\n'); self.assertFalse((self.stage.root/'shell-ran').exists())

    def test_missing_identity_fails_promptly_and_reaps_child(self):
        processes = []
        from solace_core import check_process
        real_popen = check_process.subprocess.Popen
        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            # Keep a regression from hanging the test runner indefinitely.
            timer = threading.Timer(2, lambda: os.killpg(process.pid, signal.SIGKILL)
                                    if process.returncode is None else None)
            timer.start()
            self.addCleanup(timer.join)
            self.addCleanup(timer.cancel)
            return process
        started = time.monotonic()
        with patch.object(check_process, 'process_start_time', return_value=None), \
                patch.object(check_process.subprocess, 'Popen', side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, 'start identity'):
                self.run_step('import time; time.sleep(60)')
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertIsNone(process_start_time(processes[0].pid))

    def test_identity_read_failure_during_timeout_cleans_group(self):
        from solace_core import check_process
        calls = 0
        def lookup(pid):
            nonlocal calls
            calls += 1
            return process_start_time(pid) if calls == 1 else None
        started = time.monotonic()
        with patch.object(check_process, 'process_start_time', side_effect=lookup):
            result = self.run_step(self.child_code(), timeout=.2)
        self.assertEqual(result.status, 'timed_out')
        self.assertLess(time.monotonic() - started, 2)
        self.assert_child_dead(result)

    def test_precancel_does_not_launch(self):
        cancel = threading.Event()
        cancel.set()
        result = self.run_step("raise RuntimeError('must not run')", cancel=cancel,
                               on_start=lambda p, s: self.fail('launched'))
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.stdout_bytes, 0)

    def test_callback_failure_reaps_leader(self):
        identities = []
        def callback(pid, start):
            identities.append(pid)
            raise RuntimeError('persistence failed')
        with self.assertRaisesRegex(RuntimeError, 'persistence failed'):
            self.run_step('import time; time.sleep(60)', on_start=callback)
        self.assertIsNone(process_start_time(identities[0]))

    def test_exited_leader_does_not_leave_child_running(self):
        result = self.run_step("import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True)")
        self.assertEqual(result.status, 'passed')
        self.assert_child_dead(result)

if __name__ == '__main__': unittest.main()
