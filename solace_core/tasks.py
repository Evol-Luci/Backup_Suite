"""Revisioned task records and body-free, transaction-local request receipts."""
from datetime import datetime,timezone
import json
import uuid
from pathlib import Path
from .errors import CoreError,invalid
from .protocol import fingerprint
from .schema import require_tasks

STATES=('created','active','blocked','ready','cancelled')

def utcnow():
    return datetime.now(timezone.utc)


def profile(exclusions):
    if (not isinstance(exclusions,list) or any(not isinstance(s,str) or not s or '\0' in s for s in exclusions)
            or len(set(exclusions))!=len(exclusions)):
        raise invalid('Exclusions must be distinct nonempty strings.')
    return dict(version=1,protected=['.git','.solace_vault','solace_owned_roots'],exclusions=exclusions)


class TaskCatalog:
    def __init__(self,catalog,*,now=None):
        self.catalog=catalog
        self.now=now or utcnow

    def lookup_request(self,request,db):
        signature=fingerprint(request['method'],request['params'],request['actor'])
        operation=db.execute('SELECT id,fingerprint FROM operations WHERE request_id=?',(request['id'],)).fetchone()
        row=db.execute('SELECT fingerprint,result_json FROM task_requests WHERE request_id=?',(request['id'],)).fetchone()
        if operation:
            if operation['fingerprint']!=signature:
                raise CoreError('REQUEST_ID_REUSED','This request ID has different arguments.','Use the original arguments or a new request ID.')
            return dict(operation_id=operation['id'])
        if row:
            if row['fingerprint']!=signature:
                raise CoreError('REQUEST_ID_REUSED','This request ID has different arguments.','Use the original arguments or a new request ID.')
            return json.loads(row['result_json'])
        return None

    def save_request(self,db,request,result):
        db.execute('INSERT INTO task_requests VALUES (?,?,?,?)',
                   (request['id'],fingerprint(request['method'],request['params'],request['actor']),json.dumps(result),self.now().isoformat()))

    def event(self,db,request,kind,state,details):
        self.catalog._event(db,dict(id=None,actor=request['actor']),kind,state,details)

    def row(self,db,task):
        row=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        if row is None:
            raise CoreError('TASK_NOT_FOUND','The task does not exist in this project.','List this project’s tasks.')
        return dict(row)

    def assert_revision(self,db,task,expected):
        row=self.row(db,task)
        if type(expected) is not int or expected<1:
            raise invalid('Expected revision must be a positive integer.')
        if row['revision']!=expected:
            raise CoreError('TASK_REVISION_CONFLICT','The task or policy changed since it was inspected.','Inspect the current revision and decide again.')
        return row

    def assert_unclaimed(self,db,task):
        if db.execute('SELECT 1 FROM task_claims WHERE task_id=?',(task,)).fetchone():
            raise CoreError('TASK_BUSY','A core operation still owns this task.','Inspect its operation or explicit recovery before changing it.',True)

    def claim(self,db,operation_id,revisions):
        for task,revision in revisions.items():
            self.assert_revision(db,task,revision)
            self.assert_unclaimed(db,task)
        for task in revisions:
            db.execute('INSERT INTO task_claims VALUES (?,?)',(task,operation_id))

    def release(self,db,operation_id):
        db.execute('DELETE FROM task_claims WHERE operation_id=?',(operation_id,))

    def create(self,request):
        require_tasks(self.catalog)
        from .prompts import PromptStore,validate_policy,validate_prompt
        p=request['params'];title=p.get('title')
        if not isinstance(title,str) or not title.strip() or len(title)>200 or not title.isprintable():
            raise invalid('Title must contain 1–200 printable characters.')
        with self.catalog.connect(write=True) as db:
            cached=self.lookup_request(request,db)
            if cached is not None: return cached
            parent=p.get('parent');now=self.now().isoformat()
            if parent:
                row=self.assert_revision(db,parent,p.get('expected_parent_revision'))
                self.assert_unclaimed(db,parent)
                if row['parent_id']:
                    raise CoreError('TASK_DEPTH_UNSUPPORTED','Only parent tasks and their direct children are supported.','Create a direct child of a root task.')
                if row['state'] not in ('created','active','blocked'):
                    raise CoreError('TASK_STATE_INVALID','The parent cannot accept new children in its current state.','Resume a ready parent or create a new parent.')
                if 'exclusions' in p: raise invalid('Children inherit the parent capture profile.')
                effective=json.loads(row['profile_json'])
            else:
                if 'expected_parent_revision' in p: raise invalid('A root task has no parent revision.')
                effective=profile(p.get('exclusions',[]))
            policy=db.execute('SELECT policy,days FROM project_prompt_policy WHERE singleton=1').fetchone()
            mode=p.get('prompt_policy',policy['policy']);days=p.get('prompt_days',policy['days'] if mode==policy['policy'] else None)
            if mode!='days' and 'prompt_days' not in p: days=None
            validate_policy(mode,days)
            supplied=p.get('prompt');validate_prompt(supplied)
            task=uuid.uuid4().hex
            db.execute('INSERT INTO tasks VALUES (?,?,?,\'created\',1,?,NULL,?,?)',
                       (task,parent,title,json.dumps(effective,sort_keys=True),now,now))
            PromptStore(self.catalog,now=self.now).insert(db,task,supplied,mode,days,now)
            if parent:
                db.execute('UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=?',(now,parent))
            result=dict(task_id=task,revision=1)
            self.event(db,request,'task.created','created',dict(task_id=task,parent_id=parent))
            self.save_request(db,request,result)
        self.catalog.notify()
        return result

    def summary(self,db,task_id):
        task=self.row(db,task_id)
        counts={state:0 for state in STATES}
        for state,count in db.execute('SELECT state,COUNT(*) FROM tasks WHERE parent_id=? GROUP BY state',(task_id,)):
            counts[state]=count
        if task['state']=='cancelled': aggregate='cancelled'
        elif task['state']=='blocked' or counts['blocked']: aggregate='blocked'
        elif counts['created'] or counts['active']: aggregate='waiting_for_children'
        elif task['state']=='ready' and counts['ready']: aggregate='awaiting_integration'
        else: aggregate=task['state']
        return dict(children=counts,aggregate=aggregate)

    def show(self,task_id):
        require_tasks(self.catalog)
        from .prompts import PromptStore
        with self.catalog.connect() as db:
            task=self.row(db,task_id)
            task['profile']=json.loads(task.pop('profile_json'))
            task['prompt']=PromptStore(self.catalog,now=self.now).metadata(db,task_id)
            task.update(self.summary(db,task_id))
            task['results']=[r[0] for r in db.execute("SELECT checkpoint_id FROM task_checkpoint_refs WHERE task_id=? AND role='result' ORDER BY created_at,rowid",(task_id,))]
            claim=db.execute('SELECT operation_id FROM task_claims WHERE task_id=?',(task_id,)).fetchone()
            task['pending_operation']=claim[0] if claim else None
            workspace=db.execute('SELECT * FROM workspaces WHERE task_id=?',(task_id,)).fetchone()
            task['workspace']=None
            if workspace:
                from .workspace_paths import directory_identity
                w=dict(workspace)
                try:
                    current=directory_identity(Path(w['path']),self.catalog.policy)
                    available=all(current[k]==w[k] for k in ('path','mount_id','device','inode'))
                except (OSError,CoreError): available=False
                observation=json.loads(w['observed_json']) if w['observed_json'] else None
                if observation: observation['stale']=observation['checkpoint_id']!=w['head_checkpoint']
                task['workspace']=dict(id=w['id'],path=w['path'] if available else None,
                    base_checkpoint=w['base_checkpoint'],head_checkpoint=w['head_checkpoint'],
                    availability='available' if available else 'unavailable',last_observed=observation)
        return task

    def list(self,parent=None):
        require_tasks(self.catalog)
        with self.catalog.connect() as db:
            if parent is not None:
                self.row(db,parent)
                ids=[r[0] for r in db.execute('SELECT id FROM tasks WHERE parent_id=? ORDER BY created_at,rowid',(parent,))]
            else: ids=[r[0] for r in db.execute('SELECT id FROM tasks ORDER BY created_at,rowid')]
        return [self.show(task) for task in ids]
