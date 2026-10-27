"""Explicit, consistent-backup upgrades; never migrate during catalog open."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from .catalog import readonly_database
from .errors import CoreError
from .protocol import fingerprint
from .check_schema import DDL as CHECK_DDL, create_schema_v4
from .integration_schema import DDL as INTEGRATION_DDL, create_schema_v3
from solace_engine.object_store import ObjectStore

DDL = """
CREATE TABLE source_head (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 checkpoint_id TEXT REFERENCES checkpoints(id)
);
CREATE TABLE project_prompt_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 revision INTEGER NOT NULL CHECK(revision>=1),
 policy TEXT NOT NULL CHECK(policy IN ('days','retain','none')),
 days INTEGER,
 CHECK((policy='days' AND days IS NOT NULL AND days>0) OR (policy<>'days' AND days IS NULL))
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY,
 parent_id TEXT REFERENCES tasks(id),
 title TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('created','active','blocked','ready','cancelled')),
 revision INTEGER NOT NULL CHECK(revision>=1),
 profile_json TEXT NOT NULL,
 block_reason TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX tasks_parent ON tasks(parent_id);
CREATE TABLE task_prompts (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),
 policy TEXT NOT NULL CHECK(policy IN ('days','retain','none')),
 days INTEGER,
 status TEXT NOT NULL CHECK(status IN ('missing','available','discarded','deleted','expired')),
 body TEXT,
 expires_at TEXT,
 redacted_at TEXT,
 CHECK((policy='days' AND days IS NOT NULL AND days>0) OR (policy<>'days' AND days IS NULL)),
 CHECK((status='available' AND body IS NOT NULL AND policy<>'none')
    OR (status<>'available' AND body IS NULL))
);
CREATE TABLE workspaces (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 path TEXT NOT NULL UNIQUE,
 mount_id INTEGER NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
 base_checkpoint TEXT NOT NULL REFERENCES checkpoints(id),
 head_checkpoint TEXT NOT NULL REFERENCES checkpoints(id),
 observed_at TEXT, observed_json TEXT
);
CREATE TABLE checkpoint_context (
 checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
 purpose TEXT NOT NULL CHECK(purpose IN ('source','task_base','child_base','workspace','ready')),
 task_id TEXT REFERENCES tasks(id),
 workspace_id TEXT,
 profile_json TEXT NOT NULL,
 published INTEGER NOT NULL CHECK(published IN (0,1))
);
CREATE TABLE task_checkpoint_refs (
 task_id TEXT NOT NULL REFERENCES tasks(id),
 checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
 role TEXT NOT NULL CHECK(role IN ('base','head','result')),
 created_at TEXT NOT NULL,
 PRIMARY KEY(task_id,checkpoint_id,role)
);
CREATE TABLE task_requests (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE task_claims (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),
 operation_id TEXT NOT NULL REFERENCES operations(id)
);
CREATE TABLE workspace_intents (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 task_id TEXT NOT NULL REFERENCES tasks(id), workspace_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('start','relink')),
 phase TEXT NOT NULL CHECK(phase IN ('reserved','staged','published','committed','abandoned')),
 staging_path TEXT, final_path TEXT NOT NULL,
 staging_identity_json TEXT, final_identity_json TEXT,
 base_checkpoint TEXT REFERENCES checkpoints(id),
 expected_json TEXT NOT NULL,
 recovery_operation_id TEXT REFERENCES operations(id)
);
CREATE UNIQUE INDEX workspace_intents_live ON workspace_intents(workspace_id)
 WHERE phase NOT IN ('committed','abandoned');
