"""Descriptor-held, recoverable per-file publication of an approved candidate.

The apply owner authenticates approval and writer acknowledgement before calling
stage. This internal publisher requires a committed Task 5 reservation. It never
releases claims, commits history, resumes an interrupted action or removes evidence.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable

from solace_engine.repository import ProjectRepository
from .errors import CoreError
from .integration_claims import IntegrationClaims
from .integration_merge import _entry_key
from .integration_snapshots import SnapshotStore, canonical, entry_json, entry_from_json
from .integration_types import Identity, Journal, PreparedPlan
from .task_operations import staging_entries
from .workspace_paths import directory_identity
from .workspace_trees import _rename_noreplace


def _refuse(message):
    raise CoreError('RECOVERY_REQUIRED', message,
                    'Preserve the target and journal; inspect explicit integration recovery.')


def _identity(meta):
    return dict(device=meta.st_dev, inode=meta.st_ino, mode=meta.st_mode)


def _stat(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _match(meta, expected):
    return (meta is None and expected is None or
            meta is not None and expected is not None and
            all(_identity(meta)[key] == expected[key] for key in ('device', 'inode')) and
            ('mode' not in expected or meta.st_mode == expected['mode']))


def _write(fd, data):
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0: raise OSError('short journal write')
        offset += written


class _HeldDirectories:
    """Hold every ancestor and recheck every name-to-descriptor link."""
    def __init__(self):
        self.fds = {Path('/'): os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)}

    def close(self):
        for fd in reversed(list(self.fds.values())): os.close(fd)

    def open(self, path):
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts: _refuse('Invalid journal directory path.')
        if path not in self.fds:
            parent = self.open(path.parent)
            meta = _stat(parent, path.name)
            fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            if not _match(os.fstat(fd), _identity(meta) if meta else None):
                os.close(fd); _refuse('A directory changed during descriptor traversal.')
            self.fds[path] = fd
        return self.fds[path]

    def check(self):
        for path, fd in self.fds.items():
            if path == Path('/'): continue
            if not _match(_stat(self.fds[path.parent], path.name), _identity(os.fstat(fd))):
                _refuse('A held directory or ancestor was replaced.')

    def forget(self, path):
        os.close(self.fds.pop(Path(path)))


class IntegrationPublisher:
    def __init__(self, service, catalog, *, failpoint: Callable[[str], None] | None = None):
        self.service, self.catalog, self.failpoint = service, catalog, failpoint
        self.claims = IntegrationClaims(service, catalog)
        self.plans = self.claims.plans
        self.snapshots = SnapshotStore(catalog)

    def _hit(self, name):
        if self.failpoint is not None: self.failpoint(name)

    @contextmanager
    def _failure(self, operation_id):
        try:
            yield
        except Exception as error:
            # A failed sync/rename may already have changed disk. Retain all claims.
            try:
                with self.catalog.connect(write=True) as db:
                    db.execute("UPDATE integration_journals SET phase=CASE WHEN identity_json IS NULL THEN 'reserved' ELSE 'recovery_required' END WHERE operation_id=? AND phase NOT IN ('committed','restored','abandoned')", (operation_id,))
                    db.execute("UPDATE integration_plans SET state='recovery_required' WHERE id=(SELECT plan_id FROM integration_journals WHERE operation_id=?) AND state NOT IN ('applied','restored','abandoned')", (operation_id,))
            except Exception:
                # Durable intent and unreleased claims still survive a database failure.
                pass
            if isinstance(error, CoreError): raise
            raise CoreError('RECOVERY_REQUIRED', 'Publication stopped with retained journal evidence.',
                            'Preserve all entries and inspect explicit integration recovery.') from error

    def _row(self, operation_id):
        with self.catalog.connect() as db:
            row = db.execute('SELECT * FROM integration_journals WHERE operation_id=?', (operation_id,)).fetchone()
        if row is None: _refuse('Publication has no committed journal reservation.')
        return dict(row)

    def _actions(self, operation_id):
        with self.catalog.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM integration_actions WHERE operation_id=? ORDER BY ordinal', (operation_id,))]

    @staticmethod
    def _record(fd, name, value):
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            _write(descriptor, canonical(value).encode()); os.fsync(descriptor)
        finally: os.close(descriptor)
        os.fsync(fd)

    @staticmethod
    def _read_record(fd, name, value):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(descriptor)
            expected = canonical(value).encode()
            if not stat.S_ISREG(before.st_mode) or before.st_size != len(expected): _refuse('Journal intent is invalid.')
            offset = 0
            while offset < len(expected):
                block = os.read(descriptor, min(65536, len(expected)-offset))
                if not block or block != expected[offset:offset+len(block)]: _refuse('Journal intent changed.')
                offset += len(block)
            if not _match(_stat(fd, name), _identity(before)): _refuse('Journal intent was replaced.')
            after = os.fstat(descriptor)
            if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns): _refuse('Journal intent changed during validation.')
        finally: os.close(descriptor)

    def _content(self, fd, name, entry, expected, *, sync=False):
        meta = _stat(fd, name)
        if not _match(meta, expected): _refuse('An entry identity changed.')
        digest = hashlib.sha256(); size = 0
        if entry.kind == 'symlink':
            if not stat.S_ISLNK(meta.st_mode): _refuse('An expected link changed type.')
            data = os.fsencode(os.readlink(name, dir_fd=fd)); digest.update(data); size = len(data)
            if sync: os.fsync(fd)
        else:
            if not stat.S_ISREG(meta.st_mode): _refuse('An expected regular file changed type.')
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                before = os.fstat(descriptor)
                if not _match(before, expected): _refuse('A regular file was replaced while opening.')
                while block := os.read(descriptor, 65536): digest.update(block); size += len(block)
                if sync: os.fsync(descriptor)
                after = os.fstat(descriptor)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    _refuse('A file changed during validation.')
            finally: os.close(descriptor)
            if stat.S_IMODE(meta.st_mode) != entry.mode: _refuse('A regular file mode changed.')
        if size != entry.size or digest.hexdigest() != entry.file_digest: _refuse('Entry bytes differ from the retained version.')
        if not _match(_stat(fd, name), expected): _refuse('An entry changed after validation.')

    @staticmethod
    def _endpoint(space, name, identity=None, entry=None):
        return dict(space=space, name=name, identity=identity, entry=entry_json(entry))

    def _build(self, held, root, current, candidate, expected):
        old = {e.path:e for e in current}; new = {e.path:e for e in candidate}
        changed = {path for path in old.keys() | new.keys() if _entry_key(old.get(path)) != _entry_key(new.get(path))}
        directories = {}; identities = {}
        for entry in current:
            path = root/entry.path; fd = held.open(path.parent)
            meta = _stat(fd, path.name)
            if meta is None: _refuse('A current entry disappeared before staging.')
            identities[entry.path] = _identity(meta)
            self._content(fd, path.name, entry, identities[entry.path])
        needed = set()
        for entry in candidate:
            parent = Path(entry.path).parent
            while parent != Path('.'):
                needed.add(parent.as_posix()); parent = parent.parent
        for path in list(held.fds):
            if path.is_relative_to(root): directories[path.relative_to(root).as_posix()] = _identity(os.fstat(held.fds[path]))
        removed_dirs = expected['directory_inventories']
        for name, inventory in removed_dirs.items():
            fd = held.open(root/name)
            if not _match(os.fstat(fd), inventory['identity']) or staging_entries(fd) != inventory['entries']:
                _refuse('The complete replacement directory inventory changed.')
            directories[name] = _identity(os.fstat(fd))
        create = []
        for name in sorted(needed, key=lambda p:(p.count('/'), p)):
            path = root/name
            try:
                fd = held.open(path)
            except (FileNotFoundError, NotADirectoryError):
                # Missing parents or a current leaf explicitly scheduled for displacement.
                if name in old and name not in changed: _refuse('A required parent is an unchanged leaf.')
                create.append(name)
            else:
                directories[name] = _identity(os.fstat(fd))
        actions = []
        def append(kind, path, before, after):
            ordinal = len(actions); slot = str(ordinal) if kind in ('move_old', 'publish_new') else None
            if kind == 'move_old': after['name'] = slot
            if kind == 'publish_new': before['name'] = slot
            actions.append(dict(ordinal=ordinal, path=path, kind=kind, old=before, new=after, slot=slot))
        for path in sorted(changed & old.keys(), key=lambda p:(-p.count('/'), p)):
            append('move_old', path, self._endpoint('target', path, identities[path], old[path]), self._endpoint('old', '', identities[path], old[path]))
        for path in sorted(removed_dirs, key=lambda p:(-p.count('/'), p)):
            append('rmdir', path, self._endpoint('target', path, directories[path]), self._endpoint('target', path))
        for path in create:
            append('mkdir', path, self._endpoint('target', path), self._endpoint('target', path))
        for path in sorted(changed & new.keys()):
            append('publish_new', path, self._endpoint('new', '', entry=new[path]), self._endpoint('target', path, entry=new[path]))
        held.check()
        return actions, directories, identities

    def stage(self, operation_id: str, plan: PreparedPlan) -> Journal:
        with self._failure(operation_id):
            row = self._row(operation_id)
            if (row['phase'] != 'reserved' or row['identity_json'] is not None or
                    (row['plan_id'], row['revision']) != (plan.id, plan.revision)):
                _refuse('Only an uncreated, committed reservation may be staged.')
            if json.loads(row['target_identity_json']) != asdict(plan.target.identity): _refuse('The reserved target differs from the plan.')
            intent = json.loads(row['intent_json']); root = Path(plan.target.identity.path); path = Path(row['journal_path'])
            if path != root.parent/('.solace-integration-'+operation_id): _refuse('Invalid sibling journal reservation.')
            self.claims.validate(plan, scan=True)
            self._hit('journal_reserved')
            held = _HeldDirectories()
            try:
                parent = held.open(root.parent); target = held.open(root)
                self._check_location(held, root.parent, intent['parent_identity'])
                self._check_location(held, root, asdict(plan.target.identity))
                if (root == root.parent or os.fstat(parent).st_dev != os.fstat(target).st_dev or
                        intent['parent_identity']['mount_id'] != plan.target.identity.mount_id or
                        not os.fstat(parent).st_mode & 0o222 or not os.access(root.parent, os.W_OK | os.X_OK)):
                    _refuse('The target no longer has a writable same-filesystem sibling.')
                current = self.snapshots.entries(plan.current); candidate = self.snapshots.entries(plan.candidate)
                actions, directories, identities = self._build(held, root, current, candidate, intent['expected'])
                held.check()
                os.mkdir(path.name, mode=0o700, dir_fd=parent)
                journal_fd = held.open(path); os.fsync(journal_fd); os.fsync(parent); held.check()
                identity = Identity(**directory_identity(path, self.catalog.policy))
                if not _match(os.fstat(journal_fd), asdict(identity)): _refuse('The created journal identity changed.')
                with self.catalog.connect(write=True) as db:
                    db.execute('UPDATE integration_journals SET identity_json=? WHERE operation_id=?', (canonical(asdict(identity)), operation_id))
                    db.execute("UPDATE integration_claims SET identity_json=? WHERE operation_id=? AND kind='journal'", (canonical(asdict(identity)), operation_id))
                for name in ('new', 'old'):
                    held.check(); os.mkdir(name, mode=0o700, dir_fd=journal_fd)
                    os.fsync(held.open(path/name)); os.fsync(journal_fd)
                new_fd = held.open(path/'new')
                for action in actions:
                    if action['kind'] != 'publish_new': continue
                    entry = entry_from_json(action['new']['entry']); slot = action['slot']
                    held.check()
                    if entry.kind == 'file':
                        fd = os.open(slot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=new_fd)
                        try:
                            for block in self.snapshots.iter_bytes(entry): _write(fd, block)
                            os.fchmod(fd, entry.mode); os.fsync(fd)
                        finally: os.close(fd)
                    else:
                        os.symlink(os.fsdecode(self.snapshots.read_bytes(entry)), slot, dir_fd=new_fd)
                    os.fsync(new_fd)
                    observed = _identity(_stat(new_fd, slot))
                    self._content(new_fd, slot, entry, observed)
                    action['old']['identity'] = observed; action['new']['identity'] = observed
                held.check()
                intent.update(format=1, operation_id=operation_id, plan_id=plan.id, revision=plan.revision,
                              target=asdict(plan.target.identity), journal=asdict(identity), actions=actions,
                              current_entries=[entry_json(e) for e in current], candidate_entries=[entry_json(e) for e in candidate],
                              directories=directories, current_identities=identities,
                              slots={name:_identity(os.fstat(held.open(path/name))) for name in ('new', 'old')})
                self._record(journal_fd, 'intent.json', intent); held.check()
                with self.catalog.connect(write=True) as db:
                    for action in actions:
                        db.execute('INSERT INTO integration_actions VALUES (?,?,?,?,?,?,?,?,?)',
                                   (operation_id, action['ordinal'], action['path'], action['kind'], 'planned',
                                    canonical(action['old']), canonical(action['new']), action['slot'], None))
                    db.execute("UPDATE integration_journals SET intent_json=?,phase='staged' WHERE operation_id=?", (canonical(intent), operation_id))
                self._hit('journal_staged')
                return Journal(operation_id, plan.id, plan.revision, path, identity, plan.target.identity)
            finally: held.close()

    def _check_location(self, held, path, expected):
        if not _match(os.fstat(held.open(path)), expected) or directory_identity(path, self.catalog.policy) != expected:
            _refuse('A reserved physical location changed.')
        held.check()

    @contextmanager
    def _session(self, journal):
        row = self._row(journal.operation_id)
        if (journal.identity is None or row['identity_json'] is None or
                (row['plan_id'], row['revision'], row['journal_path']) != (journal.plan_id, journal.revision, str(journal.path)) or
                json.loads(row['identity_json']) != asdict(journal.identity) or json.loads(row['target_identity_json']) != asdict(journal.target)):
            _refuse('The supplied journal is not the retained physical publication.')
        intent = json.loads(row['intent_json']); root = Path(journal.target.path)
        held = _HeldDirectories()
        try:
            for path, identity in ((root.parent, intent['parent_identity']), (root, asdict(journal.target)), (journal.path, asdict(journal.identity))):
                self._check_location(held, path, identity)
            for identity in intent['expected']['ancestors']:
                self._check_location(held, Path(identity['path']), identity)
            for name in ('new', 'old'):
                fd = held.open(journal.path/name)
                if not _match(os.fstat(fd), intent['slots'][name]) or stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                    _refuse('A private journal slot directory changed.')
            if stat.S_IMODE(os.fstat(held.open(journal.path)).st_mode) != 0o700: _refuse('The journal is no longer private.')
            self._read_record(held.open(journal.path), 'intent.json', intent)
            yield held, intent, row
        finally: held.close()

    def _directory_state(self, intent, actions):
        directories = dict(intent['directories'])
        for action in actions:
            if action['phase'] == 'done':
                if action['kind'] == 'rmdir': directories.pop(action['path'], None)
                if action['kind'] == 'mkdir': directories[action['path']] = json.loads(action['observed_json'])['destination']
        return directories

    @staticmethod
    def _validate_actions(intent, actions):
        if len(actions) != len(intent['actions']): _refuse('The durable action list changed.')
        for expected, actual in zip(intent['actions'], actions):
            if any(expected[key] != actual[key] for key in ('ordinal', 'path', 'kind', 'slot')):
                _refuse('A durable action differs from the staged intent.')
            for key in ('old', 'new'):
                endpoint = json.loads(actual[key+'_json'])
                endpoint.pop('parent_identity', None)
                if endpoint != expected[key]: _refuse('An action endpoint differs from the staged intent.')

    @staticmethod
    def _check_directories(held, root, directories):
        for name, expected in directories.items():
            if not _match(os.fstat(held.open(root/name)), expected): _refuse('A target directory identity changed.')
        held.check()

    @staticmethod
    def _location(held, journal, endpoint):
        space = endpoint['space']; name = endpoint['name']
        # Values are internal, but reject corrupted records before descriptor traversal.
        if not name or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/')) or '\0' in name:
            _refuse('Invalid recorded action name.')
        if space == 'target': path = Path(journal.target.path)/name
        elif space in ('old', 'new') and '/' not in name: path = journal.path/space/name
        else: _refuse('Invalid recorded action location.')
        return held.open(path.parent), path.name

    def publish(self, journal: Journal) -> None:
        with self._failure(journal.operation_id), self._session(journal) as (held, intent, row):
            if row['phase'] not in ('staged', 'applying'): _refuse('Publication requires staged actions or explicit recovery reconciliation.')
            actions = self._actions(journal.operation_id)
            self._validate_actions(intent, actions)
            if any(action['phase'] != 'planned' for action in actions): _refuse('Interrupted actions require explicit recovery reconciliation.')
            root = Path(journal.target.path); directories = self._directory_state(intent, actions)
            self._check_directories(held, root, directories)
            with self.catalog.connect(write=True) as db:
                db.execute("UPDATE integration_journals SET phase='applying' WHERE operation_id=?", (journal.operation_id,))
                db.execute("UPDATE integration_plans SET state='applying' WHERE id=?", (journal.plan_id,))
            for action in actions:
                ordinal = action['ordinal']; kind = action['kind']
                before = json.loads(action['old_json']); after = json.loads(action['new_json'])
                self._check_directories(held, root, directories)
                source_fd, source_name = self._location(held, journal, before)
                destination_fd, destination_name = self._location(held, journal, after)
                before['parent_identity'] = _identity(os.fstat(source_fd)); after['parent_identity'] = _identity(os.fstat(destination_fd))
                if not _match(_stat(source_fd, source_name), before['identity']): _refuse('The action source identity changed.')
                if kind in ('move_old', 'publish_new'):
                    self._content(source_fd, source_name, entry_from_json(before['entry']), before['identity'])
                    if _stat(destination_fd, destination_name) is not None: _refuse('Another writer occupied the publication name.')
                elif kind == 'rmdir':
                    if os.listdir(held.open(root/action['path'])): _refuse('A replacement directory is no longer empty.')
                record = dict(ordinal=ordinal, path=action['path'], kind=kind, old=before, new=after, slot=action['slot'])
                held.check()
                self._record(held.open(journal.path), f'{ordinal}.intent.json', record)
                with self.catalog.connect(write=True) as db:
                    db.execute("UPDATE integration_actions SET phase='intent',old_json=?,new_json=? WHERE operation_id=? AND ordinal=?",
                               (canonical(before), canonical(after), journal.operation_id, ordinal))
                self._hit('intent:'+str(ordinal)); held.check()
                if not _match(_stat(source_fd, source_name), before['identity']): _refuse('The action source changed after durable intent.')
                if kind in ('move_old', 'publish_new'):
                    _rename_noreplace(source_name, destination_name, source_fd, destination_fd)
                    self._hit(('old_moved:' if kind == 'move_old' else 'new_published:')+str(ordinal))
                elif kind == 'mkdir':
                    os.mkdir(destination_name, mode=0o700, dir_fd=destination_fd)
                    created_fd = held.open(root/action['path']); os.fsync(created_fd)
                    self._hit('dir_changed:'+str(ordinal))
                else:
                    removed = root/action['path']
                    os.rmdir(source_name, dir_fd=source_fd)
                    # Retain removed directory descriptor until it too is synced.
                    os.fsync(held.open(removed)); held.forget(removed)
                    self._hit('dir_changed:'+str(ordinal))
                os.fsync(source_fd); os.fsync(destination_fd)
                self._hit('synced:'+str(ordinal)); held.check()
                observed = _stat(destination_fd, destination_name)
                observed = _identity(observed) if observed is not None else None
                if kind in ('move_old', 'publish_new'):
                    self._content(destination_fd, destination_name, entry_from_json(after['entry']), after['identity'], sync=True)
                    if _stat(source_fd, source_name) is not None: _refuse('Another writer occupied the vacated name.')
                elif kind == 'mkdir':
                    if not _match(os.fstat(created_fd), observed) or observed['mode'] != stat.S_IFDIR | 0o700:
                        _refuse('A newly created directory changed.')
                    directories[action['path']] = observed
                else:
                    if observed is not None: _refuse('A removed directory name was reoccupied.')
                    directories.pop(action['path'], None)
                held.check(); progress = dict(source=None, destination=observed)
                self._record(held.open(journal.path), f'{ordinal}.done.json', dict(record=record, observed=progress))
                with self.catalog.connect(write=True) as db:
                    db.execute("UPDATE integration_actions SET phase='done',observed_json=? WHERE operation_id=? AND ordinal=?",
                               (canonical(progress), journal.operation_id, ordinal))
                self._hit('progress:'+str(ordinal))

    def verify_final(self, journal: Journal) -> None:
        with self._failure(journal.operation_id), self._session(journal) as (held, intent, row):
            if row['phase'] not in ('applying', 'files_ready'): _refuse('Final verification requires completed publication actions.')
            actions = self._actions(journal.operation_id)
            self._validate_actions(intent, actions)
            if any(action['phase'] != 'done' for action in actions): _refuse('Unfinished actions require explicit recovery.')
            root = Path(journal.target.path)
            directories = self._directory_state(intent, actions)
            self._check_directories(held, root, directories)
            identities = dict(intent['current_identities'])
            for action in actions:
                if action['kind'] == 'move_old':
                    identities.pop(action['path'], None)
                    after = json.loads(action['new_json']); fd, name = self._location(held, journal, after)
                    self._content(fd, name, entry_from_json(after['entry']), after['identity'], sync=True)
                if action['kind'] == 'publish_new': identities[action['path']] = json.loads(action['observed_json'])['destination']
            candidate = tuple(entry_from_json(e) for e in intent['candidate_entries'])
            for entry in candidate:
                path = root/entry.path; fd = held.open(path.parent)
                self._content(fd, path.name, entry, identities[entry.path], sync=True)
            profile = json.loads(intent['candidate']['profile_json'])
            boundaries = self.plans._boundaries(journal.operation_id)
            repo = ProjectRepository.open_existing(root, self.catalog.store)
            observed, warnings = repo.inspect_tree(profile['exclusions'], excluded=self.plans._excluder(root, profile, boundaries))
            if warnings or {e.path:_entry_key(e) for e in observed} != {e.path:_entry_key(e) for e in candidate}:
                _refuse('The final target does not match the exact candidate.')
            held.check()
            for fd in held.fds.values(): os.fsync(fd)
            held.check()
            self._hit('files_verified')
            # Revalidate after the full scan and sync boundary, including same-inode edits.
            for entry in candidate:
                path = root/entry.path
                self._content(held.open(path.parent), path.name, entry, identities[entry.path], sync=True)
            held.check()
            with self.catalog.connect(write=True) as db:
                db.execute("UPDATE integration_journals SET phase='files_ready' WHERE operation_id=?", (journal.operation_id,))
