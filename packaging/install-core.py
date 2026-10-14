#!/usr/bin/env python3
"""Preview by default; explicit installation stages a fixed runtime and backups."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import uuid

SOURCE = Path(__file__).resolve().parent.parent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    value=str(path)
    if not path.is_absolute() or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('Install paths must be absolute and contain no control characters.')
    return path


def systemd_quote(path):
    return '"'+str(path).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def atomic_write(path,data,mode=0o600):
    if path.parent.is_symlink(): raise ValueError('Refusing a symlink install directory.')
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temporary=path.parent/('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def payload():
    result={}
    for package in ('solace_core','solace_engine'):
        for file in sorted((SOURCE/package).glob('*.py')):
            result[str(file.relative_to(SOURCE))]=file.read_bytes()
    return result


def layout():
    home=safe_path(Path(os.environ.get('HOME',str(Path.home()))))
    data=safe_path(Path(os.environ.get('XDG_DATA_HOME') or str(home/'.local/share')))
    config=safe_path(Path(os.environ.get('XDG_CONFIG_HOME') or str(home/'.config')))
    files=payload()
    revision=digest(b''.join(name.encode()+b'\0'+content+b'\0' for name,content in sorted(files.items())))
    runtime=data/'solace/core-runtimes'/revision[:16]
    python=safe_path(Path(sys.executable).resolve())
    units=config/'systemd/user'
    destinations={}
    for name in ('solace-core.socket','solace-core.service'):
        text=(SOURCE/'packaging/systemd'/name).read_text()
        text=text.replace('@PYTHON@',systemd_quote(python)).replace('@RUNTIME@',str(runtime).replace('%','%%'))
        destinations[units/name]=(text.encode(),0o600)
    code=f'import sys; sys.path.insert(0, {str(runtime)!r}); from solace_core.cli import main; raise SystemExit(main())'
    launcher='#!/bin/sh\nexec '+shlex.quote(str(python))+' -B -c '+shlex.quote(code)+' "$@"\n'
    destinations[home/'.local/bin/solace']=(launcher.encode(),0o700)
    return data,runtime,files,destinations


def verify_runtime(runtime,files):
    if runtime.is_symlink() or not (runtime/'manifest.json').is_file():
        raise ValueError('Refusing an unknown existing runtime directory.')
    manifest=json.loads((runtime/'manifest.json').read_text())
    expected={name:digest(content) for name,content in files.items()}
    if manifest != expected: raise ValueError('Existing runtime manifest does not match this build.')
    actual={str(p.relative_to(runtime)) for p in runtime.rglob('*') if p.is_file()}
    if actual != set(files)|{'manifest.json'}: raise ValueError('Existing runtime has unrecognized files.')
    for name,checksum in expected.items():
        path=runtime/name
        if path.is_symlink() or digest(path.read_bytes())!=checksum:
            raise ValueError('Existing runtime content changed; preserve it for review.')


def install(data,runtime,files,destinations):
    runtime.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if runtime.exists():
        verify_runtime(runtime,files)
    else:
        stage=Path(tempfile.mkdtemp(prefix='.stage-',dir=runtime.parent))
        for name,content in files.items(): atomic_write(stage/name,content,0o444)
        atomic_write(stage/'manifest.json',json.dumps({n:digest(c) for n,c in files.items()},sort_keys=True).encode(),0o444)
        for directory in sorted((p for p in stage.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
            directory.chmod(0o555)
        stage.chmod(0o555)
        os.rename(stage,runtime)
    changes=[]
    for path,(content,mode) in destinations.items():
        if path.is_symlink(): raise ValueError('Refusing to replace a symlink launcher or unit.')
        if not path.is_file() or path.read_bytes()!=content or path.stat().st_mode & 0o777 != mode:
            changes.append((path,content,mode))
    if not changes: return None
    backup=data/'solace/core-install-backups'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700,parents=True)
    manifest=[]
    for index,(path,content,mode) in enumerate(changes):
        previous=None
        if path.exists():
            previous=str(index)+'.previous';shutil.copy2(path,backup/previous)
        manifest.append(dict(target=str(path),previous=previous,installed_digest=digest(content)))
    atomic_write(backup/'manifest.json',json.dumps(manifest,indent=2).encode())
    try:
        for path,content,mode in changes: atomic_write(path,content,mode)
    except BaseException:
        rollback(backup, emit=False)
        raise
    return backup


def rollback(backup, *, emit=True):
    records=json.loads((backup/'manifest.json').read_text())
    applied=[]
    for record in records:
        path=Path(record['target'])
        if path.is_symlink(): raise ValueError('Installed file changed; refusing rollback.')
        if not path.exists():
            if record['previous'] is None: continue
            raise ValueError('A previous file disappeared; preserve the backup for review.')
        current=digest(path.read_bytes())
        if current==record['installed_digest']:
            applied.append(record)
        elif record['previous'] and current==digest((backup/record['previous']).read_bytes()):
            continue  # An installation failure never replaced this target.
        else:
            raise ValueError('Installed file changed; refusing to overwrite it during rollback.')
    for record in applied:
        path=Path(record['target'])
        if record['previous']:
            prior=backup/record['previous']
            atomic_write(path,prior.read_bytes(),prior.stat().st_mode & 0o777)
        else:
            path.unlink()
    if emit:
        print(json.dumps({'rolled_back':str(backup),'stores_preserved':True,'next_step':'Run systemctl --user daemon-reload; restore your previous activation settings.'}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--preview',action='store_true');group.add_argument('--apply',action='store_true');group.add_argument('--rollback',type=Path)
    parser.add_argument('--preview-dir',type=Path)
    args=parser.parse_args()
    if args.rollback: rollback(args.rollback);return
    data,runtime,files,destinations=layout()
    backup=install(data,runtime,files,destinations) if args.apply else None
    if args.preview_dir:
        preview=safe_path(args.preview_dir.absolute());preview.mkdir(mode=0o700,parents=True,exist_ok=True)
        for path,(content,mode) in destinations.items(): atomic_write(preview/path.name,content,mode)
    print(json.dumps(dict(mode='installed' if args.apply else 'preview',runtime=str(runtime),
          files=[str(path) for path in destinations],runtime_files=list(files),backup=str(backup) if backup else None,
          activation_commands=['systemctl --user daemon-reload','systemctl --user enable --now solace-core.socket'],
          rollback_command=shlex.join([sys.executable,str(SOURCE/'packaging/install-core.py'),'--rollback',str(backup)]) if backup else None),indent=2))


if __name__=='__main__':
    try: main()
    except (OSError,ValueError) as error:
        print(str(error),file=sys.stderr);sys.exit(1)
