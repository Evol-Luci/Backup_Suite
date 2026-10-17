"""Immutable review revisions, bounded version views and retained separate exports."""
import base64
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from solace_engine.models import FileEntry
from .errors import CoreError, classify, invalid
from .integration_plans import IntegrationPlans
from .integration_snapshots import canonical
from .integration_types import MergeOutcome
from .observations import encoded_size, SUMMARY_BYTES, PAGE_BYTES
from .schema import require_integrations
from .task_operations import rowdict
from .workspace_paths import directory_identity, overlap
from .workspace_trees import tree_directory, _rename_noreplace

_CONTENT_BYTES = 48 * 1024
_VERSIONS = ('base', 'current', 'incoming', 'candidate')


def review_token(plan, expected):
    """Bind the exact retained versions and sorted target/ownership expectations."""
    value = dict(plan=plan.id, revision=plan.revision, expected=expected,
                 **{name: asdict(getattr(plan, name)) for name in _VERSIONS})
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _bounded(value, ceiling):
    if encoded_size(value) > ceiling:
        raise CoreError('RESPONSE_TOO_LARGE', 'One review record exceeds the response byte limit.',
                        'Use integration export to inspect the complete retained versions.')
    return value


def _path(value):
    if (not isinstance(value, str) or not value or '\0' in value or value.startswith('/')
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise invalid('Paths must be nonempty engine-relative names with no dot components.')
    return value


def _absolute(value):
    if not isinstance(value, str) or '\0' in value or not value.startswith('/') or '..' in value.split('/'):
        raise invalid('An absolute path without dot-dot components is required.')
    return Path(value)


def _integer(value, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise invalid('The integer argument is outside its permitted range.')
    return value


@contextmanager
def _held_directory(path):
    """Hold and recheck every named absolute ancestor, including after reading."""
    descriptors = [os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    parts = Path(path).parts[1:]
    try:
        for name in parts:
            descriptors.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        yield descriptors[-1]
        for index, name in enumerate(parts):
            if not os.path.samestat(os.stat(name, dir_fd=descriptors[index], follow_symlinks=False),
                                   os.fstat(descriptors[index+1])):
                raise CoreError('INTEGRATION_STALE', 'A named ancestor changed during review I/O.',
                                'Preserve the files and retry with stable real directories.')
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _file_identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class IntegrationReview:
    def __init__(self, service, catalog):
        self.service, self.catalog = service, catalog
        self.plans = IntegrationPlans(service, catalog)
        self.snapshots = self.plans.snapshots
        self.paths, self.tasks = self.plans.paths, self.plans.tasks

    def _header(self, db, plan_id, revision=None):
        require_integrations(self.catalog)
        plan = rowdict(db, 'SELECT * FROM integration_plans WHERE id=?', (plan_id,))
        if plan is None:
            raise CoreError('INTEGRATION_PLAN_NOT_FOUND', 'The retained plan does not exist.', 'List this project integration plans.')
        if revision is not None:
            _integer(revision, 1)
            if revision != plan['current_revision']:
                raise CoreError('INTEGRATION_PLAN_STALE', 'The plan revision changed since this review.',
                                'Inspect the current revision and restart paging or resolution.')
        row = rowdict(db, 'SELECT * FROM integration_revisions WHERE plan_id=? AND revision=?',
                      (plan_id, plan['current_revision']))
        return plan, row

    def _summary(self, db, plan, row):
        counts = dict(paths=0, conflicts=0)
        for kind, count in db.execute('SELECT kind,COUNT(*) FROM integration_decisions WHERE plan_id=? AND revision=? GROUP BY kind',
                                      (plan['id'], plan['current_revision'])):
            counts['paths'] += count
            if kind == 'conflict':
                counts['conflicts'] += count
        result = dict(plan_id=plan['id'], revision=plan['current_revision'], state=plan['state'],
                      task_id=plan['task_id'], result_id=plan['result_id'], target_key=plan['target_key'],
                      validated_at=row['validated_at'], counts=counts, manifest_sha256=row['manifest_sha256'],
                      checks='not_run_by_solace')
        if plan['state'] == 'prepared':
            result['review_token'] = row['review_token']
        return _bounded(result, SUMMARY_BYTES)

    def show(self, plan_id):
        with self.catalog.connect() as db:
            plan, row = self._header(db, plan_id)
            return self._summary(db, plan, row)

    def diff(self, params):
        with self.catalog.connect() as db:
            plan, revision = self._header(db, params['plan'], params.get('revision'))
            if 'path' in params:
                if 'revision' not in params or params.get('version') not in _VERSIONS or {'after', 'limit'} & params.keys():
                    raise invalid('Content pages require revision and version, without metadata cursors.')
                return self._content(plan, revision, params)
            if {'version', 'offset'} & params.keys():
                raise invalid('Version and offset require explicit content mode with path.')
            after = _integer(params.get('after', 0)); limit = _integer(params.get('limit', 128), 1, 256)
            if after and 'revision' not in params:
                raise invalid('Subsequent metadata pages must pin revision.')
            total = db.execute('SELECT COUNT(*) FROM integration_decisions WHERE plan_id=? AND revision=?',
                               (plan['id'], plan['current_revision'])).fetchone()[0]
            if after > total:
                raise CoreError('CURSOR_INVALID', 'The cursor is beyond this revision.', 'Start at cursor zero.')
            page = dict(revision=plan['current_revision'], entries=[], total=total, next_after=None)
            rows = db.execute('SELECT path,kind,reason,decision_json FROM integration_decisions '
                              'WHERE plan_id=? AND revision=? ORDER BY path LIMIT ? OFFSET ?',
                              (plan['id'], plan['current_revision'], limit, after))
            for row in rows:
                entry = dict(path=row['path'], kind=row['kind'], reason=row['reason'], **json.loads(row['decision_json']))
                page['entries'].append(entry)
                consumed = after + len(page['entries'])
                page['next_after'] = consumed if consumed < total else None
                if encoded_size(page) > PAGE_BYTES:
                    page['entries'].pop()
                    if not page['entries']:
                        _bounded(page | dict(entries=[entry]), PAGE_BYTES)
                    page['next_after'] = after + len(page['entries'])
                    break
            return _bounded(page, PAGE_BYTES)

    def _content(self, plan, revision, params):
        path = _path(params['path']); offset = _integer(params.get('offset', 0))
        retained = self.plans.load(plan['id'], plan['current_revision'])
        # Shared loading verifies profiles, totals and complete link graphs. The
        # original token also authenticates B/C/R digests, which are bound there
        # rather than stored in separate revision columns like the M digest.
        if review_token(retained, json.loads(revision['expected_json'])) != revision['review_token']:
            raise CoreError('INTEGRATION_MANIFEST_INVALID', 'The retained versions no longer match their review binding.',
                            'Preserve the store and inspect the retained plan.')
        version = getattr(retained, params['version'])
        value = next((entry for entry in self.snapshots.entries(version) if entry.path == path), None)
        if value is None:
            raise CoreError('INTEGRATION_PATH_ABSENT', 'The selected path is absent in this version.', 'Choose a version containing the path.')
        if offset > value.size:
            raise invalid('Content offset exceeds the exact entry size.')
        entry = dict(path=value.path, kind=value.kind, mode=value.mode, size=value.size, file_digest=value.file_digest)
        data = bytearray(); position = 0
        try:
            for block in self.snapshots.iter_bytes(value):
                start = max(0, offset-position); end = min(len(block), offset+_CONTENT_BYTES-position)
                if start < end:
                    data.extend(block[start:end])
                position += len(block)
        except Exception as error:
            raise classify(error) from error
        next_offset = offset+len(data)
        result = dict(revision=plan['current_revision'], version=params['version'], path=path, offset=offset,
                      next_offset=next_offset if next_offset < entry['size'] else None,
                      size=entry['size'], digest=entry['file_digest'], entry=entry,
                      data=base64.b64encode(data).decode('ascii'))
        return _bounded(result, PAGE_BYTES)

    def _import(self, path, relative):
        """Read stable bytes twice before importing independently owned CAS chunks."""
        path = _absolute(path)
        try:
            with tempfile.TemporaryFile() as spool, _held_directory(path.parent) as parent:
                before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    raise invalid('Edited-file imports require a regular file.')
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    if _file_identity(before) != _file_identity(os.fstat(fd)):
                        raise invalid('The edited file changed before it was opened.')
                    digest = hashlib.sha256(); size = 0
                    while True:
                        block = os.read(fd, 1024*1024)
                        if not block: break
                        spool.write(block); digest.update(block); size += len(block)
                    os.lseek(fd, 0, os.SEEK_SET); verify = hashlib.sha256()
                    while True:
                        block = os.read(fd, 1024*1024)
                        if not block: break
                        verify.update(block)
                    if (size != before.st_size or digest.digest() != verify.digest()
                            or _file_identity(before) != _file_identity(os.fstat(fd))
                            or _file_identity(before) != _file_identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))):
                        raise CoreError('INTEGRATION_IMPORT_CHANGED', 'The edited file changed while its bytes were read.',
                                        'Stop writes and import the edited file again.')
                finally:
                    os.close(fd)
                # No source inode is modified, including any of its hard links.
                spool.seek(0); chunks = []
                while True:
                    block = spool.read(1024*1024)
                    if not block: break
                    chunks.append(self.snapshots.objects.put_bytes(block))
                if not chunks:
                    chunks.append(self.snapshots.objects.put_bytes(b''))
                return FileEntry(relative, 'file', stat.S_IMODE(before.st_mode) & 0o777,
                                 size, digest.hexdigest(), tuple(chunks))
        except Exception as error:
            raise classify(error) from error

    def resolve(self, request):
        params = request['params']; require_integrations(self.catalog)
        with self.paths.registry.lock, self.plans.coordinator.lock, self.catalog.connect(write=True) as db:
            cached = self.tasks.lookup_request(request, db)
            if cached is not None: return cached
            header, row = self._header(db, params['plan'], params['revision'])
            if header['state'] not in ('conflicted', 'prepared', 'approved'):
                raise CoreError('INTEGRATION_STATE_INVALID', 'Only a retained draft can be resolved.', 'Inspect application or recovery before changing this plan.')
            path = _path(params['path'])
            if ('take' in params) == ('from_file' in params) or ('take' in params and params['take'] not in ('current', 'incoming', 'delete')):
                raise invalid('Select exactly one of current, incoming, delete or an edited regular file.')
            if db.execute("SELECT 1 FROM integration_decisions WHERE plan_id=? AND path=? AND kind='conflict' LIMIT 1", (header['id'], path)).fetchone() is None:
                raise CoreError('INTEGRATION_CONFLICT_NOT_FOUND', 'This path has never been a conflict in the retained plan.', 'Resolve a path reported as conflicted.')
            plan = self.plans.load(header['id'])
            versions = {name: self.snapshots.entries(getattr(plan, name)) for name in _VERSIONS}
            candidate = {entry.path:entry for entry in versions['candidate']}
            affected = {name for name in candidate if name == path or name.startswith(path+'/') or path.startswith(name+'/')}
            if 'from_file' in params:
                selected = (self._import(params['from_file'], path),)
                resolution = dict(take='from_file', digest=selected[0].file_digest, size=selected[0].size)
            else:
                selected = tuple(entry for entry in versions.get(params['take'], ()) if entry.path == path or entry.path.startswith(path+'/'))
                resolution = dict(take=params['take'])
                if params['take'] == 'delete' and not affected:
                    raise invalid('An explicit deletion must select an existing candidate entry or subtree.')
            for name in affected: candidate.pop(name)
            candidate.update((entry.path,entry) for entry in selected)
            entries = tuple(candidate[name] for name in sorted(candidate))
            # Verification includes the full graph, including unchanged symlinks.
            self.snapshots._verify(entries)
            expected = json.loads(row['expected_json'])
            profile = json.loads(row['profile_json'])
            self.plans._protected_delta(Path(plan.target.identity.path), profile, expected['protected_roots'], versions['current'], entries)
            expected['directory_inventories'] = self.plans._inventories(Path(plan.target.identity.path), versions['current'], entries)
            conflicts = tuple(conflict for conflict in plan.conflicts if not (conflict.path == path or conflict.path.startswith(path+'/') or path.startswith(conflict.path+'/')))
            revision = plan.revision+1; now = self.catalog._now()
            version = self.snapshots.insert(db, entries, parent_id=plan.current.checkpoint_id, profile=profile,
                task_id=plan.target.task_id or plan.task_id, purpose='workspace' if plan.target.task_id else 'source')
            revised = replace(plan, revision=revision, candidate=version, conflicts=conflicts, state='conflicted' if conflicts else 'prepared')
            token = review_token(revised, expected)
            db.execute('INSERT INTO integration_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (plan.id,revision,plan.base.checkpoint_id,plan.incoming.checkpoint_id,plan.current.checkpoint_id,
                 version.checkpoint_id,row['previous_receipt_id'],row['profile_json'],canonical(expected),version.manifest_sha256,token,now,now))
            self.plans._decisions(db,plan.id,revision,versions['base'],versions['current'],versions['incoming'],MergeOutcome(entries,conflicts))
            prior = {}
            for decision in db.execute('SELECT path,decision_json FROM integration_decisions WHERE plan_id=? AND revision=?', (plan.id,plan.revision)):
                data = json.loads(decision['decision_json'])
                if 'resolution' in data:
                    prior[decision['path']] = data['resolution']
            # Preserve explicit choices in the complete new decision set.
            for decision in db.execute('SELECT path,decision_json FROM integration_decisions WHERE plan_id=? AND revision=?', (plan.id,revision)).fetchall():
                selected_path = decision['path'] == path or decision['path'].startswith(path+'/') or decision['path'] in affected
                if selected_path or decision['path'] in prior:
                    data = json.loads(decision['decision_json'])
                    data['resolution'] = resolution if selected_path else prior[decision['path']]
                    db.execute("UPDATE integration_decisions SET kind='resolved',reason=NULL,decision_json=? WHERE plan_id=? AND revision=? AND path=?",
                               (canonical(data),plan.id,revision,decision['path']))
            db.execute('INSERT INTO integration_refs VALUES (?,?,?,?)', (version.checkpoint_id,plan.id,'candidate',now))
            db.execute('UPDATE integration_plans SET state=?,current_revision=?,updated_at=? WHERE id=?', (revised.state,revision,now,plan.id))
            new_header, new_row = self._header(db,plan.id)
            result = self._summary(db,new_header,new_row)
            self.tasks.event(db,request,'integration.resolved','succeeded',dict(plan_id=plan.id,revision=revision))
            self.tasks.save_request(db,request,result)
        self.catalog.notify()
        return result

    def _export_location(self, destination, staging):
        for path in (destination, staging):
            self.paths.validate(path, creating=True)
            self.paths.assert_unowned(path)
            roots = self.plans._boundaries()
            if any(overlap(path, Path(root)) for root in roots):
                raise CoreError('UNSUPPORTED_STORAGE', 'The export overlaps retained Solace ownership.', 'Choose a fresh separate export directory.')
            if os.path.lexists(path):
                raise FileExistsError(str(path))

    def _materialize(self, entries, tree_fd):
        for entry in entries:
            relative = Path(entry.path)
            with tree_directory(relative.parent, create=True, root_fd=tree_fd) as parent:
                if entry.kind == 'symlink':
                    data = self.snapshots.read_bytes(entry)
                    os.symlink(data.decode('utf-8', 'surrogateescape'), relative.name, dir_fd=parent)
                else:
                    fd = os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    try:
                        for data in self.snapshots.iter_bytes(entry):
                            remaining = memoryview(data)
                            while remaining:
                                remaining = remaining[os.write(fd, remaining):]
                        os.fchmod(fd, entry.mode); os.fsync(fd)
                    finally:
                        os.close(fd)
                os.fsync(parent)
        os.fsync(tree_fd)

    def export(self, operation_id, request):
        params = request['params']; require_integrations(self.catalog)
        reserved = False
        try:
            with self.paths.registry.lock, self.plans.coordinator.lock:
                with self.catalog.connect() as db:
                    retained = rowdict(db,'SELECT * FROM integration_exports WHERE operation_id=?',(operation_id,))
                    if retained:
                        # Restart and retries inspect retained intent; never resume writes.
                        return _bounded(dict(operation_id=operation_id,plan_id=retained['plan_id'],revision=retained['revision'],
                            path=retained['path'],phase=retained['phase']),SUMMARY_BYTES)
                    self._header(db,params['plan'],params['revision'])
                destination = _absolute(params['destination'])
                staging = destination.parent/('.solace-export-'+operation_id)
                self._export_location(destination, staging)
                # Parent must already exist, be writable and traverse no symlinks.
                with _held_directory(destination.parent) as parent:
                    if not os.access(destination.parent, os.W_OK | os.X_OK) or not os.fstat(parent).st_mode & 0o222:
                        raise CoreError('PERMISSION_DENIED','The export parent is not writable.','Choose a writable local parent directory.')
                    plan = self.plans.load(params['plan'])
                    versions = {name:self.snapshots.entries(getattr(plan,name)) for name in _VERSIONS}
                    with self.catalog.connect(write=True) as db:
                        self._header(db,plan.id,params['revision'])
                        now = self.catalog._now()
                        db.execute('INSERT INTO integration_exports VALUES (?,?,?,?,?,?)', (operation_id,plan.id,plan.revision,str(destination),None,'reserved'))
                        for path in (destination,staging):
                            db.execute('INSERT INTO integration_claims VALUES (?,?,?,?,?)', (str(path),operation_id,'export',None,now))
                        for name in _VERSIONS:
                            db.execute('INSERT OR IGNORE INTO integration_refs VALUES (?,?,?,?)', (getattr(plan,name).checkpoint_id,operation_id,'export',now))
                    reserved = True
                    os.mkdir(staging.name,mode=0o700,dir_fd=parent); os.fsync(parent)
                    identity = directory_identity(staging,self.catalog.policy)
                    with self.catalog.connect(write=True) as db:
                        db.execute('UPDATE integration_exports SET identity_json=? WHERE operation_id=?', (canonical(identity),operation_id))
                        db.execute('UPDATE integration_claims SET identity_json=? WHERE path=?', (canonical(identity),str(staging)))
                    with _held_directory(staging) as tree:
                        for name in _VERSIONS:
                            os.mkdir(name,mode=0o700,dir_fd=tree); os.fsync(tree)
                            with tree_directory(Path(name),root_fd=tree) as subtree:
                                self._materialize(versions[name],subtree)
                        os.fsync(tree)
                    with self.catalog.connect(write=True) as db:
                        db.execute("UPDATE integration_exports SET phase='staged' WHERE operation_id=?", (operation_id,))
                    # The registry lock still excludes concurrent core placement.
                    self.paths.validate(destination,creating=True)
                    if directory_identity(staging,self.catalog.policy) != identity:
                        raise CoreError('INTEGRATION_STALE','The export staging identity changed.','Preserve its retained preparation.')
                    _rename_noreplace(staging.name,destination.name,parent)
                    published = directory_identity(destination,self.catalog.policy)
                    if any(published[key] != identity[key] for key in ('mount_id','device','inode')):
                        raise CoreError('INTEGRATION_STALE','The published export identity changed.','Preserve its retained preparation.')
                    result = _bounded(dict(operation_id=operation_id,plan_id=plan.id,revision=plan.revision,path=str(destination),phase='published'),SUMMARY_BYTES)
                # Ancestor verification above must finish before recording success.
                with self.catalog.connect(write=True) as db:
                    db.execute("UPDATE integration_exports SET phase='published',identity_json=? WHERE operation_id=?", (canonical(published),operation_id))
                    db.execute('UPDATE integration_claims SET identity_json=? WHERE path=?', (canonical(published),str(destination)))
                    self.catalog._transition(db,operation_id,'succeeded',result=result,kind='integration.exported')
            self.catalog.notify()
            return result
        except Exception as error:
            failure = classify(error)
            with self.catalog.connect(write=True) as db:
                if reserved:
                    db.execute("UPDATE integration_exports SET phase='interrupted' WHERE operation_id=?", (operation_id,))
                operation = db.execute('SELECT state FROM operations WHERE id=?',(operation_id,)).fetchone()
                if operation and operation[0] in ('queued','running'):
                    self.catalog._transition(db,operation_id,'failed',error=failure.as_dict())
            self.catalog.notify()
            raise failure from error
