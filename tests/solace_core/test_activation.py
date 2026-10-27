from contextlib import contextmanager
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from solace_core.activation import activated_socket
from solace_core.client import CoreClient
from solace_core.errors import CoreError


class ActivationTests(unittest.TestCase):
    def test_invalid_activation_descriptors_rejected(self):
        for env in [{}, {'LISTEN_PID':str(os.getpid()),'LISTEN_FDS':'2'},
                    {'LISTEN_PID':'1','LISTEN_FDS':'1'}]:
            with self.assertRaises(CoreError): activated_socket(env)
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            with self.assertRaises(CoreError):
                activated_socket({'LISTEN_PID':str(os.getpid()),'LISTEN_FDS':'1'},fd=listener.fileno())

    def test_inherited_socket_serves_without_rebinding_or_unlinking(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory(dir=Path.cwd()) as persistent:
            base=Path(temp);base.chmod(0o700)
            runtime=base/'runtime';runtime.mkdir(mode=0o700)
            sock=runtime/'inherited.sock'
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
                listener.bind(str(sock));sock.chmod(0o600);listener.listen(16)
                inode=sock.stat().st_ino
                script='''
import os,sys,socket
os.environ['LISTEN_PID']=str(os.getpid());os.environ['LISTEN_FDS']='1'
fd=int(sys.argv[1])
if fd!=3: os.dup2(fd,3)
sys.argv=[sys.argv[0]]
from solace_core.__main__ import main
main()
'''
                env=dict(os.environ,XDG_RUNTIME_DIR=str(runtime),XDG_DATA_HOME=str(Path(persistent)/'data'))
                child=subprocess.Popen([sys.executable,'-c',script,str(listener.fileno())],pass_fds=(listener.fileno(),),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    reply=CoreClient(sock,timeout=5).request('core.status',{})
                    self.assertTrue(reply['ok'],reply)
                    self.assertEqual(reply['result']['pid'],child.pid)
                    self.assertEqual(sock.stat().st_ino,inode)
                    inet=False
                    for fd in (Path('/proc')/str(child.pid)/'fd').iterdir():
                        target=os.readlink(fd)
                        if target.startswith('socket:'):
                            number=target.split('[')[1].rstrip(']')
                            for table in ('tcp','tcp6','udp','udp6'):
                                if any(number in line.split() for line in (Path('/proc')/str(child.pid)/'net'/table).read_text().splitlines()[1:]): inet=True
                    self.assertFalse(inet)
                finally:
                    child.terminate();stdout,stderr=child.communicate(timeout=10)
                self.assertEqual(child.returncode,0,stderr.decode())
                self.assertEqual(sock.stat().st_ino,inode)

    def test_install_preview_and_idempotent_isolated_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'home with % space';root.mkdir()
            env=dict(os.environ,HOME=str(root),XDG_DATA_HOME=str(root/'data'),XDG_CONFIG_HOME=str(root/'config'))
            cmd=[sys.executable,'packaging/install-core.py']
            preview=subprocess.run(cmd+['--preview'],env=env,capture_output=True,text=True)
            self.assertEqual(preview.returncode,0,preview.stderr)
            result=json.loads(preview.stdout)
            self.assertEqual(result['core_schema_supported'],[1,2,3,4,5])
            self.assertEqual(result['core_schema_created'],5)
            self.assertFalse((root/'data').exists())
            self.assertTrue(result['runtime'].startswith(str(root)))
            applied=subprocess.run(cmd+['--apply'],env=env,capture_output=True,text=True)
            self.assertEqual(applied.returncode,0,applied.stderr)
            again=subprocess.run(cmd+['--apply'],env=env,capture_output=True,text=True)
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual(json.loads(again.stdout)['runtime'],json.loads(applied.stdout)['runtime'])
            unit=root/'config/systemd/user/solace-core.service'
            self.assertIn('%%',unit.read_text())
            verified=subprocess.run(['systemd-analyze','--user','verify',str(unit),str(unit.with_name('solace-core.socket'))],capture_output=True,text=True)
            self.assertEqual(verified.returncode,0,verified.stderr)
            launcher=root/'.local/bin/solace'
            help_=subprocess.run([str(launcher),'--help'],env=env,capture_output=True,text=True)
            self.assertEqual(help_.returncode,0,help_.stderr)
            self.assertIn('checkpoint',help_.stdout)
            runtime=Path(json.loads(applied.stdout)['runtime'])
            (runtime/'solace_core/errors.py').chmod(0o644)
            (runtime/'solace_core/errors.py').write_text('unknown runtime')
            refused=subprocess.run(cmd+['--apply'],env=env,capture_output=True,text=True)
            self.assertNotEqual(refused.returncode,0)

    def test_partial_install_failure_restores_previous_files(self):
        import importlib.util
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('installer','packaging/install-core.py')
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);launcher=home/'.local/bin/solace';launcher.parent.mkdir(parents=True)
            launcher.write_text('previous launcher');launcher.chmod(0o755)
            with patch.dict(os.environ,{'HOME':str(home),'XDG_DATA_HOME':str(home/'data'),'XDG_CONFIG_HOME':str(home/'config')}):
                data,runtime,files,destinations=installer.layout()
                original=installer.atomic_write
                def interrupted(path,content,mode=0o600):
                    if path.name=='solace-core.service': raise OSError('injected publication error')
                    return original(path,content,mode)
                with patch.object(installer,'atomic_write',side_effect=interrupted):
                    with self.assertRaises(OSError): installer.install(data,runtime,files,destinations)
                self.assertEqual(launcher.read_text(),'previous launcher')
                self.assertFalse((home/'config/systemd/user/solace-core.socket').exists())
