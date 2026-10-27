"""Owned, disposable exact-candidate copies; retained evidence lives separately."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .errors import CoreError, classify
from .integration_review import IntegrationReview, _held_directory
from .integration_snapshots import canonical
from .schema import require_checks
from .workspace_paths import directory_identity
from .workspace_trees import tree_directory


def _unsafe():
    return CoreError('CHECK_STAGING_UNSAFE', 'The private check copy or its ownership changed.',
                     'Preserve the retained run and inspect its staging location before cleanup.')


@dataclass(frozen=True)
class StagedCheck:
    run_id: str
    root: Path
    identity: dict
    manifest_sha256: str
    home: Path
    tmpdir: Path
    evidence: Path


class CheckStaging:
    def __init__(self, service, catalog):
        self.catalog = catalog
        self.review = IntegrationReview(service, catalog)

    def _row(self, run_id):
        require_checks(self.catalog)
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', run_id):
            raise _unsafe()
        with self.catalog.connect() as db:
            row = db.execute('SELECT * FROM check_runs WHERE id=?', (run_id,)).fetchone()
        if row is None: raise _unsafe()
        row = dict(row)
        path = self.catalog.store/'check-runs'/run_id
        if row['staging_path'] != str(path): raise _unsafe()
        return row, path

    def _identity(self, path):
        return directory_identity(path, self.catalog.policy)

    def _check_identity(self, path, identity):
        if self._identity(path) != identity: raise _unsafe()

    def stage(self, run_id, plan):
        row, container = self._row(run_id)
        if (row['state'] != 'reserved' or row['staging_identity_json'] is not None
                or (row['plan_id'], row['revision'], row['candidate_id'], row['manifest_sha256']) !=
                (plan.id, plan.revision, plan.candidate.checkpoint_id, plan.candidate.manifest_sha256)
                or plan.state != 'prepared' or plan.conflicts):
            raise _unsafe()
        entries = self.review.snapshots.entries(plan.candidate)
        evidence = self.catalog.store/'check-evidence'/run_id
        try:
            # The durable run row owns both derived paths before any mkdir.
            with _held_directory(self.catalog.store) as store:
                for name in ('check-runs', 'check-evidence'):
                    try: os.mkdir(name, 0o700, dir_fd=store)
                    except FileExistsError: pass
                os.fsync(store)
                with tree_directory('check-runs', root_fd=store) as runs, tree_directory('check-evidence', root_fd=store) as logs:
                    os.mkdir(run_id, 0o700, dir_fd=runs); os.fsync(runs)
                    os.mkdir(run_id, 0o700, dir_fd=logs); os.fsync(logs)
                    with tree_directory(run_id, root_fd=runs) as owned:
                        for name in ('candidate', 'home', 'tmp'):
                            os.mkdir(name, 0o700, dir_fd=owned)
                        os.fsync(owned)
                        identity = self._identity(container)
                        identity['ancestors'] = [self._identity(p) for p in container.parents]
                        identity['children'] = {name:self._identity(container/name) for name in ('candidate','home','tmp')}
                        identity['evidence'] = self._identity(evidence)
                        with self.catalog.connect(write=True) as db:
                            count = db.execute("UPDATE check_runs SET staging_identity_json=? WHERE id=? AND state='reserved' AND staging_identity_json IS NULL",
                                               (canonical(identity), run_id)).rowcount
                            if count != 1: raise _unsafe()
                        with tree_directory('candidate', root_fd=owned) as tree:
                            self.review._materialize(entries, tree)
            staged = StagedCheck(run_id, container/'candidate', identity['children']['candidate'],
                                 plan.candidate.manifest_sha256, container/'home', container/'tmp', evidence)
            self.verify(staged, plan)
            return staged
        except Exception as error:
            raise classify(error) from error

    def _owned(self, container, identity):
        self._check_identity(container, {k:identity[k] for k in ('path','mount_id','device','inode')})
        for ancestor in identity['ancestors']:
            self._check_identity(Path(ancestor['path']), ancestor)

    def verify(self, stage, plan):
        row, container = self._row(stage.run_id)
        try:
            identity = json.loads(row['staging_identity_json'])
            self._owned(container, identity)
            if (stage.root != container/'candidate' or stage.home != container/'home' or stage.tmpdir != container/'tmp'
                    or stage.evidence != self.catalog.store/'check-evidence'/stage.run_id
                    or stage.manifest_sha256 != plan.candidate.manifest_sha256
                    or row['manifest_sha256'] != plan.candidate.manifest_sha256
                    or row['candidate_id'] != plan.candidate.checkpoint_id
                    or row['plan_id'] != plan.id or row['revision'] != plan.revision): raise _unsafe()
            for name, expected in identity['children'].items(): self._check_identity(container/name, expected)
            self._check_identity(stage.evidence, identity['evidence'])
            expected = {entry.path:entry for entry in self.review.snapshots.entries(plan.candidate)}
            observed = {}
            def scan(fd, prefix=''):
                for name in os.listdir(fd):
                    path = prefix+name; meta = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(meta.st_mode):
                        with tree_directory(name, root_fd=fd) as child: scan(child, path+'/')
                    elif stat.S_ISLNK(meta.st_mode):
                        data = os.readlink(name, dir_fd=fd).encode('utf-8','surrogateescape')
                        observed[path] = ('symlink', None, len(data), hashlib.sha256(data).hexdigest())
                    elif stat.S_ISREG(meta.st_mode):
                        handle = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=fd)
                        try:
                            if not os.path.samestat(meta, os.fstat(handle)) or meta.st_nlink != 1: raise _unsafe()
                            digest = hashlib.sha256(); size = 0
                            while data := os.read(handle, 1024*1024): digest.update(data); size += len(data)
                            observed[path] = ('file', stat.S_IMODE(meta.st_mode), size, digest.hexdigest())
                        finally: os.close(handle)
                    else: raise _unsafe()
            with _held_directory(stage.root) as tree: scan(tree)
            wanted = {path:(e.kind, e.mode if e.kind=='file' else None,e.size,e.file_digest) for path,e in expected.items()}
            if observed != wanted: raise _unsafe()
            self._owned(container, identity)
        except Exception as error:
            if isinstance(error, CoreError): raise
            raise _unsafe() from error

    @staticmethod
    def _remove_contents(directory, device, retained=None):
        """Bind retained children before deletion; never adopt a replaced name."""
        held = {}
        try:
            for name, identity in (retained or {}).items():
                child = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=directory)
                held[name] = child
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (identity['device'], identity['inode']): raise _unsafe()
            for name in os.listdir(directory):
                before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if name in held and not os.path.samestat(before, os.fstat(held[name])): raise _unsafe()
                if stat.S_ISDIR(before.st_mode):
                    child = held.get(name)
                    if child is None:
                        child = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        opened = os.fstat(child)
                        if opened.st_dev != device or not os.path.samestat(before, opened): raise _unsafe()
                        CheckStaging._remove_contents(child, device)
                        if not os.path.samestat(opened, os.stat(name, dir_fd=directory, follow_symlinks=False)):
                            raise _unsafe()
                        os.rmdir(name, dir_fd=directory)
                    finally:
                        if name not in held: os.close(child)
                else:
                    if not os.path.samestat(before, os.stat(name, dir_fd=directory, follow_symlinks=False)):
                        raise _unsafe()
                    os.unlink(name, dir_fd=directory)
            os.fsync(directory)
        finally:
            for child in held.values(): os.close(child)

    def cleanup(self, run_id):
        row, container = self._row(run_id)
        if row['state'] in ('reserved','running'): raise _unsafe()
        if row['cleanup_state'] == 'cleaned': return dict(run_id=run_id, cleanup_state='cleaned')
        state = 'unsafe'
        try:
            identity = json.loads(row['staging_identity_json'])
            with _held_directory(container.parent) as parent:
                self._owned(container, identity)
                for name, expected in identity['children'].items():
                    self._check_identity(container/name, expected)
                # Open once, bind that descriptor to the recorded inode, and
                # delete through it. A later name replacement is never adopted.
                owned = os.open(container.name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=parent)
                try:
                    opened = os.fstat(owned)
                    if (opened.st_dev, opened.st_ino) != (identity['device'], identity['inode']): raise _unsafe()
                    self._remove_contents(owned, opened.st_dev, identity['children'])
                    if not os.path.samestat(opened, os.stat(container.name, dir_fd=parent, follow_symlinks=False)):
                        raise _unsafe()
                    os.rmdir(container.name, dir_fd=parent)
                finally:
                    os.close(owned)
                os.fsync(parent)
                if os.path.lexists(container): raise _unsafe()
            state = 'cleaned'
        except (CoreError, OSError, ValueError, TypeError, KeyError):
            pass
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE check_runs SET cleanup_state=? WHERE id=?', (state,run_id))
        return dict(run_id=run_id, cleanup_state=state)
