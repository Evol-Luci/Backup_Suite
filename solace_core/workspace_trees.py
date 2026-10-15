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
        for directory,children,files in os.walk(path,followlinks=False):
            if Path(directory).stat().st_mode & 0o777 != 0o500: raise CorruptObject('cache directory mode changed')
    return sum(e.size for e in expected)


def _private_directories(path):
    """Never follow a substituted directory component while creating parents."""
    path=Path(path)
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            try: os.mkdir(component,mode=0o700,dir_fd=fd)
            except FileExistsError: pass
            child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.fsync(fd);os.close(fd);fd=child
        os.fsync(fd)
    finally: os.close(fd)


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
        parent_fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            os.mkdir(staging_container.name,mode=0o700,dir_fd=parent_fd)
            os.mkdir(staging,mode=0o700)
            for entry in entries:
                target=staging.joinpath(*entry.path.split('/'));_private_directories(target.parent)
                if entry.kind=='symlink': os.symlink(targets[entry.path].decode('utf-8','surrogateescape'),target)
                else:
                    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    try:
                        for digest in entry.chunk_digests:
                            data=repo.object_store.read_object(digest);offset=0
                            while offset<len(data): offset+=os.write(fd,data[offset:])
                        os.fchmod(fd,entry.mode);os.fsync(fd)
                    finally: os.close(fd)
            _verify_tree(repo,checkpoint,staging)
            for entry in entries:
                if entry.kind=='file': (staging/entry.path).chmod(entry.mode & ~0o222)
            for directory,children,files in os.walk(staging,topdown=False,followlinks=False):
                Path(directory).chmod(0o500)
                ObjectStore._sync_directories((Path(directory),))
            _verify_tree(repo,checkpoint,staging,sealed=True)
            self.catalog.validate(require_root=False)
            if directory_identity(parent,self.catalog.policy)!=parent_identity: raise CorruptObject('cache parent changed')
            ObjectStore._sync_directories((staging_container,))
            _rename_noreplace(staging_container.name,container.name,parent_fd)
        finally: os.close(parent_fd)
        identity=directory_identity(final,self.catalog.policy)
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
            for entry in entries:
                target=staging/entry.path;_private_directories(target.parent)
                if entry.kind=='symlink': os.symlink(targets[entry.path].decode('utf-8','surrogateescape'),target)
                else:
                    source_fd=os.open(Path(cache['path'])/entry.path,os.O_RDONLY|os.O_NOFOLLOW)
                    try:
                        destination_fd=os.open(target,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                        try:
                            clone_regular(source_fd,destination_fd)
                            os.fchmod(destination_fd,entry.mode);os.fsync(destination_fd)
                        finally: os.close(destination_fd)
                    finally: os.close(source_fd)
            _verify_tree(repo,checkpoint,staging)
            for directory,children,files in os.walk(staging,topdown=False,followlinks=False):
                ObjectStore._sync_directories((Path(directory),))
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
