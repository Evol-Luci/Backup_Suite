"""Verified materialized trees and read-only workspace comparisons."""
import copy
from pathlib import Path
from .tasks import utcnow
from .workspace_paths import protected_excluder


def compare_tree(repo,checkpoint,root,profile):
    root=Path(root)
    inspector=copy.copy(repo)
    inspector.project_root=root
    inspector._project_identity=root.stat()
    expected={entry.path:entry for entry in repo.get_file_entries(checkpoint)}
    entries,warnings=inspector.inspect_tree(profile['exclusions'],excluded=protected_excluder(root,profile,[repo.store_dir]))
    observed={entry.path:entry for entry in entries}
    changes=[]
    for path in sorted(expected.keys() & observed.keys()):
        old,new=expected[path],observed[path];reasons=[]
        if old.kind!=new.kind: reasons.append('type')
        elif old.kind=='symlink':
            if old.file_digest!=new.file_digest: reasons.append('link_target')
        else:
            if (old.file_digest,old.size)!=(new.file_digest,new.size): reasons.append('content')
            if old.mode!=new.mode: reasons.append('mode')
        if reasons: changes.append(dict(path=path,reasons=reasons))
    added=sorted(observed.keys()-expected.keys());removed=sorted(expected.keys()-observed.keys())
    return dict(checkpoint_id=checkpoint,observed_at=utcnow().isoformat(),clean=not (added or removed or changes),
                added=added,removed=removed,changed=changes,logical_bytes=sum(e.size for e in entries),warnings=warnings)

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from .errors import CoreError
from .workspace_paths import directory_identity
from solace_engine.errors import CorruptObject
from solace_engine.repository import ProjectRepository
from solace_engine.object_store import ObjectStore


def validate_workspace_links(entries,targets):
    ProjectRepository._validate_restore_entries(entries)
    for entry in entries:
        if entry.kind!='symlink': continue
        visited=set();stack=entry.path.split('/')[:-1];pending=[entry.path.split('/')[-1]]
        while pending:
            part=pending.pop(0)
            if part in ('','.'): continue
            if part=='..':
                if not stack: _unsafe_link(entry.path)
                stack.pop();continue
            stack.append(part);selected='/'.join(stack)
            if selected in targets:
                if selected in visited: _unsafe_link(entry.path)
                visited.add(selected)
                target=targets[selected].decode('utf-8','surrogateescape')
                if target.startswith('/') or '\0' in target: _unsafe_link(entry.path)
                stack.pop();pending=target.split('/')+pending


def _unsafe_link(path):
    raise CoreError('WORKSPACE_SYMLINK_UNSAFE',f'Unsafe workspace symlink: {path}',
                    'Exclude or replace absolute, escaping or cyclic links before starting a workspace.')


def clone_regular(source_fd,destination_fd):
    try:
        fcntl.ioctl(destination_fd,0x40049409,source_fd)
        return 'reflink'
    except OSError as error:
        if error.errno not in {errno.EOPNOTSUPP,errno.ENOTTY,errno.EXDEV,errno.EINVAL,errno.ENOSYS}: raise
    os.ftruncate(destination_fd,0);os.lseek(destination_fd,0,os.SEEK_SET);os.lseek(source_fd,0,os.SEEK_SET)
    while True:
        data=os.read(source_fd,1024*1024)
        if not data: break
        offset=0
        while offset<len(data): offset+=os.write(destination_fd,data[offset:])
    return 'copy'


def _targets(repo,entries):
    return {entry.path:b''.join(repo.object_store.read_object(d) for d in entry.chunk_digests)
            for entry in entries if entry.kind=='symlink'}


