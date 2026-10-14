"""Identity-checked project catalog, shared with the checkpoint engine."""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3

from solace_engine.repository import ProjectRepository
from solace_engine.object_store import ObjectStore
from .errors import CoreError, classify
from .storage import StoragePolicy


@contextmanager
def readonly_database(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CoreError('STORE_UNAVAILABLE', 'The database is unavailable.', 'Reconnect storage or explicitly relink.')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
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
            db.execute('INSERT INTO core_metadata VALUES (1,1,?,?)', (registration['id'], registration['store_uuid']))
        catalog.database.chmod(0o600)
        ObjectStore._sync_directories((catalog.store / 'objects', catalog.store, catalog.store.parent))
        return catalog

    def validate(self, *, require_root=True):
        r = self.registration
        root = Path(r['root'])
        if require_root and (root.is_symlink() or not root.is_dir()):
            raise CoreError('PROJECT_UNAVAILABLE', 'The source directory is unavailable.', 'Explicitly relink the source root.')
        if self.store.is_symlink() or not self.store.is_dir():
            raise CoreError('STORE_UNAVAILABLE', 'The configured store is unavailable.', 'Reconnect storage or explicitly relink.')
        location = self.policy.check(self.store)
        if location.mount_id != r['mount_id']:
            raise CoreError('STORE_UNAVAILABLE', 'The configured store mount changed.', 'Explicitly relink to validate the store on its current mount.')
        if self.store == root or root.is_relative_to(self.store):
            raise CoreError('UNSUPPORTED_STORAGE', 'The store cannot contain its source root.', 'Explicitly relink to a separate source root.')
        try:
            with readonly_database(self.database) as db:
                if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise CoreError('SCHEMA_VERSION_UNSUPPORTED', 'Unsupported checkpoint schema.', 'Use a compatible Solace version; preserve this store.')
                row = db.execute('SELECT schema_version, project_id, store_uuid FROM core_metadata WHERE singleton=1').fetchone()
                if row is None or row[0] != 1:
                    raise CoreError('SCHEMA_VERSION_UNSUPPORTED', 'Unsupported core schema.', 'Use a compatible Solace version; preserve this store.')
                if row[1:] != (r['id'], r['store_uuid']):
                    raise CoreError('STORE_IDENTITY_MISMATCH', 'This store belongs to a different project.', 'Select the original store; no data has been replaced.')
            objects = self.store / 'objects'
            if objects.is_symlink() or not objects.is_dir():
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
