"""Explicit source history; task checkpoints never become the source head."""
import json
from dataclasses import asdict
from .schema import require_tasks

class Lineage:
    def __init__(self,catalog):
        self.catalog=catalog

    def source_head(self,db=None):
        if db is not None:
            return db.execute('SELECT checkpoint_id FROM source_head WHERE singleton=1').fetchone()[0]
        with self.catalog.connect() as connection:
            return self.source_head(connection)

    def publish_source(self,db,checkpoint,exclusions):
        profile=json.dumps(dict(version=1,protected=[],exclusions=list(exclusions)),sort_keys=True)
        db.execute("INSERT INTO checkpoint_context VALUES (?,'source',NULL,NULL,?,1)",(checkpoint.id,profile))
        db.execute('UPDATE source_head SET checkpoint_id=? WHERE singleton=1',(checkpoint.id,))

    def publish_integration(self,db,before,after,target):
        """Expose existing C/M contexts and advance only their logical target."""
        for checkpoint in (before,after):
            db.execute('UPDATE checkpoint_context SET published=1 WHERE checkpoint_id=?',(checkpoint,))
        if target.task_id:
            db.execute('UPDATE workspaces SET head_checkpoint=?,observed_at=NULL,observed_json=NULL WHERE id=?',
                       (after,target.key.split(':',1)[1]))
        else:
            db.execute('UPDATE source_head SET checkpoint_id=? WHERE singleton=1',(after,))

    def published(self,all_workspaces=False):
        with self.catalog.connect() as db:
            contexts={r['checkpoint_id']:dict(r) for r in db.execute('SELECT * FROM checkpoint_context WHERE published=1')}
        result=[]
        for cp in self.catalog.open_existing().list_checkpoints():
            context=contexts.get(cp.id)
            if context is None or (not all_workspaces and context['purpose']!='source'):
                continue
            context.pop('checkpoint_id');context.pop('published')
            context['profile']=json.loads(context.pop('profile_json'))
            result.append(dict(asdict(cp),**context))
        return result

    def source_usage(self):
        repo=self.catalog.open_existing()
        with self.catalog.connect() as db:
            head=self.source_head(db)
            logical=db.execute('SELECT COALESCE(SUM(size),0) FROM file_entries WHERE checkpoint_id=?',(head,)).fetchone()[0]
            cache=db.execute('SELECT COALESCE(SUM(logical_bytes),0) FROM workspace_caches').fetchone()[0]
            refs=db.execute('SELECT COUNT(*) FROM task_checkpoint_refs').fetchone()[0]
            observations=[json.loads(row[0]) if row[0] else None for row in db.execute('SELECT observed_json FROM workspaces')]
            workspace_bytes=sum(row['logical_bytes'] for row in observations) if all(row is not None for row in observations) else None
        physical=repo.storage_usage().physical_bytes
        return dict(source_head=head,logical_bytes=logical,physical_bytes=physical,object_bytes=physical,
                    reclaimable_bytes=None,cache_logical_bytes=cache,workspace_logical_bytes=workspace_bytes,
                    workspace_measurement='last observed; may be stale',retained_task_references=refs,
                    unique_physical_bytes=None,shared_extent_accounting='unavailable')
