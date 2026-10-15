"""Real process-loss fixtures; no crash hooks enter production code."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import os
import threading
from contextlib import contextmanager
from unittest.mock import patch
from solace_core.registry import Registry
from solace_core.storage import StoragePolicy
from solace_core.service import CoreService
from solace_core.workspace_paths import CoreConfig
from solace_core.workspace_trees import WorkspaceBuilder
from solace_core.task_operations import TaskOperations
from solace_core.tasks import TaskCatalog
import solace_core.workspace_trees as trees

registry_path,project,task,request_id,phase,workspace_root=sys.argv[1:]
policy=StoragePolicy(mountinfo='1 0 0:1 / / rw - ext4 x rw\n',sync_roots=[])
service=CoreService(Registry(Path(registry_path),policy),config=CoreConfig((),Path(workspace_root)))
catalog=service.catalog(project)
original_prepare=WorkspaceBuilder.prepare
original_rename=trees._rename_noreplace
original_finish=TaskOperations.finish_start
original_connect=catalog.connect
original_notify=catalog.notify

def prepare(owner,*args):
    if phase=='before_stage': os._exit(71)
    if phase=='partial_stage':
        intent=args[2];staging=Path(intent['staging_path']);staging.mkdir(mode=0o700)
        from solace_core.workspace_paths import directory_identity
        import json
        identity=directory_identity(staging,policy)
        with original_connect(write=True) as db:
            db.execute('UPDATE workspace_intents SET staging_identity_json=? WHERE operation_id=?',(json.dumps(identity),intent['operation_id']))
        (staging/'partial').write_text('incomplete');os._exit(76)
    result=original_prepare(owner,*args)
    if phase=='after_stage': os._exit(72)
    return result

def rename(source,destination,*args):
    result=original_rename(source,destination,*args)
    if phase=='after_rename' and destination==task: os._exit(73)
    return result

@contextmanager
def connect(*args,**kwargs):
    with original_connect(*args,**kwargs) as db:
        yield db
        if kwargs.get('write') and phase in ('before_commit','ready_before_commit'):
            row=db.execute("SELECT state FROM operations WHERE request_id=?",(request_id,)).fetchone()
            if row and row[0]=='succeeded': os._exit(74)

def notify():
    with original_connect() as db:
        row=db.execute('SELECT state FROM operations WHERE request_id=?',(request_id,)).fetchone()
    if phase in ('after_commit','ready_after_commit') and row and row[0]=='succeeded': os._exit(75)
    original_notify()

# Ready uses the engine connection, so inject at its publication hook too.
original_complete=TaskOperations.complete_capture
def complete(owner,db,operation,checkpoint,**kwargs):
    original_complete(owner,db,operation,checkpoint,**kwargs)
    if phase=='ready_before_commit': os._exit(74)

with patch.object(WorkspaceBuilder,'prepare',prepare),patch.object(trees,'_rename_noreplace',rename),patch.object(catalog,'connect',connect),patch.object(catalog,'notify',notify),patch.object(TaskOperations,'complete_capture',complete):
    method='task.ready' if phase.startswith('ready_') else 'task.start'
    result=service.handle(dict(version=1,id=request_id,method=method,params=dict(project=project,task=task,expected_revision=TaskCatalog(catalog).show(task)['revision']),actor='test'))
    if not result['ok']: raise RuntimeError(result)
    threading.Event().wait(12)
raise RuntimeError('crash boundary not reached')
