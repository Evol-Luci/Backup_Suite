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
        catalog.validate()
        for path in (catalog.store, catalog.database):
            mode = path.stat().st_mode
            if path.stat().st_uid != os.getuid() or mode & 0o077:
                raise CoreError('PERMISSION_DENIED', 'Stored metadata is not private to this user.', 'Check ownership and permissions without changing stored content.')
        with readonly_database(catalog.database) as db:
            checks = [row[0] for row in db.execute('PRAGMA integrity_check')]
            if checks != ['ok']:
                raise CoreError('DATABASE_CORRUPT', 'Database integrity checks failed.', 'Preserve the store for explicit recovery.')
            pending = [dict(id=row[0],kind=row[1],state=row[2]) for row in db.execute("SELECT id,kind,state FROM operations WHERE state IN ('queued','running','interrupted')")]
        result.update(ok=True, operations=pending)
    except Exception as error:
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