def _manifest_digest(entries,targets):
    rows=[dict(path=e.path,kind=e.kind,mode=e.mode,size=e.size,digest=e.file_digest,
               target=targets[e.path].hex() if e.path in targets else None) for e in entries]
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _verify_tree(repo,checkpoint,path,*,sealed=False):
    expected=repo.get_file_entries(checkpoint)
    inspector=copy.copy(repo);inspector.project_root=Path(path);inspector._project_identity=Path(path).stat()
    observed,warnings=inspector.inspect_tree(include_store=True)
    if warnings: raise CorruptObject('materialized tree contains unsupported entries')
    old={e.path:e for e in expected};new={e.path:e for e in observed}
    if old.keys()!=new.keys(): raise CorruptObject('materialized tree paths differ')
    for name,e in old.items():
        actual=new[name]
        mode=e.mode & ~0o222 if sealed and e.kind=='file' else e.mode
        if (e.kind,e.size,e.file_digest)!=(actual.kind,actual.size,actual.file_digest):
            raise CorruptObject('materialized file content differs')
        if e.kind=='file' and actual.mode!=mode: raise CorruptObject('materialized file mode differs')
    if sealed:
        with tree_directory(path) as fd:
            _sync_tree(fd,check_sealed=True)
    return sum(e.size for e in expected)


@contextmanager
def tree_directory(path,*,create=False,root_fd=None):
    """Hold every traversal to a real directory; callers operate on the fd."""
    path=Path(path)
    if root_fd is None:
        if not path.is_absolute(): raise ValueError('absolute tree root required')
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);parts=path.parts[1:]
    else:
        if path.is_absolute() or '..' in path.parts: raise ValueError('relative tree path required')
        fd=os.dup(root_fd);parts=path.parts
    try:
        for component in parts:
            if create:
                try:
                    os.mkdir(component,mode=0o700,dir_fd=fd);os.fsync(fd)
                except FileExistsError: pass
            child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        yield fd
    finally: os.close(fd)


def _private_directories(path):
    with tree_directory(path,create=True) as fd: os.fsync(fd)


def _sync_tree(fd,*,seal=False,check_sealed=False,reject_hardlinks=False):
    """Never chmod, read or sync a pathname through a mutable ancestor."""
    os.lseek(fd,0,os.SEEK_SET)
    for name in os.listdir(fd):
        meta=os.stat(name,dir_fd=fd,follow_symlinks=False)
        if stat.S_ISDIR(meta.st_mode):
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                if not os.path.samestat(meta,os.fstat(child)): raise CorruptObject('tree directory changed')
                _sync_tree(child,seal=seal,check_sealed=check_sealed,reject_hardlinks=reject_hardlinks)
            finally: os.close(child)
        elif reject_hardlinks and stat.S_ISREG(meta.st_mode) and meta.st_nlink!=1:
            raise CoreError('UNSUPPORTED_STORAGE','A relink candidate contains shared mutable file inodes.',
                            'Copy the candidate into independent files before relinking.')
    if seal: os.fchmod(fd,0o500)
    if check_sealed and stat.S_IMODE(os.fstat(fd).st_mode)!=0o500: raise CorruptObject('cache directory mode changed')
    if not reject_hardlinks and not check_sealed: os.fsync(fd)


def validate_private_files(path):
    with tree_directory(path) as fd: _sync_tree(fd,reject_hardlinks=True)


def _rename_noreplace(source_name,destination_name,parent_fd,destination_fd=None):
    libc=ctypes.CDLL(None,use_errno=True)
    rename=libc.renameat2
    rename.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint)
    rename.restype=ctypes.c_int
    destination_fd=parent_fd if destination_fd is None else destination_fd
    if rename(parent_fd,os.fsencode(source_name),destination_fd,os.fsencode(destination_name),1)!=0:
        value=ctypes.get_errno();raise OSError(value,os.strerror(value))
    os.fsync(parent_fd)
    if destination_fd!=parent_fd: os.fsync(destination_fd)


