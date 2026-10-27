"""Exact optional prompt bodies, fixed retention and immediate read-time expiry."""
from datetime import datetime,timedelta
from .errors import CoreError,invalid
from .schema import require_tasks
from .tasks import TaskCatalog,utcnow


def validate_prompt(body):
    if body is None: return
    try:
        valid=isinstance(body,str) and '\0' not in body and len(body.encode('utf-8','strict'))<=131072
    except UnicodeError:
        valid=False
    if not valid:
        raise CoreError('PROMPT_INVALID','Prompt must be valid UTF-8 text without NUL, at most 128 KiB.','Use a smaller UTF-8 text file; its contents were not logged.')


def validate_policy(policy,days):
    if (policy not in ('days','retain','none') or
        (policy=='days' and (type(days) is not int or days<=0 )) or
        (policy!='days' and days is not None)):
        raise invalid('Use retain, none, or days with a positive integer day count.')
    if policy=='days':
        try: utcnow()+timedelta(days=days)
        except (OverflowError,ValueError): raise invalid('Retention days must fit the supported UTC date range through year 9999.')


class PromptStore:
    def __init__(self,catalog,*,now=None):
        self.catalog=catalog
        self.now=now or utcnow
        self.tasks=TaskCatalog(catalog,now=self.now)

    def policy(self):
        require_tasks(self.catalog)
        with self.catalog.connect() as db:
            return dict(db.execute('SELECT policy,days,revision FROM project_prompt_policy WHERE singleton=1').fetchone())

    def set_policy(self,request):
        require_tasks(self.catalog)
        p=request['params'];policy=p['policy'];days=p.get('days');validate_policy(policy,days)
        with self.catalog.connect(write=True) as db:
            cached=self.tasks.lookup_request(request,db)
            if cached is not None: return cached
            old=db.execute('SELECT revision FROM project_prompt_policy WHERE singleton=1').fetchone()[0]
            if type(p.get('expected_revision')) is not int or p['expected_revision']!=old:
                raise CoreError('TASK_REVISION_CONFLICT','The prompt policy changed.','Inspect its revision and decide again.')
            result=dict(policy=policy,days=days,revision=old+1)
            db.execute('UPDATE project_prompt_policy SET policy=?,days=?,revision=? WHERE singleton=1',(policy,days,old+1))
            self.tasks.event(db,request,'project.prompt_policy.changed','succeeded',dict(policy=policy,days=days))
            self.tasks.save_request(db,request,result)
        self.catalog.notify()
        return result

    def insert(self,db,task_id,supplied,policy,days,created_at):
        validate_prompt(supplied);validate_policy(policy,days)
        expires=None
        if supplied is None: status='missing';body=None
        elif policy=='none': status='discarded';body=None
        else:
            status='available';body=supplied
            if policy=='days':
                try: expires=(datetime.fromisoformat(created_at)+timedelta(days=days)).isoformat()
                except (OverflowError,ValueError): raise invalid('The retention day count exceeds the supported UTC date range.')
        db.execute('INSERT INTO task_prompts VALUES (?,?,?,?,?,?,NULL)',(task_id,policy,days,status,body,expires))

    def metadata(self,db,task_id):
        row=db.execute('SELECT policy,days,status,expires_at FROM task_prompts WHERE task_id=?',(task_id,)).fetchone()
        if row is None:
            self.tasks.row(db,task_id)
            raise CoreError('DATABASE_CORRUPT','A task is missing its prompt metadata.','Preserve the store for inspection.')
        value=dict(row)
        if value['status']=='available' and value['expires_at'] and self.now()>=datetime.fromisoformat(value['expires_at']):
            value['status']='expired'
        return value

    def read(self,task_id):
        require_tasks(self.catalog)
        with self.catalog.connect() as db:
            metadata=self.metadata(db,task_id)
            if metadata['status']=='available':
                metadata['body']=db.execute('SELECT body FROM task_prompts WHERE task_id=?',(task_id,)).fetchone()[0]
        return metadata

    def delete(self,request):
        require_tasks(self.catalog)
        p=request['params'];task=p['task']
        with self.catalog.connect(write=True) as db:
            cached=self.tasks.lookup_request(request,db)
            if cached is not None: return cached
            row=self.tasks.assert_revision(db,task,p.get('expected_revision'));self.tasks.assert_unclaimed(db,task)
            old=db.execute('SELECT status FROM task_prompts WHERE task_id=?',(task,)).fetchone()[0]
            revision=row['revision']
            if old=='available':
                revision+=1;now=self.now().isoformat()
                db.execute("UPDATE task_prompts SET body=NULL,status='deleted',redacted_at=? WHERE task_id=?",(now,task))
                db.execute('UPDATE tasks SET revision=?,updated_at=? WHERE id=?',(revision,now,task))
                self.tasks.event(db,request,'task.prompt.redacted',row['state'],dict(task_id=task,reason='explicit_delete'))
            result=dict(task_id=task,revision=revision)
            self.tasks.save_request(db,request,result)
        self.catalog.notify()
        return result

    def purge_expired(self,*,limit=100):
        require_tasks(self.catalog)
        if type(limit) is not int or limit<1: raise invalid('Purge limit must be positive.')
        now=self.now().isoformat()
        with self.catalog.connect(write=True) as db:
            rows=db.execute("SELECT p.task_id,t.state FROM task_prompts p JOIN tasks t ON t.id=p.task_id WHERE p.status='available' AND p.expires_at IS NOT NULL AND p.expires_at<=? AND NOT EXISTS(SELECT 1 FROM task_claims c WHERE c.task_id=p.task_id) ORDER BY p.expires_at LIMIT ?",(now,limit)).fetchall()
            for task,state in rows:
                db.execute("UPDATE task_prompts SET body=NULL,status='expired',redacted_at=? WHERE task_id=?",(now,task))
                db.execute('UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=?',(now,task))
                self.tasks.event(db,dict(actor='core'),'task.prompt.redacted',state,dict(task_id=task,reason='expired'))
        if rows: self.catalog.notify()
        return len(rows)
