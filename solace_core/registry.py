"""User registrations with durable request receipts and creation intents."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3
import threading
import uuid
from .catalog import ProjectCatalog, readonly_database
from .errors import CoreError, classify, invalid
from .protocol import fingerprint
from .storage import StoragePolicy


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Registry:
    def __init__(self, path, policy=None, *, read_only=False):
        self.path = Path(path).absolute()
        self.policy = policy or StoragePolicy()
        self.read_only = read_only
        self.lock = threading.RLock()
        self.projects = self.path.parent / 'projects'
        if read_only:
            with readonly_database(self.path) as db:
                self._version(db)
        elif self.path.exists():
            with readonly_database(self.path) as db:
                self._version(db)
        else:
            self.policy.check(self.path.parent, creating=True)
            if (self.path.parent / 'core.lock').exists() or (self.projects.is_dir() and any(self.projects.iterdir())):
                raise CoreError('STORE_UNAVAILABLE', 'The registry is missing but project stores remain.', 'Preserve the stores and recover the original registry.')
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as db:
                db.executescript('''
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=FULL;
                    CREATE TABLE projects (id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE,
                        store TEXT NOT NULL UNIQUE, store_uuid TEXT NOT NULL UNIQUE,
                        mount_id INTEGER NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE requests (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                        response_json TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE registration_intents (request_id TEXT PRIMARY KEY,
                        fingerprint TEXT NOT NULL, project_id TEXT NOT NULL UNIQUE,
                        root TEXT NOT NULL UNIQUE, store TEXT NOT NULL UNIQUE,
                        store_uuid TEXT NOT NULL, mount_id INTEGER NOT NULL,
                        actor TEXT NOT NULL, created_at TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('preparing','interrupted')));
                    PRAGMA user_version=1;
                ''')
            db.close()
            self.path.chmod(0o600)
        self._identity = self.path.stat()

    @staticmethod
    def _version(db):
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise CoreError('SCHEMA_VERSION_UNSUPPORTED', 'Unsupported registry schema.', 'Use a compatible Solace version; preserve the registry.')

    @contextmanager
    def connect(self, *, write=False):
        if self.path.is_symlink() or not self.path.is_file() or not os.path.samestat(self._identity, self.path.stat()):
            raise CoreError('STORE_UNAVAILABLE', 'The registry is unavailable or replaced.', 'Recover the original registry; no replacement is created.')
        if write and self.read_only:
            raise CoreError('PERMISSION_DENIED', 'This registry handle is read-only.', 'Use the local core for mutations.')
        if not write and self.read_only:
            try:
                with readonly_database(self.path) as db:
                    db.row_factory = sqlite3.Row
                    self._version(db)
                    yield db
            except sqlite3.DatabaseError as error:
                raise classify(error) from error
            return
        try:
            db = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('PRAGMA query_only=ON')
            try:
                self._version(db)
                yield db
                if write:
                    db.commit()
            except BaseException:
                if write:
                    db.rollback()
                raise
            finally:
                db.close()
        except sqlite3.DatabaseError as error:
            raise classify(error) from error

    def _receipt(self, db, request_id, signature):
        row = db.execute('SELECT fingerprint,response_json FROM requests WHERE id=?', (request_id,)).fetchone()
        if row:
            if row[0] != signature:
                raise CoreError('REQUEST_ID_REUSED', 'This request ID was used for different arguments.', 'Use the original arguments or a new request ID.')
            return json.loads(row[1])

    def _save_receipt(self, db, request_id, signature, result):
        db.execute('INSERT INTO requests VALUES (?,?,?,?)', (request_id, signature, json.dumps(result), timestamp()))

    def get(self, project_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
        if not row:
            raise CoreError('PROJECT_NOT_FOUND', 'The project is not registered.', 'List registered projects or explicitly add the source root.')
        return dict(row)

    def retired_registration(self, project_id):
        """Read retained location receipts without restoring a registration."""
        keys = ('id', 'root', 'store', 'store_uuid', 'mount_id', 'created_at')
        with self.connect() as db:
            rows = db.execute('SELECT response_json FROM requests ORDER BY rowid DESC')
            for row in rows:
                value = json.loads(row[0])
                if value.get('id') == project_id and all(key in value for key in keys):
                    return {key: value[key] for key in keys}
        raise CoreError('PROJECT_NOT_FOUND', 'No retained project location is available.',
                        'Use a registered project or preserve its original store for recovery.')

    def _available(self, record):
        result = dict(record)
        try:
            ProjectCatalog(record, self.policy).validate()
            result['available'] = True
        except Exception as error:
            result.update(available=False, error=classify(error).as_dict())
        return result

    def list(self):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM projects ORDER BY created_at,id')]
        return [self._available(r) for r in rows]

    def show(self, project_id):
        return self._available(self.get(project_id))

    def resolve(self, project):
        try:
            return self.get(project)['id']
        except CoreError as error:
            if error.code != 'PROJECT_NOT_FOUND':
                raise
        root = str(Path(project).resolve())
        with self.connect() as db:
            row = db.execute('SELECT id FROM projects WHERE root=?', (root,)).fetchone()
        if row:
            return row[0]
        raise CoreError('PROJECT_NOT_FOUND', 'The project is not registered.', 'Use its exact ID or registered canonical root.')

    def add(self, root, store, request_id, actor):
        root = Path(root).resolve()
        signature = fingerprint('project.add', {'root': str(root), 'store': str(Path(store).resolve()) if store else None}, actor)
        with self.lock:
            with self.connect() as db:
                receipt = self._receipt(db, request_id, signature)
                intent = db.execute('SELECT * FROM registration_intents WHERE request_id=?', (request_id,)).fetchone()
            if receipt is not None:
                return receipt
            if intent:
                if intent['fingerprint'] != signature:
                    raise CoreError('REQUEST_ID_REUSED', 'Request arguments changed.', 'Use a new request ID.')
                raise CoreError('OPERATION_INTERRUPTED', 'Registration was interrupted.', 'Run doctor and preserve its store; restart the core to reconcile.')
            if not root.is_dir():
                raise CoreError('PROJECT_UNAVAILABLE', 'The source directory is unavailable.', 'Choose an existing directory.')
            project_id, store_uuid = uuid.uuid4().hex, uuid.uuid4().hex
            if store is None:
                self.policy.check(self.projects, creating=True)
                self.projects.mkdir(mode=0o700, exist_ok=True)
                store = self.projects / project_id
            store = Path(store).resolve()
            location = self.policy.check(store, creating=True)
            if store == root or root.is_relative_to(store):
                raise CoreError('UNSUPPORTED_STORAGE', 'The store cannot contain the source.', 'Choose separate directories.')
            if store.exists():
                raise CoreError('DESTINATION_EXISTS', 'The store destination already exists.', 'Choose a new store directory; existing stores are preserved.')
            record = dict(id=project_id, root=str(root), store=str(store), store_uuid=store_uuid,
                          mount_id=location.mount_id, created_at=timestamp())
            with self.connect(write=True) as db:
                if db.execute('SELECT 1 FROM projects WHERE root=?', (str(root),)).fetchone():
                    raise CoreError('PROJECT_ALREADY_REGISTERED', 'This source root is already registered.', 'Use the existing project ID.')
                try:
                    db.execute('INSERT INTO registration_intents VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (request_id, signature, project_id, str(root), str(store), store_uuid,
                                location.mount_id, actor, record['created_at'], 'preparing'))
                except sqlite3.IntegrityError as error:
                    raise CoreError('PROJECT_ALREADY_REGISTERED', 'Registration for this location is already pending.', 'Run doctor to inspect the interrupted registration.') from error
            ProjectCatalog.create(record, self.policy)
            self._publish_intent(record, request_id, signature)
            return record

    def _publish_intent(self, record, request_id, signature):
        with self.connect(write=True) as db:
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?)', tuple(record[k] for k in ('id','root','store','store_uuid','mount_id','created_at')))
            self._save_receipt(db, request_id, signature, record)
            db.execute('DELETE FROM registration_intents WHERE request_id=?', (request_id,))

    def reconcile_intents(self):
        results = []
        with self.lock:
            with self.connect() as db:
                intents = [dict(row) for row in db.execute('SELECT * FROM registration_intents')]
            for intent in intents:
                record = {key: intent[key] for key in ('root','store','store_uuid','mount_id','created_at')}
                record['id'] = intent['project_id']
                try:
                    ProjectCatalog(record, self.policy).validate()
                    self._publish_intent(record, intent['request_id'], intent['fingerprint'])
                    results.append(dict(project=record['id'], state='ready'))
                except Exception as error:
                    with self.connect(write=True) as db:
                        db.execute("UPDATE registration_intents SET state='interrupted' WHERE request_id=?", (intent['request_id'],))
                    results.append(dict(project=record['id'], state='interrupted', error=classify(error).as_dict()))
        return results

    def relink(self, project_id, root, store, confirmed_root, request_id, actor):
        root, store = Path(root).resolve(), Path(store).resolve()
        params = dict(project=project_id, root=str(root), store=str(store), confirmed_root=confirmed_root)
        signature = fingerprint('project.relink', params, actor)
        with self.lock:
            with self.connect() as db:
                receipt = self._receipt(db, request_id, signature)
            if receipt is not None:
                return receipt
            if confirmed_root != str(root):
                raise CoreError('CONFIRMATION_REQUIRED', 'Confirm the selected canonical source root.', f'Confirm this source root: {root}')
            record = self.get(project_id)
            location = self.policy.check(store)
            record.update(root=str(root), store=str(store), mount_id=location.mount_id)
            ProjectCatalog(record, self.policy).validate()
            result = dict(record, available=True)
            with self.connect(write=True) as db:
                try:
                    db.execute('UPDATE projects SET root=?,store=?,mount_id=? WHERE id=?', (str(root),str(store),location.mount_id,project_id))
                except sqlite3.IntegrityError as error:
                    raise CoreError('PROJECT_ALREADY_REGISTERED', 'That location belongs to another project.', 'Select the original store and a unique root.') from error
                self._save_receipt(db, request_id, signature, result)
            return result

    def unregister(self, project_id, request_id, actor):
        signature = fingerprint('project.unregister', {'project': project_id}, actor)
        with self.lock:
            with self.connect(write=True) as db:
                receipt = self._receipt(db, request_id, signature)
                if receipt is not None:
                    return receipt
                row = db.execute('SELECT store FROM projects WHERE id=?', (project_id,)).fetchone()
                if row is None:
                    raise CoreError('PROJECT_NOT_FOUND', 'The project is not registered.', 'List registered projects.')
                result = dict(id=project_id, unregistered=True, store=row[0], store_preserved=True)
                db.execute('DELETE FROM projects WHERE id=?', (project_id,))
                self._save_receipt(db, request_id, signature, result)
            return result