class BaseCache:
    def __init__(self,catalog,paths):
        self.catalog,self.paths=catalog,paths

    def ensure(self,repo,checkpoint):
        self.catalog.validate(require_root=False)
        entries=repo.get_file_entries(checkpoint);targets=_targets(repo,entries)
        validate_workspace_links(entries,targets)
        if not repo.verify_checkpoint(checkpoint).ok: raise CorruptObject('checkpoint objects failed verification')
        manifest=_manifest_digest(entries,targets)
        with self.catalog.connect() as db:
            row=db.execute('SELECT * FROM workspace_caches WHERE checkpoint_id=?',(checkpoint,)).fetchone()
        if row:
            record=dict(row);identity=json.loads(record['identity_json'])
            try:
                if directory_identity(Path(record['path']),self.catalog.policy)!=identity: raise CorruptObject('cache identity changed')
                if manifest!=record['manifest_sha256']: raise CorruptObject('cache manifest metadata changed')
                _verify_tree(repo,checkpoint,record['path'],sealed=True)
            except (OSError,CoreError,CorruptObject) as error:
                raise CoreError('CACHE_CORRUPT','The immutable workspace base failed verification.',
                                'Preserve the cache and store; it will not be reused or silently rebuilt.') from error
            return dict(cache_id=record['cache_id'],path=record['path'],identity=identity,
                        manifest_sha256=manifest,logical_bytes=record['logical_bytes'])
        cache_id=uuid.uuid4().hex
        parent=self.catalog.store/'workspace-cache'/checkpoint
        _private_directories(parent)
        parent_identity=directory_identity(parent,self.catalog.policy)
        staging_container=parent/('.stage-'+cache_id);staging=staging_container/'tree'
        container=parent/cache_id;final=container/'tree'
        with tree_directory(parent) as parent_fd:
            os.mkdir(staging_container.name,mode=0o700,dir_fd=parent_fd)
            with tree_directory(staging_container.name,root_fd=parent_fd) as container_fd:
                os.mkdir('tree',mode=0o700,dir_fd=container_fd)
                with tree_directory('tree',root_fd=container_fd) as tree_fd:
                    staging_meta=os.fstat(tree_fd)
                    for entry in entries:
                        relative=Path(entry.path)
                        with tree_directory(relative.parent,create=True,root_fd=tree_fd) as entry_fd:
                            if entry.kind=='symlink': os.symlink(targets[entry.path].decode('utf-8','surrogateescape'),relative.name,dir_fd=entry_fd)
                            else:
                                fd=os.open(relative.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=entry_fd)
                                try:
                                    for digest in entry.chunk_digests:
                                        data=repo.object_store.read_object(digest);offset=0
                                        while offset<len(data): offset+=os.write(fd,data[offset:])
                                    os.fchmod(fd,entry.mode & ~0o222);os.fsync(fd)
                                finally: os.close(fd)
                    _sync_tree(tree_fd,seal=True)
                os.fsync(container_fd)
            _verify_tree(repo,checkpoint,staging,sealed=True)
            self.catalog.validate(require_root=False)
            if directory_identity(parent,self.catalog.policy)!=parent_identity: raise CorruptObject('cache parent changed')
            _rename_noreplace(staging_container.name,container.name,parent_fd)
        identity=directory_identity(final,self.catalog.policy)
        if (identity['device'],identity['inode'])!=(staging_meta.st_dev,staging_meta.st_ino):
            raise CorruptObject('published cache identity changed')
        result=dict(cache_id=cache_id,path=str(final),identity=identity,manifest_sha256=manifest,
                    logical_bytes=sum(e.size for e in entries))
        with self.catalog.connect(write=True) as db:
            db.execute('INSERT INTO workspace_caches VALUES (?,?,?,?,?,?,?)',
                (checkpoint,cache_id,str(final),json.dumps(identity),manifest,result['logical_bytes'],self.catalog._now()))
        return result


