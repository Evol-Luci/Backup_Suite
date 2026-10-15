"""Coordinated durable task operations; filesystem work precedes public state."""
from pathlib import Path
import json
import logging
import os
import sqlite3
import uuid
from .errors import CoreError,classify
from .protocol import fingerprint
from .schema import require_tasks
from .tasks import TaskCatalog
from .lineage import Lineage
from .workspace_paths import directory_identity,protected_excluder
from .workspace_trees import WorkspaceBuilder,compare_tree
from solace_engine.repository import ProjectRepository

log=logging.getLogger(__name__)


def rowdict(db,query,params=()):
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    row=cursor.execute(query,params).fetchone()
    return dict(row) if row is not None else None


class TaskOperations:
    def __init__(self,service,catalog):
        self.service,self.catalog=service,catalog
        self.paths=service.workspace_paths
        self.tasks=TaskCatalog(catalog)
        self.project=catalog.registration['id']
        self.coordinator=service.coordinator(self.project)

    def workspace(self,db,task):
        row=rowdict(db,'SELECT * FROM workspaces WHERE task_id=?',(task,))
        if row is None:
            raise CoreError('WORKSPACE_UNAVAILABLE','This task has no published workspace.','Start it or inspect its interrupted preparation.')
        self.paths.validate(Path(row['path']),expected=row)
        return row

    def eligible(self,db,task,checkpoint,parent=None):
        context=rowdict(db,'SELECT * FROM checkpoint_context WHERE checkpoint_id=? AND published=1',(checkpoint,))
        matching=context and json.loads(context['profile_json'])==json.loads(task['profile_json'])
        if parent:
            owned=db.execute('SELECT 1 FROM task_checkpoint_refs WHERE task_id=? AND checkpoint_id=?',(parent['id'],checkpoint)).fetchone()
        else:
            owned=db.execute("SELECT 1 FROM task_checkpoint_refs r JOIN tasks t ON t.id=r.task_id WHERE r.checkpoint_id=? AND t.state='cancelled' LIMIT 1",(checkpoint,)).fetchone()
        if not matching or not owned:
            raise CoreError('BASELINE_INELIGIBLE','The checkpoint is not eligible for this task.','Select published parent history, or matching cancelled-task history for a root task.')

    def accept(self,request):
        require_tasks(self.catalog)
        p=request['params'];method=request['method'];task_id=p['task']
        with self.catalog.connect(write=True) as db:
            cached=self.tasks.lookup_request(request,db)
            if cached is not None: return cached
            task=self.tasks.row(db,task_id)
            expected_revision=p.get('expected_revision',task['revision'])
            self.tasks.assert_revision(db,task_id,expected_revision);self.tasks.assert_unclaimed(db,task_id)
            context=dict(task=task_id,expected_revision=expected_revision,state=task['state'],profile=json.loads(task['profile_json']),
                         store_identity=directory_identity(self.catalog.store,self.catalog.policy))
            revisions={task_id:expected_revision};parent=None;selected=None
            if method=='task.start':
                if task['state']!='created': self.invalid_state()
                if task['parent_id']:
                    parent=self.tasks.assert_revision(db,task['parent_id'],p.get('expected_parent_revision'))
                    self.tasks.assert_unclaimed(db,parent['id'])
                    if parent['state'] not in ('active','blocked'): self.invalid_state()
                    workspace=self.workspace(db,parent['id'])
                    revisions[parent['id']]=parent['revision']
                    context.update(parent=parent['id'],parent_revision=parent['revision'],parent_workspace=workspace,
                                   capture_identity=directory_identity(Path(workspace['path']),self.catalog.policy),capture_parent=workspace['head_checkpoint'])
                else:
                    if 'expected_parent_revision' in p: raise CoreError('INVALID_REQUEST','Root tasks have no parent revision.','Remove the parent revision.')
                    if 'from_checkpoint' not in p:
                        self.catalog.validate()
                        context.update(capture_identity=directory_identity(Path(self.catalog.registration['root']),self.catalog.policy),
                                       capture_parent=Lineage(self.catalog).source_head(db))
                if 'from_checkpoint' in p:
                    self.eligible(db,task,p['from_checkpoint'],parent)
                    selected=p['from_checkpoint']
                context['explicit_base']=selected is not None
            else:
                if method in ('task.ready','workspace.checkpoint') and task['state'] not in ('active','blocked'): self.invalid_state()
                workspace=self.workspace(db,task_id)
                context['workspace']=workspace
                if method=='task.ready': self.children_ready(db,task_id)
            operation=uuid.uuid4().hex;now=self.catalog._now()
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (operation,request['id'],fingerprint(method,p,request['actor']),method,request['actor'],'queued',now,now,None,None,json.dumps(context)))
            self.tasks.claim(db,operation,revisions)
            if method=='task.start':
                selected_paths=self.paths.reserve(self.project,task_id,operation)
                context['destination_parent']=self.paths.ensure_parent(Path(selected_paths['final_path']))
                db.execute('UPDATE operations SET params_json=? WHERE id=?',(json.dumps(context),operation))
                db.execute('INSERT INTO workspace_intents(operation_id,task_id,workspace_id,kind,phase,staging_path,final_path,base_checkpoint,expected_json) VALUES (?,?,?,\'start\',\'reserved\',?,?,?,?)',
                    (operation,task_id,uuid.uuid4().hex,selected_paths['staging_path'],selected_paths['final_path'],selected,json.dumps(context)))
            self.catalog._event(db,dict(id=operation,actor=request['actor']),'operation.queued','queued',dict(task_id=task_id))
        self.catalog.notify()
        self.coordinator.submit(operation,lambda:self.run(operation,request))
        return dict(operation_id=operation)

    @staticmethod
    def invalid_state():
        raise CoreError('TASK_STATE_INVALID','This action is not allowed in the current task state.','Inspect the task and choose an explicit valid transition.')

    @staticmethod
    def children_ready(db,task):
        if db.execute("SELECT 1 FROM tasks WHERE parent_id=? AND state IN ('created','active','blocked') LIMIT 1",(task,)).fetchone():
            raise CoreError('TASK_CHILDREN_PENDING','Some children still have unfinished work.','Save their results or explicitly cancel them first.')

    def context(self,db,operation):
        row=rowdict(db,'SELECT * FROM operations WHERE id=?',(operation,))
        return row,json.loads(row['params_json'])

    def check(self,db,operation,context):
        self.catalog.validate(require_root=False)
        if directory_identity(self.catalog.store,self.catalog.policy)!=context['store_identity']:
            raise CoreError('STORE_IDENTITY_MISMATCH','The store directory identity changed.','Preserve storage and inspect recovery.')
        task=self.tasks.assert_revision(db,context['task'],context['expected_revision'])
        if task['state']!=context['state']: self.invalid_state()
        wanted=[context['task']]
        if context.get('parent'):
            self.tasks.assert_revision(db,context['parent'],context['parent_revision']);wanted.append(context['parent'])
        for task_id in wanted:
            claim=db.execute('SELECT operation_id FROM task_claims WHERE task_id=?',(task_id,)).fetchone()
            if claim is None or claim[0]!=operation:
                raise CoreError('TASK_BUSY','The operation no longer owns its task.','Inspect the current operation and recovery record.')
        if 'capture_identity' in context:
            expected=context['capture_identity']
            if directory_identity(Path(expected['path']),self.catalog.policy)!=expected:
                raise CoreError('WORKSPACE_IDENTITY_MISMATCH','Capture input identity changed.','Reconnect or explicitly relink the input.')
        return task

    def run(self,operation,request):
        try:
            self.catalog.transition(operation,'running')
            method=request['method']
            if method=='task.start': self.start(operation)
            elif method=='workspace.status': self.refresh(operation)
            else: self.capture(operation,request)
        except Exception as error:
            with self.coordinator.lock:
                try:
                    current=self.catalog.get_operation(operation)
                    if current['state'] in ('queued','running'):
                        with self.catalog.connect(write=True) as db:
                            intent=rowdict(db,'SELECT * FROM workspace_intents WHERE operation_id=?',(operation,))
                            unresolved=intent and (intent['staging_identity_json'] or os.path.lexists(intent['final_path']))
                            if not unresolved:
                                self.tasks.release(db,operation)
                                if intent: db.execute("UPDATE workspace_intents SET phase='abandoned' WHERE operation_id=?",(operation,))
                            self.catalog._transition(db,operation,'failed',error=classify(error).as_dict())
                        self.catalog.notify()
                except Exception:
                    self.service._pending_recovery.add(self.project)
                    log.error('task operation unavailable id=%s exception=%s',operation,type(error).__name__)

    def start(self,operation):
        with self.coordinator.lock,self.catalog.connect() as db:
            intent=rowdict(db,'SELECT * FROM workspace_intents WHERE operation_id=?',(operation,))
            context=json.loads(intent['expected_json']);self.check(db,operation,context)
        if intent['base_checkpoint'] is None:
            identity=context['capture_identity'];repo=ProjectRepository.open_existing(Path(identity['path']),self.catalog.store)
            owner=context.get('parent',context['task'])
            workspace=context['parent_workspace']['id'] if context.get('parent') else intent['workspace_id']
            purpose='child_base' if context.get('parent') else 'task_base'
            def prepared(db,cp):
                self.check(db,operation,context)
                db.execute('INSERT INTO checkpoint_context VALUES (?,?,?,?,?,0)',(cp.id,purpose,owner,workspace,json.dumps(context['profile'],sort_keys=True)))
                db.execute('UPDATE workspace_intents SET base_checkpoint=? WHERE operation_id=?',(cp.id,operation))
            cp=repo.create_checkpoint('Task baseline '+context['task'],context['profile']['exclusions'],
                parent_id=context['capture_parent'],excluded=protected_excluder(repo.project_root,context['profile'],[self.catalog.store,self.paths.config.workspace_root]),
                publication_hook=prepared,publication_guard=self.coordinator.lock)
            intent['base_checkpoint']=cp.id
        else:
            # An existing directory is sufficient for immutable object reads;
            # this repository view never captures the store's parent tree.
            repo=ProjectRepository.open_existing(self.catalog.store.parent,self.catalog.store)
        builder=WorkspaceBuilder(self.catalog,self.paths)
        staged=builder.prepare(repo,intent['base_checkpoint'],intent)
        with self.coordinator.lock,self.catalog.connect() as db: self.check(db,operation,context)
        identity=builder.publish(intent,staged)
        self.finish_start(operation,intent,identity)

    def finish_start(self,operation,intent,identity):
        context=json.loads(intent['expected_json']);checkpoint=intent['base_checkpoint'];now=self.catalog._now()
        with self.coordinator.lock,self.catalog.connect(write=True) as db:
            self.check(db,operation,context)
            self.paths.validate(Path(intent['final_path']),expected=identity)
            cp=db.execute('SELECT logical_bytes FROM checkpoints WHERE id=?',(checkpoint,)).fetchone()
            observed=dict(checkpoint_id=checkpoint,observed_at=now,clean=True,added=[],removed=[],changed=[],logical_bytes=cp[0],warnings=[])
            db.execute('INSERT INTO workspaces VALUES (?,?,?,?,?,?,?,?,?,?)',
                (intent['workspace_id'],context['task'],identity['path'],identity['mount_id'],identity['device'],identity['inode'],checkpoint,checkpoint,now,json.dumps(observed)))
            db.execute('UPDATE checkpoint_context SET published=1 WHERE checkpoint_id=?',(checkpoint,))
            for role in ('base','head'): self.ref(db,context['task'],checkpoint,role,now)
            if context.get('parent') and not context['explicit_base']:
                db.execute('UPDATE workspaces SET head_checkpoint=? WHERE task_id=?',(checkpoint,context['parent']))
                self.ref(db,context['parent'],checkpoint,'head',now)
                db.execute('UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=?',(now,context['parent']))
            revision=context['expected_revision']+1
            db.execute("UPDATE tasks SET state='active',revision=?,updated_at=? WHERE id=?",(revision,now,context['task']))
            db.execute("UPDATE workspace_intents SET phase='committed' WHERE operation_id=?",(intent['operation_id'],))
            result=dict(task_id=context['task'],workspace_id=intent['workspace_id'],path=identity['path'],base_checkpoint=checkpoint,head_checkpoint=checkpoint,
                        revision=revision,warnings=['Protected Git/vault/Solace metadata excluded. Empty directories are not captured. Pause external writers for stable captures.'])
            self.catalog._transition(db,operation,'succeeded',result=result,kind='task.started');self.tasks.release(db,operation)
        self.catalog.notify()

    @staticmethod
    def ref(db,task,checkpoint,role,now):
        db.execute('INSERT OR IGNORE INTO task_checkpoint_refs VALUES (?,?,?,?)',(task,checkpoint,role,now))

    def capture(self,operation,request):
        with self.coordinator.lock,self.catalog.connect() as db:
            op,context=self.context(db,operation);self.check(db,operation,context)
            workspace=self.workspace(db,context['task'])
        repo=ProjectRepository.open_existing(Path(workspace['path']),self.catalog.store)
        ready=request['method']=='task.ready';profile=context['profile']
        message='Ready '+context['task'] if ready else request['params']['message']
        repo.create_checkpoint(message,profile['exclusions'],parent_id=workspace['head_checkpoint'],
            excluded=protected_excluder(repo.project_root,profile,[self.catalog.store,self.paths.config.workspace_root]),
            publication_guard=self.coordinator.lock,
            publication_hook=lambda db,cp:self.complete_capture(db,operation,cp,ready=ready))
        self.catalog.notify()

    def complete_capture(self,db,operation_id,checkpoint,*,ready):
        operation,context=self.context(db,operation_id);self.check(db,operation_id,context)
        workspace=self.workspace(db,context['task']);now=self.catalog._now()
        if ready: self.children_ready(db,context['task'])
        if workspace['head_checkpoint']!=checkpoint.parent_id:
            raise CoreError('TASK_REVISION_CONFLICT','The workspace head changed.','Inspect it before capturing again.')
        db.execute('INSERT INTO checkpoint_context VALUES (?,?,?,?,?,1)',
            (checkpoint.id,'ready' if ready else 'workspace',context['task'],workspace['id'],json.dumps(context['profile'],sort_keys=True)))
        self.ref(db,context['task'],checkpoint.id,'head',now)
        if ready: self.ref(db,context['task'],checkpoint.id,'result',now)
        db.execute('UPDATE workspaces SET head_checkpoint=? WHERE id=?',(checkpoint.id,workspace['id']))
        state='ready' if ready else context['state'];revision=context['expected_revision']+1
        db.execute('UPDATE tasks SET state=?,revision=?,updated_at=? WHERE id=?',(state,revision,now,context['task']))
        self.catalog._transition(db,operation_id,'succeeded',result=dict(checkpoint_id=checkpoint.id,task_id=context['task'],workspace_id=workspace['id'],
            revision=revision,file_count=checkpoint.file_count,logical_bytes=checkpoint.logical_bytes),kind='task.ready' if ready else 'workspace.checkpointed')
        self.tasks.release(db,operation_id)

    def refresh(self,operation):
        with self.coordinator.lock,self.catalog.connect() as db:
            op,context=self.context(db,operation);self.check(db,operation,context);workspace=self.workspace(db,context['task'])
        repo=ProjectRepository.open_existing(Path(workspace['path']),self.catalog.store)
        report=compare_tree(repo,workspace['head_checkpoint'],repo.project_root,context['profile'])
        with self.coordinator.lock,self.catalog.connect(write=True) as db:
            self.check(db,operation,context);self.workspace(db,context['task'])
            db.execute('UPDATE workspaces SET observed_at=?,observed_json=? WHERE id=?',(report['observed_at'],json.dumps(report),workspace['id']))
            self.catalog._transition(db,operation,'succeeded',result=report,kind='workspace.observed');self.tasks.release(db,operation)
        self.catalog.notify()
