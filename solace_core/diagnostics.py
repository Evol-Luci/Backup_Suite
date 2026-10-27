"""Read-only diagnostics: never initialize schemas or repair stored data."""
from pathlib import Path
import os
import stat
from .catalog import ProjectCatalog, readonly_database
from .errors import CoreError, classify


def inspect(registration, policy=None):
    result = dict(project=registration['id'], root=registration['root'], store=registration['store'], ok=False)
    try:
        catalog = ProjectCatalog(registration, policy)
        catalog.validate(require_root=False)
        for path in (catalog.store, catalog.database):
            mode = path.stat().st_mode
            if path.stat().st_uid != os.getuid() or mode & 0o077:
                raise CoreError('PERMISSION_DENIED', 'Stored metadata is not private to this user.', 'Check ownership and permissions without changing stored content.')
        with readonly_database(catalog.database) as db:
            checks = [row[0] for row in db.execute('PRAGMA integrity_check')]
            if checks != ['ok']:
                raise CoreError('DATABASE_CORRUPT', 'Database integrity checks failed.', 'Preserve the store for explicit recovery.')
            version=db.execute('SELECT schema_version FROM core_metadata WHERE singleton=1').fetchone()[0]
            result.update(core_schema=version,engine_schema=db.execute('PRAGMA user_version').fetchone()[0],
                          task_upgrade_required=(version==1),database_rollback='Runtime rollback does not downgrade project databases or erase task/workspace references.')
            if version in (2, 3, 4, 5):
                result['tasks']={state:0 for state in ('created','active','blocked','ready','cancelled')}
                result['tasks'].update({row[0]:row[1] for row in db.execute('SELECT state,COUNT(*) FROM tasks GROUP BY state')})
                result['workspaces']=[]
                from .workspace_paths import directory_identity
                for row in db.execute('SELECT id,task_id,path,mount_id,device,inode FROM workspaces'):
                    identity=dict(zip(('id','task_id','path','mount_id','device','inode'),row))
                    try:
                        actual=directory_identity(Path(identity['path']),catalog.policy)
                        available=all(actual[key]==identity[key] for key in ('path','mount_id','device','inode'))
                    except (OSError,CoreError):available=False
                    result['workspaces'].append(dict(id=identity['id'],task_id=identity['task_id'],available=available))
            if version in (3, 4, 5):
                # Aggregate metadata only; never load plans/CAS, approvals or prompt bodies.
                result['integrations'] = dict(
                    plans={r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM integration_plans GROUP BY state')},
                    receipts=db.execute('SELECT COUNT(*) FROM integration_receipts').fetchone()[0],
                    interrupted_exports=db.execute("SELECT COUNT(*) FROM integration_exports WHERE phase='interrupted'").fetchone()[0])
                result['integration_recovery'] = [dict(operation_id=r[0],plan_id=r[1],phase=r[2],validation='deferred') for r in db.execute("SELECT operation_id,plan_id,phase FROM integration_journals WHERE phase NOT IN ('committed','restored','abandoned')")]
            pending = [dict(id=row[0],kind=row[1],state=row[2]) for row in db.execute("SELECT id,kind,state FROM operations WHERE state IN ('queued','running','interrupted')")]
        result.update(ok=True, operations=pending)
        root=Path(registration['root'])
        if root.is_symlink() or not root.is_dir():
            raise CoreError('PROJECT_UNAVAILABLE','The source directory is unavailable.','Explicitly relink the source root.')
    except Exception as error:
        result['ok']=False
        result['error'] = classify(error).as_dict()
    return result


def inspect_registry(registry, project=None):
    try:
        if not registry.read_only:
            from .registry import Registry
            registry = Registry(registry.path, registry.policy, read_only=True)
        if project:
            records = [registry.get(registry.resolve(project))]
        else:
            records = registry.list()
        with registry.connect() as db:
            integrity = [row[0] for row in db.execute('PRAGMA integrity_check')]
            intents = [dict(project=row[0], state=row[1]) for row in db.execute('SELECT project_id,state FROM registration_intents')]
        reports = [inspect(record, registry.policy) for record in records]
        return dict(ok=integrity == ['ok'] and all(r['ok'] for r in reports) and not intents,
                    registry=dict(path=str(registry.path),ok=integrity == ['ok'],registration_intents=intents), projects=reports)
    except Exception as error:
        return dict(ok=False, registry=dict(path=str(registry.path),ok=False,error=classify(error).as_dict()), projects=[])