class WorkspaceBuilder:
    def __init__(self,catalog,paths):
        self.catalog,self.paths=catalog,paths

    def prepare(self,repo,checkpoint,intent):
        cache=BaseCache(self.catalog,self.paths).ensure(repo,checkpoint)
        entries=repo.get_file_entries(checkpoint);targets=_targets(repo,entries)
        staging=Path(intent['staging_path']);final=Path(intent['final_path'])
        expected=json.loads(intent['expected_json'])
        parent=expected['destination_parent']
        if staging.parent!=final.parent or staging.name!='.staging-'+intent['operation_id']:
            raise CoreError('RECOVERY_REQUIRED','The staging reservation is invalid.','Inspect the operation intent.')
        self.paths.validate(final,creating=True)
        with self.paths.open_checked(final.parent,parent) as parent_fd:
            os.mkdir(staging.name,mode=0o700,dir_fd=parent_fd)
            identity=directory_identity(staging,self.catalog.policy)
            with self.catalog.connect(write=True) as db:
                changed=db.execute('UPDATE workspace_intents SET staging_identity_json=? WHERE operation_id=? AND phase=\'reserved\'',(json.dumps(identity),intent['operation_id'])).rowcount
                if changed!=1: raise CoreError('RECOVERY_REQUIRED','The preparation has no matching reserved intent.','Inspect its operation.')
            with tree_directory(staging.name,root_fd=parent_fd) as tree_fd,tree_directory(cache['path']) as cache_fd:
                if (os.fstat(tree_fd).st_dev,os.fstat(tree_fd).st_ino)!=(identity['device'],identity['inode']):
                    raise CorruptObject('staging directory changed')
                if (os.fstat(cache_fd).st_dev,os.fstat(cache_fd).st_ino)!=(cache['identity']['device'],cache['identity']['inode']):
                    raise CorruptObject('cache directory changed')
                for entry in entries:
                    relative=Path(entry.path)
                    with tree_directory(relative.parent,create=True,root_fd=tree_fd) as entry_fd:
                        if entry.kind=='symlink': os.symlink(targets[entry.path].decode('utf-8','surrogateescape'),relative.name,dir_fd=entry_fd)
                        else:
                            with tree_directory(relative.parent,root_fd=cache_fd) as source_parent:
                                source_fd=os.open(relative.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=source_parent)
                            try:
                                source_meta=os.fstat(source_fd)
                                if not stat.S_ISREG(source_meta.st_mode) or source_meta.st_nlink!=1:
                                    raise CorruptObject('cache file is not an independent regular inode')
                                destination_fd=os.open(relative.name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=entry_fd)
                                try:
                                    clone_regular(source_fd,destination_fd)
                                    os.fchmod(destination_fd,entry.mode);os.fsync(destination_fd)
                                finally: os.close(destination_fd)
                            finally: os.close(source_fd)
                _sync_tree(tree_fd)
            _verify_tree(repo,checkpoint,staging)
            with self.catalog.connect(write=True) as db:
                db.execute("UPDATE workspace_intents SET phase='staged' WHERE operation_id=?",(intent['operation_id'],))
        return dict(identity=identity,manifest_sha256=cache['manifest_sha256'],checkpoint_id=checkpoint)

    def publish(self,intent,staged):
        final=Path(intent['final_path']);staging=Path(intent['staging_path'])
        parent=json.loads(intent['expected_json'])['destination_parent']
        self.paths.validate(staging,expected=staged['identity'])
        self.paths.validate(final,creating=True)
        verifier=ProjectRepository.open_existing(staging,self.catalog.store)
        _verify_tree(verifier,staged['checkpoint_id'],staging)
        with self.paths.open_checked(final.parent,parent) as parent_fd:
            # Recheck named parent immediately before publishing through its fd.
            self.paths.validate(final.parent,expected=parent)
            _rename_noreplace(staging.name,final.name,parent_fd)
        identity=directory_identity(final,self.catalog.policy)
        if any(identity[k]!=staged['identity'][k] for k in ('mount_id','device','inode')):
            raise CoreError('WORKSPACE_IDENTITY_MISMATCH','Published workspace identity changed.','Preserve the files and inspect recovery.')
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE workspace_intents SET phase='published',final_identity_json=? WHERE operation_id=?",(json.dumps(identity),intent['operation_id']))
        return identity
