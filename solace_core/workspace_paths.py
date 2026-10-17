"""One configuration and identity-checked, descriptor-held workspace paths."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
import os
import stat
import sqlite3
from .errors import CoreError,invalid


def overlap(a,b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def git_control_marker(path):
    """Empty .git directories do not define repositories; ambiguous markers fail closed."""
    try:
        metadata=path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):return True
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            with os.scandir(fd) as entries:return next(entries,None) is not None
        finally:os.close(fd)
    except FileNotFoundError:return False
    except OSError:return True


@dataclass(frozen=True)
class CoreConfig:
    sync_roots: tuple[Path,...]
    workspace_root: Path

    @classmethod
    def load(cls,data_root):
        data_root=Path(data_root)
        if not data_root.is_absolute(): raise invalid('XDG_DATA_HOME must be absolute.')
        path=data_root/'solace/core-config.json'
        value={}
        if path.exists():
            try:
                value=json.loads(path.read_text())
                if not isinstance(value,dict) or set(value)-{'sync_roots','workspace_root'}: raise ValueError()
                roots=value.get('sync_roots',[])
                if not isinstance(roots,list): raise ValueError()
                for root in roots+[value.get('workspace_root',str(data_root/'solace/workspaces'))]:
                    if not isinstance(root,str) or not Path(root).is_absolute() or '\0' in root: raise ValueError()
            except (OSError,ValueError,TypeError) as error:
                raise invalid('Core config accepts absolute sync_roots and workspace_root paths only.') from error
        conventional=[Path.home()/name for name in ('Dropbox','Google Drive','Nextcloud')]
        return cls(tuple(p.resolve() for p in conventional+[Path(p) for p in value.get('sync_roots',[])]),
                   Path(value.get('workspace_root',str(data_root/'solace/workspaces'))).resolve())


def directory_identity(path,policy):
    path=Path(path)
    try:
        if path.is_symlink() or not path.is_dir(): raise OSError('directory unavailable')
        location=policy.check(path)
        meta=path.stat()
        return dict(path=str(location.canonical),mount_id=location.mount_id,device=meta.st_dev,inode=meta.st_ino)
    except OSError as error:
        raise CoreError('WORKSPACE_UNAVAILABLE','The workspace directory is unavailable.','Reconnect it or explicitly relink; no replacement was created.') from error


class WorkspacePaths:
    def __init__(self,registry,config,policy):
        self.registry,self.config,self.policy=registry,config,policy

    def validate(self,path,*,expected=None,creating=False):
        path=Path(path)
        if not path.is_absolute(): raise invalid('Workspace paths must be absolute.')
        if path.is_symlink():
            raise CoreError('WORKSPACE_UNAVAILABLE','A workspace path cannot be a symlink.','Select an existing real directory.')
        canonical=path.resolve()
        if any(canonical.is_relative_to(Path(root).resolve()) for root in self.config.sync_roots):
            raise CoreError('UNSUPPORTED_STORAGE','Synchronized workspaces are unsupported.','Choose unsynchronized local storage.')
        self.policy.check(canonical,creating=creating)
        for project in self.registry.known_registrations():
            for key in ('root','store'):
                if overlap(canonical,Path(project[key])):
                    raise CoreError('UNSUPPORTED_STORAGE','The workspace overlaps a registered source or store.','Choose a separate unsynchronized local workspace root.')
        for ancestor in (canonical,*canonical.parents):
            if git_control_marker(ancestor/'.git'):
                raise CoreError('UNSUPPORTED_STORAGE','The workspace is inside a Git-controlled tree.','Choose a workspace root outside Git working trees.')
        if creating:
            return dict(path=str(canonical))
        identity=directory_identity(canonical,self.policy)
        if expected and any(identity[k]!=expected[k] for k in ('path','mount_id','device','inode')):
            raise CoreError('WORKSPACE_IDENTITY_MISMATCH','The workspace location or identity changed.','Pause writers and explicitly relink it.')
        return identity

    def reserve(self,project_id,task_id,operation_id):
        root=self.config.workspace_root
        self.validate(root,creating=True)
        final=root/project_id/task_id
        self.validate(final,creating=True)
        self.assert_unowned(final)
        if os.path.lexists(final): raise FileExistsError(str(final))
        return dict(final_path=str(final),staging_path=str(final.parent/('.staging-'+operation_id)))

    @contextmanager
    def open_checked(self,path,expected):
        path=Path(path)
        self.validate(path,expected=expected)
        descriptors=[];identities=[];current=Path('/')
        try:
            try:
                fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);descriptors.append(fd);identities.append((current,os.fstat(fd)))
                for name in path.parts[1:]:
                    fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    current=current/name;descriptors.append(fd);identities.append((current,os.fstat(fd)))
            except OSError as error:
                raise CoreError('WORKSPACE_UNAVAILABLE','A workspace ancestor is unavailable.','Preserve its files and inspect recovery.') from error
            yield fd
            try:
                for named,identity in identities:
                    if named.is_symlink() or not os.path.samestat(identity,named.stat()):
                        raise CoreError('WORKSPACE_IDENTITY_MISMATCH','A workspace ancestor changed during the operation.','Preserve the preparation and inspect its recovery record.')
                self.validate(path,expected=expected)
            except OSError as error:
                raise CoreError('WORKSPACE_UNAVAILABLE','A workspace ancestor changed.','Preserve its files and inspect recovery.') from error
        finally:
            for fd in reversed(descriptors): os.close(fd)

    def ensure_parent(self,final):
        """Create only validated Solace parents via nofollow directory descriptors."""
        final=Path(final);self.validate(final,creating=True)
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
        try:
            for name in final.parent.parts[1:]:
                try: os.mkdir(name,mode=0o700,dir_fd=fd)
                except FileExistsError: pass
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.fsync(fd)  # Persist the entry in the directory that received mkdir.
                os.close(fd);fd=child
            os.fsync(fd)
        finally: os.close(fd)
        return self.validate(final.parent)

    def managed_locations(self,project_id=None,candidate=None,*,include_read_claims=True):
        """Read authoritative retained catalogs; fail closed if ownership is unknown."""
        from .catalog import ProjectCatalog,readonly_database
        locations=[]
        for record in self.registry.known_registrations():
            if candidate is not None and record['id']==project_id: record=candidate
            catalog=ProjectCatalog(record,self.policy)
            try:
                catalog.validate(require_root=False)
                with readonly_database(catalog.database) as db:
                    version=db.execute('SELECT schema_version FROM core_metadata').fetchone()[0]
                    if version in (2, 3):
                        locations += [(Path(r[0]),r[1]) for r in db.execute('SELECT path,id FROM workspaces')]
                        for r in db.execute('SELECT staging_path,final_path,workspace_id,phase FROM workspace_intents'):
                            locations += [(Path(path),r[2]) for path in r[:2]
                                          if path and (r[3]!='abandoned' or os.path.lexists(path))]
                        locations += [(Path(r[0]),None) for r in db.execute('SELECT path FROM workspace_caches')]
                    if version == 3:
                        for table,column in (('integration_journals','journal_path'),('integration_exports','path')):
                            locations += [(Path(r[0]),None) for r in db.execute(f'SELECT {column} FROM {table}')]
                        query = 'SELECT path FROM integration_claims'
                        if not include_read_claims: query += " WHERE kind!='target'"
                        locations += [(Path(r[0]),None) for r in db.execute(query)]
            except (CoreError,OSError,sqlite3.DatabaseError) as error:
                raise CoreError('STORE_UNAVAILABLE','A retained catalog is unavailable for placement checks.',
                                'Reconnect its recorded store, or relink it if still registered, before reserving new locations.') from error
            locations.append((Path(record['store']),None))
        return locations

    def active_claims(self,project_id=None,candidate=None):
        from .catalog import ProjectCatalog, readonly_database
        claims=[]
        for record in self.registry.known_registrations():
            if candidate is not None and record['id']==project_id: record=candidate
            catalog=ProjectCatalog(record,self.policy)
            try:
                catalog.validate(require_root=False)
                with readonly_database(catalog.database) as db:
                    if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] == 3:
                        for path,operation,kind,phase in db.execute(
                            'SELECT c.path,c.operation_id,c.kind,j.phase FROM integration_claims c '
                            'LEFT JOIN integration_journals j ON c.operation_id=j.operation_id'):
                            claims.append(dict(path=path,operation_id=operation,kind=kind,phase=phase))
            except (CoreError,OSError,sqlite3.DatabaseError) as error:
                raise CoreError('STORE_UNAVAILABLE','Retained ownership cannot be checked.',
                                'Reconnect the retained store before accessing live trees.') from error
        return claims

    def assert_access(self,path,*,operation_id=None,project_id=None,candidate=None):
        accessed=Path(path).absolute()
        # Match both the recorded spelling and physical spelling. A substituted
        # symlink cannot transfer ownership away from its recorded location.
        for claim in self.active_claims(project_id,candidate):
            if claim['operation_id'] != operation_id and any(overlap(p,Path(claim['path'])) for p in (accessed,accessed.resolve())):
                raise CoreError('INTEGRATION_TARGET_BUSY','An integration still owns this tree.',
                                'Inspect integration recovery before using its files.',True)

    def assert_unowned(self,path,workspace_id=None):
        self.assert_access(Path(path))
        canonical=Path(path).resolve()
        if any(overlap(canonical,owned.resolve()) for owned,owner in self.managed_locations()
               if workspace_id is None or owner!=workspace_id):
            raise CoreError('UNSUPPORTED_STORAGE','The destination overlaps retained Solace data.',
                            'Choose a separate directory owned only by this workspace.')

    def guard_source(self,root,project_id=None,candidate=None):
        root=Path(root).resolve()
        self.assert_access(root,project_id=project_id,candidate=candidate)
        managed=[self.config.workspace_root,self.registry.path.parent/'projects']
        managed += [path for path,owner in self.managed_locations(project_id,candidate)
                    if not (candidate and owner is None and path.is_relative_to(Path(candidate['store'])))]
        if any(overlap(root,path) for path in managed):
            raise CoreError('UNSUPPORTED_STORAGE','The source overlaps Solace workspace or store data.','Choose a separate source directory.')

    def guard_store(self,store,project_id=None,candidate=None):
        store=Path(store).resolve()
        self.assert_access(store,project_id=project_id,candidate=candidate)
        managed=[self.config.workspace_root]
        managed += [path for path,owner in self.managed_locations(project_id,candidate)
                    if not (candidate and owner is None and path.is_relative_to(Path(candidate['store'])))]
        if any(overlap(store,path.resolve()) for path in managed):
            raise CoreError('UNSUPPORTED_STORAGE','The store overlaps retained workspace or store data.',
                            'Choose separate local storage outside all workspaces.')


def protected_excluder(root,profile,owned_roots):
    root=Path(root)
    owned=[Path(path).resolve() for path in owned_roots if not root.is_relative_to(Path(path).resolve())]
    identities=[]
    for path in owned:
        try: identities.append(path.stat())
        except OSError: pass
    def excluded(relative,metadata):
        if any(part in ('.git','.solace_vault') for part in relative.split('/')): return True
        path=root/relative
        if any(path==own or path.is_relative_to(own) for own in owned): return True
        return any(os.path.samestat(metadata,identity) for identity in identities)
    return excluded
