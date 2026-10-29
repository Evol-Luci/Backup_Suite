"""Identity-checked project catalog, shared with the checkpoint engine."""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from .protocol import fingerprint

from solace_engine.repository import ProjectRepository
from solace_engine.object_store import ObjectStore
from .errors import CoreError, classify
from .storage import StoragePolicy


@contextmanager
def readonly_database(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CoreError('STORE_UNAVAILABLE', 'The database is unavailable.', 'Reconnect storage or explicitly relink.')
    wal = Path(str(path) + '-wal')
    shm = Path(str(path) + '-shm')
    # A live writer can create/remove the pair between our separate stats.
    # Retry observation only; a persistently orphaned WAL remains an error.
    for attempt in range(11):
        wal_present=wal.exists()
        if not wal_present or shm.exists():
            break
        if attempt==10:
            raise CoreError('STORE_UNAVAILABLE', 'Read-only access would need to create WAL metadata.', 'Inspect the database with the core stopped after a clean shutdown; preserve its WAL.')
        time.sleep(0.005)
    # Offline WAL-format databases otherwise create sidecars even with mode=ro.
    uri = path.resolve().as_uri() + '?mode=ro' + ('' if wal_present else '&immutable=1')
    db = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


class ProjectCatalog:
    def __init__(self, registration, policy=None):
        self.registration = dict(registration)
        self.policy = policy or StoragePolicy()
        self.store = Path(registration['store'])
        self.database = self.store / 'checkpoints.sqlite3'
        self.condition = threading.Condition()

    @classmethod
    def create(cls, registration, policy=None):
        catalog = cls(registration, policy)
        root = Path(registration['root'])
        location = catalog.policy.check(catalog.store, creating=True)
        if root == location.canonical or root.is_relative_to(location.canonical):
            raise CoreError('UNSUPPORTED_STORAGE', 'The store cannot contain the project.', 'Choose a separate store or a nested store.')
        catalog.store.mkdir(mode=0o700, parents=False, exist_ok=False)
        (catalog.store / 'objects').mkdir(mode=0o700)
        repo = ProjectRepository(root, catalog.store)
        with repo._connect() as db:
            db.execute('CREATE TABLE core_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL, project_id TEXT NOT NULL, store_uuid TEXT NOT NULL)')
            db.executescript('''
                CREATE TABLE operations (id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','interrupted')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT, error_json TEXT,
                    params_json TEXT NOT NULL);
                CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                    operation_id TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                    state TEXT NOT NULL, details_json TEXT NOT NULL);
            ''')
            from .schema import create_schema_v2
            from .integration_schema import create_schema_v3
            create_schema_v2(db)
            create_schema_v3(db)
            from .check_schema import create_schema_v4
            create_schema_v4(db)
            from .policy_schema import create_schema_v5
            create_schema_v5(db)
            db.execute('INSERT INTO core_metadata VALUES (1,5,?,?)', (registration['id'], registration['store_uuid']))
        catalog.database.chmod(0o600)
        ObjectStore._sync_directories((catalog.store / 'objects', catalog.store, catalog.store.parent))
        return catalog

    def validate(self, *, require_root=True, require_objects=True, allow_mount_change=False):
        r = self.registration
        root = Path(r['root'])
        if require_root and (root.is_symlink() or not root.is_dir()):
            raise CoreError('PROJECT_UNAVAILABLE', 'The source directory is unavailable.', 'Explicitly relink the source root.')
        if self.store.is_symlink() or not self.store.is_dir():
            raise CoreError('STORE_UNAVAILABLE', 'The configured store is unavailable.', 'Reconnect storage or explicitly relink.')
        location = self.policy.check(self.store)
        if location.mount_id != r['mount_id'] and not allow_mount_change:
            raise CoreError('STORE_UNAVAILABLE', 'The configured store mount changed.', 'Explicitly relink to validate the store on its current mount.')
        if self.store == root or root.is_relative_to(self.store):
            raise CoreError('UNSUPPORTED_STORAGE', 'The store cannot contain its source root.', 'Explicitly relink to a separate source root.')
        try:
            with readonly_database(self.database) as db:
                if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise CoreError('SCHEMA_VERSION_UNSUPPORTED', 'Unsupported checkpoint schema.', 'Use a compatible Solace version; preserve this store.')
                row = db.execute('SELECT schema_version, project_id, store_uuid FROM core_metadata WHERE singleton=1').fetchone()
                if row is None or row[0] not in (1, 2, 3, 4, 5):
                    raise CoreError('SCHEMA_VERSION_UNSUPPORTED', 'Unsupported core schema.', 'Use a compatible Solace version; preserve this store.')
                if row[1:] != (r['id'], r['store_uuid']):
                    raise CoreError('STORE_IDENTITY_MISMATCH', 'This store belongs to a different project.', 'Select the original store; no data has been replaced.')
            objects = self.store / 'objects'
            if require_objects and (objects.is_symlink() or not objects.is_dir()):
                raise CoreError('STORE_UNAVAILABLE', 'The objects directory is unavailable.', 'Preserve the store and inspect it with doctor.')
        except sqlite3.DatabaseError as error:
            raise classify(error) from error
        return location

    def open_existing(self):
        self.validate()
        try:
            return ProjectRepository.open_existing(Path(self.registration['root']), self.store)
        except Exception as error:
            raise classify(error) from error

    @contextmanager
    def connect(self, *, write=False, metadata=False):
        self.validate(require_root=False, require_objects=not metadata)
        if not write:
            # Offline WAL databases may create sidecars even for mode=ro.
            # Inspection must leave the project store untouched.
            with readonly_database(self.database) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA query_only=ON')
                yield db
            return
        db = sqlite3.connect(self.database.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('PRAGMA query_only=ON')
            try:
                yield db
                if write:
                    db.commit()
            except BaseException:
                if write:
                    db.rollback()
                raise
        finally:
            db.close()

    def notify(self):
        with self.condition:
            self.condition.notify_all()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _event(self, db, operation, kind, state, details):
        db.execute('INSERT INTO events (project_id,operation_id,actor,created_at,kind,state,details_json) VALUES (?,?,?,?,?,?,?)',
                   (self.registration['id'], operation['id'], operation['actor'], self._now(), kind, state, json.dumps(details)))

    def find_operation(self, request):
        signature = fingerprint(request['method'], request['params'], request['actor'])
        with self.connect(metadata=True) as db:
            if db.execute('SELECT schema_version FROM core_metadata WHERE singleton=1').fetchone()[0] in (2, 3, 4, 5):
                from .tasks import TaskCatalog
                return TaskCatalog(self).lookup_request(request,db)
            row = db.execute('SELECT id,fingerprint FROM operations WHERE request_id=?', (request['id'],)).fetchone()
        if row:
            if row['fingerprint'] != signature:
                raise CoreError('REQUEST_ID_REUSED', 'This request ID has different arguments.', 'Use the original arguments or a new request ID.')
            return {'operation_id': row['id']}
        return None

    def accept_operation(self, request):
        signature = fingerprint(request['method'], request['params'], request['actor'])
        with self.connect(write=True) as db:
            if db.execute('SELECT schema_version FROM core_metadata WHERE singleton=1').fetchone()[0] in (2, 3, 4, 5):
                from .tasks import TaskCatalog
                cached=TaskCatalog(self).lookup_request(request,db)
                if cached is not None:
                    return cached
            row = db.execute('SELECT id,fingerprint FROM operations WHERE request_id=?', (request['id'],)).fetchone()
            if row:
                if row['fingerprint'] != signature:
                    raise CoreError('REQUEST_ID_REUSED', 'This request ID has different arguments.', 'Use the original arguments or a new request ID.')
                return {'operation_id': row['id']}
            operation = dict(id=uuid.uuid4().hex, actor=request['actor'])
            now = self._now()
            # Messages stay in checkpoint records, never in operation/event details.
            params = {k:v for k,v in request['params'].items() if k in ('checkpoint','destination','exclusions')}
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (operation['id'],request['id'],signature,request['method'],request['actor'],'queued',now,now,None,None,json.dumps(params)))
            self._event(db, operation, 'operation.queued', 'queued', {})
        self.notify()
        return {'operation_id': operation['id']}

    @staticmethod
    def _operation(row):
        value = dict(row)
        for source, target in [('result_json','result'),('error_json','error'),('params_json','params')]:
            encoded = value.pop(source)
            value[target] = json.loads(encoded) if encoded is not None else None
        value.pop('fingerprint', None)
        from .observations import operation_view
        return operation_view(value)

    def get_operation(self, operation_id):
        with self.connect(metadata=True) as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if row is None:
            raise CoreError('INVALID_REQUEST', 'The operation does not exist.', 'Use an operation ID returned by this project.')
        return self._operation(row)

    def _transition(self, db, operation_id, state, result=None, error=None, *, kind=None):
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row  # Engine hook connections use tuple rows.
        row = cursor.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if row is None:
            raise CoreError('INVALID_REQUEST', 'Unknown operation.', 'Use an existing operation ID.')
        allowed = {'queued': {'running','interrupted','failed'}, 'running': {'succeeded','failed','interrupted'}}
        if state not in allowed.get(row['state'], set()):
            raise CoreError('INVALID_REQUEST', 'Invalid operation state transition.', 'Inspect the existing operation result.')
        db.execute('UPDATE operations SET state=?,updated_at=?,result_json=?,error_json=? WHERE id=?',
                   (state,self._now(),json.dumps(result) if result is not None else None,
                    json.dumps(error) if error is not None else None,operation_id))
        if state in ('succeeded','failed','interrupted') and db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (3, 4, 5):
            # A visible terminal receipt releases ordinary reader ownership in
            # this same commit. Journals and retained exports keep their claims.
            db.execute("DELETE FROM integration_claims WHERE operation_id=? AND kind='target' "
                       'AND NOT EXISTS (SELECT 1 FROM integration_journals WHERE operation_id=?)',
                       (operation_id,operation_id))
        details = {k:v for k,v in (result or {}).items() if k in ('checkpoint_id','file_count','logical_bytes','task_id','workspace_id','revision')}
        if error:
            details['error_code'] = error['code']
        self._event(db, dict(row), kind or 'operation.' + state, state, details)

    def transition(self, operation_id, state, result=None, error=None):
        with self.connect(write=True) as db:
            self._transition(db, operation_id, state, result, error)
        self.notify()
        return self.get_operation(operation_id)

    def complete_checkpoint(self, db, operation_id, checkpoint):
        self.validate()
        self._transition(db, operation_id, 'succeeded',
                         dict(checkpoint_id=checkpoint.id, file_count=checkpoint.file_count, logical_bytes=checkpoint.logical_bytes),
                         kind='checkpoint.created')

    def events_after(self, cursor, limit=256):
        with self.connect() as db:
            maximum = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            if type(cursor) is not int or cursor < 0 or cursor > maximum:
                raise CoreError('CURSOR_INVALID', 'The event cursor is outside retained history.', 'Start at cursor 0 or your last delivered sequence.')
            rows = db.execute('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?', (cursor,limit)).fetchall()
        events = []
        for row in rows:
            value = dict(row)
            value['details'] = json.loads(value.pop('details_json'))
            events.append(value)
        return events

    def has_pending(self):
        with self.connect(metadata=True) as db:
            return db.execute("SELECT 1 FROM operations WHERE state IN ('queued','running') LIMIT 1").fetchone() is not None

    def recover_interrupted(self):
        with self.connect(write=True, metadata=True) as db:
            ids = [r[0] for r in db.execute("SELECT id FROM operations WHERE state IN ('queued','running')")]
            if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (4, 5):
                from .check_runs import CheckRuns
                CheckRuns.recover(db)
            for operation in ids:
                if db.execute('SELECT state FROM operations WHERE id=?',(operation,)).fetchone()[0] == 'interrupted':
                    continue
                error = CoreError('OPERATION_INTERRUPTED', 'The core stopped before a committed result.', 'Inspect the operation and any restore destination before starting new work.').as_dict()
                self._transition(db, operation, 'interrupted', error=error)
                if db.execute('SELECT schema_version FROM core_metadata WHERE singleton=1').fetchone()[0] in (2, 3, 4, 5):
                    intent=db.execute("SELECT phase FROM workspace_intents WHERE operation_id=? OR recovery_operation_id=?",(operation,operation)).fetchone()
                    journal=None
                    if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (3, 4, 5):
                        journal=db.execute("SELECT phase FROM integration_journals WHERE operation_id=? AND phase NOT IN ('committed','restored','abandoned')",(operation,)).fetchone()
                        if journal:
                            db.execute("UPDATE integration_plans SET state='recovery_required' WHERE id=(SELECT plan_id FROM integration_journals WHERE operation_id=?)",(operation,))
                            if journal[0] != 'reserved':
                                db.execute("UPDATE integration_journals SET phase='recovery_required' WHERE operation_id=?",(operation,))
                        else:
                            # Read reservations own no displaced files and are safe to release.
                            db.execute("DELETE FROM integration_claims WHERE operation_id=? AND kind='target'",(operation,))
                    export = db.execute("SELECT 1 FROM integration_exports WHERE operation_id=? AND phase!='published'",(operation,)).fetchone() if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (3, 4, 5) else None
                    if not journal and not export and (intent is None or intent[0] in ('committed','abandoned')):
                        db.execute('DELETE FROM task_claims WHERE operation_id=?',(operation,))
            if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (3, 4, 5):
                from .integration_recovery import IntegrationRecovery
                IntegrationRecovery.classify_interrupted(db)
                # Also cover a stop after success commit but before reader cleanup.
                db.execute("DELETE FROM integration_claims WHERE kind='target' "
                           "AND operation_id IN (SELECT id FROM operations WHERE state NOT IN ('queued','running')) "
                           'AND NOT EXISTS (SELECT 1 FROM integration_journals j WHERE j.operation_id=integration_claims.operation_id)')
        if ids:
            self.notify()
        return ids
