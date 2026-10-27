"""Revisioned task records and body-free, transaction-local request receipts."""
from datetime import datetime,timezone
import json
import sqlite3
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
        cursor=db.cursor();cursor.row_factory=sqlite3.Row
        row=cursor.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
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

    def receive_integration(self,db,task_id,before,after,now):
        """A child receipt changes only the receiving parent's revision/state."""
        db.execute("UPDATE tasks SET state=CASE WHEN state='ready' THEN 'active' ELSE state END,"
                   'revision=revision+1,updated_at=? WHERE id=?',(now,task_id))
        for checkpoint in (before,after):
            db.execute("INSERT OR IGNORE INTO task_checkpoint_refs VALUES (?,?, 'head',?)",(task_id,checkpoint,now))

    def summary(self,db,task_id):
        task=self.row(db,task_id)
        counts={state:0 for state in STATES}
        for state,count in db.execute('SELECT state,COUNT(*) FROM tasks WHERE parent_id=? GROUP BY state',(task_id,)):
            counts[state]=count
        application=dict(ready=counts['ready'],applied=0,unapplied=counts['ready'])
        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] == 3:
            workspace=db.execute('SELECT id FROM workspaces WHERE task_id=?',(task_id,)).fetchone()
            if workspace:
                application['applied']=db.execute("""
                    SELECT COUNT(*) FROM tasks t WHERE t.parent_id=? AND t.state='ready'
                    AND EXISTS (SELECT 1 FROM integration_receipts r WHERE r.task_id=t.id
                        AND r.target_key=? AND r.result_id=(SELECT checkpoint_id FROM task_checkpoint_refs
                            WHERE task_id=t.id AND role='result' ORDER BY created_at DESC,rowid DESC LIMIT 1))
                """,(task_id,'workspace:'+workspace[0])).fetchone()[0]
            application['unapplied']-=application['applied']
        if task['state']=='cancelled': aggregate='cancelled'
        elif task['state']=='blocked' or counts['blocked']: aggregate='blocked'
        elif counts['created'] or counts['active']: aggregate='waiting_for_children'
        elif application['unapplied']: aggregate='awaiting_integration'
        else: aggregate=task['state']
        return dict(children=counts,children_application=application,aggregate=aggregate)

    def show(self,task_id):
        require_tasks(self.catalog)
        from .prompts import PromptStore
        with self.catalog.connect() as db:
            task=self.row(db,task_id)
            task['profile']=json.loads(task.pop('profile_json'))
            task['prompt']=PromptStore(self.catalog,now=self.now).metadata(db,task_id)
            task.update(self.summary(db,task_id))
            task['results']=[r[0] for r in db.execute("SELECT checkpoint_id FROM task_checkpoint_refs WHERE task_id=? AND role='result' ORDER BY created_at,rowid",(task_id,))]
            task['latest_result_application']=None
            if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] == 3:
                from .integration_commit import IntegrationCommit
                parent_workspace=db.execute('SELECT id FROM workspaces WHERE task_id=?',(task['parent_id'],)).fetchone() if task['parent_id'] else None
                if not task['parent_id'] or parent_workspace:
                    target_key='workspace:'+parent_workspace[0] if parent_workspace else 'source'
                    task['latest_result_application']=IntegrationCommit.latest_application(db,task_id,target_key)
            claim=db.execute('SELECT operation_id FROM task_claims WHERE task_id=?',(task_id,)).fetchone()
            task['pending_operation']=claim[0] if claim else None
            workspace=db.execute('SELECT * FROM workspaces WHERE task_id=?',(task_id,)).fetchone()
            task['workspace']=None
            if workspace:
                from .workspace_paths import directory_identity
                w=dict(workspace)
                claims=[]
                if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] == 3:
                    from .workspace_paths import overlap
                    claims=[dict(row) for row in db.execute(
                        "SELECT c.path,CASE WHEN j.operation_id IS NOT NULL AND o.state='interrupted' THEN 'recovery_required' ELSE j.phase END AS phase FROM integration_claims c "
                        'JOIN operations o ON o.id=c.operation_id LEFT JOIN integration_journals j ON j.operation_id=c.operation_id')
                        if overlap(Path(w['path']),Path(row['path']))]
                availability='unavailable'
                try:
                    if claims:
                        availability=('recovery_required' if any(c['phase']=='recovery_required' for c in claims) else
                                      ('applying' if any(c['phase'] for c in claims) else 'busy'))
                        raise CoreError('INTEGRATION_TARGET_BUSY','The tree is reserved.','Inspect its operation.')
                    current=directory_identity(Path(w['path']),self.catalog.policy)
                    available=all(current[k]==w[k] for k in ('path','mount_id','device','inode'))
                except (OSError,CoreError): available=False
                observation=json.loads(w['observed_json']) if w['observed_json'] else None
                from .observations import summary
                observation=summary(observation)
                if observation: observation['stale']=observation['checkpoint_id']!=w['head_checkpoint']
                task['workspace']=dict(id=w['id'],path=w['path'] if available else None,
                    base_checkpoint=w['base_checkpoint'],head_checkpoint=w['head_checkpoint'],
                    availability='available' if available else availability,last_observed=observation)
        return task

    def list(self,parent=None):
        require_tasks(self.catalog)
        with self.catalog.connect() as db:
            if parent is not None:
                self.row(db,parent)
                ids=[r[0] for r in db.execute('SELECT id FROM tasks WHERE parent_id=? ORDER BY created_at,rowid',(parent,))]
            else: ids=[r[0] for r in db.execute('SELECT id FROM tasks ORDER BY created_at,rowid')]
        return [self.show(task) for task in ids]

    def list_page(self,parent=None,after=0,limit=128):
        """Stable creation-row cursor; each row shows its current state."""
        from .observations import encoded_size
        require_tasks(self.catalog)
        with self.catalog.connect() as db:
            if parent is not None:self.row(db,parent)
            rows=db.execute('SELECT rowid,id FROM tasks WHERE rowid>?'+
                            (' AND parent_id=?' if parent is not None else '')+' ORDER BY rowid LIMIT ?',
                            (after,parent,limit+1) if parent is not None else (after,limit+1)).fetchall()
        tasks=[];size=0;cursor=after
        for row in rows[:limit]:
            task=self.show(row[1]);count=encoded_size(task)+1
            if size+count>512*1024:
                if tasks:return dict(tasks=tasks,next_after=cursor)
                if count>1024*1024-4096:
                    raise CoreError('RESPONSE_TOO_LARGE','A task view exceeds the page limit.','Inspect this task locally.')
            tasks.append(task);size+=count;cursor=row[0]
        return dict(tasks=tasks,next_after=cursor if len(rows)>limit else None)

    def transition(self,request):
        require_tasks(self.catalog)
        p=request['params'];task=p['task'];method=request['method']
        with self.catalog.connect(write=True) as db:
            cached=self.lookup_request(request,db)
            if cached is not None: return cached
            row=self.assert_revision(db,task,p.get('expected_revision'));self.assert_unclaimed(db,task)
            allowed={'task.block':('active',),'task.resume':('blocked','ready'),
                     'task.cancel':('created','active','blocked','ready')}
            if row['state'] not in allowed.get(method,()):
                raise CoreError('TASK_STATE_INVALID','This transition is not allowed in the current state.','Inspect the task and choose an explicit valid transition.')
            if method=='task.cancel' and db.execute("SELECT 1 FROM tasks WHERE parent_id=? AND state<>'cancelled' LIMIT 1",(task,)).fetchone():
                raise CoreError('TASK_CHILDREN_PENDING','Children must be cancelled explicitly first.','Inspect and cancel each remaining child before cancelling its parent.')
            reason=p.get('reason') if method=='task.block' else None
            if method=='task.block' and (not isinstance(reason,str) or not reason or len(reason)>1024 or '\0' in reason):
                raise invalid('Block reason must contain 1–1024 characters without NUL.')
            state={'task.block':'blocked','task.resume':'active','task.cancel':'cancelled'}[method]
            revision=row['revision']+1;now=self.now().isoformat()
            db.execute('UPDATE tasks SET state=?,revision=?,block_reason=?,updated_at=? WHERE id=?',(state,revision,reason,now,task))
            result=dict(task_id=task,state=state,revision=revision)
            self.event(db,request,'task.'+state,state,dict(task_id=task,revision=revision))
            self.save_request(db,request,result)
        self.catalog.notify();return result

    def assert_project_unregisterable(self,db):
        if (db.execute("SELECT 1 FROM tasks WHERE state<>'cancelled' LIMIT 1").fetchone()
            or db.execute('SELECT 1 FROM task_claims LIMIT 1').fetchone()
            or db.execute("SELECT 1 FROM workspace_intents WHERE phase NOT IN ('committed','abandoned') LIMIT 1").fetchone()):
            raise CoreError('PROJECT_BUSY','This project retains noncancelled tasks or unresolved workspace operations.',
                            'Finish recovery and explicitly cancel all tasks before unregistering.')