CREATE TABLE workspace_caches (
 checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
 cache_id TEXT NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
 identity_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 logical_bytes INTEGER NOT NULL CHECK(logical_bytes>=0),
 created_at TEXT NOT NULL
);
"""


def create_schema_v2(db):
    for statement in DDL.split(';'):
        if statement.strip():
            db.execute(statement)
    db.execute('INSERT INTO source_head VALUES (1,NULL)')
    db.execute("INSERT INTO project_prompt_policy VALUES (1,1,'days',30)")


def schema_version(catalog):
    catalog.validate(require_root=False, require_objects=False)
    with readonly_database(catalog.database) as db:
        return db.execute('SELECT schema_version FROM core_metadata WHERE singleton=1').fetchone()[0]


def require_tasks(catalog):
    if schema_version(catalog) not in (2, 3, 4):
        raise CoreError('PROJECT_UPGRADE_REQUIRED', 'This project requires the task catalog upgrade.',
                        'Run project upgrade --preview, review it, then apply its plan token.')


def require_integrations(catalog) -> None:
    if schema_version(catalog) not in (3, 4):
        raise CoreError("PROJECT_UPGRADE_REQUIRED",
                        "This project needs the integration catalog upgrade.",
                        "Review project upgrade --preview before applying its token.")


def _snapshot(catalog, db):
    version = db.execute('SELECT schema_version FROM core_metadata WHERE singleton=1').fetchone()[0]
    if version == 1:
        head = db.execute('SELECT id FROM checkpoints ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
    else:
        head = db.execute('SELECT checkpoint_id FROM source_head WHERE singleton=1').fetchone()
    return dict(ddl_sha256=hashlib.sha256(DDL.encode()).hexdigest(),
                integration_ddl_sha256=hashlib.sha256(INTEGRATION_DDL.encode()).hexdigest(),
                check_ddl_sha256=hashlib.sha256(CHECK_DDL.encode()).hexdigest(),
                target_schema=4 if version >= 3 else 3,
                project=catalog.registration['id'], store_uuid=catalog.registration['store_uuid'],
                store=str(catalog.store), mount_id=catalog.registration['mount_id'],
                core_schema=version, engine_schema=db.execute('PRAGMA user_version').fetchone()[0],
                source_head=head[0] if head else None,
                checkpoint_count=db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0],
                events_max=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0])


def _token(snapshot):
    return hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def preview_upgrade(catalog):
    catalog.validate(require_root=False)
    with readonly_database(catalog.database) as db:
        snapshot = _snapshot(catalog,db)
        pending = db.execute("SELECT 1 FROM operations WHERE state IN ('queued','running') LIMIT 1").fetchone()
    return dict(required=snapshot['core_schema'] in (1, 2, 3), from_schema=snapshot['core_schema'], to_schema=snapshot['target_schema'],
                plan_token=_token(snapshot), busy=bool(pending),
                backup_directory=str(catalog.store/'schema-backups'),
                rollback='Runtime rollback does not downgrade project databases.', snapshot=snapshot)


def apply_upgrade(catalog, plan_token, request):
    version = schema_version(catalog)
    signature = fingerprint(request['method'],request['params'],request['actor'])
    # Replay precedes upgrade eligibility, source availability and backup creation.
    with catalog.connect() as db:
        operation = db.execute('SELECT 1 FROM operations WHERE request_id=?', (request['id'],)).fetchone()
        if operation:
            raise CoreError('REQUEST_ID_REUSED','This request ID belongs to an operation.','Use a new request ID.')
        if version in (2, 3, 4):
            row = db.execute('SELECT fingerprint,result_json FROM task_requests WHERE request_id=?', (request['id'],)).fetchone()
            if row:
                if row[0] != signature:
                    raise CoreError('REQUEST_ID_REUSED','This request ID has different arguments.','Use a new request ID.')
                return json.loads(row[1])
    preview = preview_upgrade(catalog)
    if not preview['required'] or plan_token != preview['plan_token']:
        raise CoreError('UPGRADE_PREVIEW_STALE','The upgrade preview is no longer current.','Review a fresh project upgrade --preview.')
    if preview['busy']:
        raise CoreError('PROJECT_BUSY','Project operations are pending.','Wait for them before upgrading.',True)
    directory = catalog.store/'schema-backups'
    if directory.is_symlink():
        raise CoreError('STORE_UNAVAILABLE','The backup directory is a symlink.','Preserve it and choose an intact store.')
    directory.mkdir(mode=0o700,exist_ok=True)
    backup_directory=directory/request['id']
    backup_directory.mkdir(mode=0o700,exist_ok=False)
    backup=backup_directory/'checkpoints.sqlite3'
    fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
    with readonly_database(catalog.database) as source:
        destination=sqlite3.connect(backup)
        try: source.backup(destination)
        finally: destination.close()
    with readonly_database(backup) as db:
        if (db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                or db.execute('SELECT schema_version FROM core_metadata WHERE singleton=1').fetchone()[0] != version):
            raise CoreError('DATABASE_CORRUPT','The migration backup failed verification.','Preserve the backup and original catalog.')
        if _token(_snapshot(catalog, db)) != plan_token:
            raise CoreError('UPGRADE_PREVIEW_STALE','The catalog changed before the backup completed.','Review a fresh preview; backup evidence was preserved.')
    with backup.open('rb') as stream:
        os.fsync(stream.fileno())
    ObjectStore._sync_directories((backup_directory,directory,catalog.store))
    target=preview['to_schema']
    result=dict(schema_version=target,backup=str(backup))
    with catalog.connect(write=True) as db:
        if _token(_snapshot(catalog,db))!=plan_token:
            raise CoreError('UPGRADE_PREVIEW_STALE','The catalog changed before migration.','Review a fresh preview; backup evidence was preserved.')
        if version == 1:
            create_schema_v2(db)
            legacy=json.dumps(dict(version=1,protected=[],exclusions=None,origin='legacy_unknown'),sort_keys=True)
            db.execute("INSERT INTO checkpoint_context SELECT id,'source',NULL,NULL,?,1 FROM checkpoints",(legacy,))
            db.execute('UPDATE source_head SET checkpoint_id=? WHERE singleton=1',(preview['snapshot']['source_head'],))
        if version in (1, 2):
            create_schema_v3(db)
        else:
            create_schema_v4(db)
        db.execute('UPDATE core_metadata SET schema_version=? WHERE singleton=1',(target,))
        catalog._event(db,dict(id=None,actor=request['actor']),'project.upgraded','succeeded',dict(from_schema=version,to_schema=target))
        db.execute('INSERT INTO task_requests VALUES (?,?,?,?)',(request['id'],signature,json.dumps(result),catalog._now()))
    catalog.notify()
    return result


def require_checks(catalog) -> None:
    if schema_version(catalog) != 4:
        raise CoreError('PROJECT_UPGRADE_REQUIRED', 'This project needs the configured checks upgrade.',
                        'Review project upgrade --preview before applying its token.')
