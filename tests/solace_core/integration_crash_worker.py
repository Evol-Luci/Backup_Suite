"""Standalone disposable-test worker; never imported by the production service.

Usage: python integration_crash_worker.py FIXTURE_JSON FAILPOINT MODE
Only this process installs instrumentation. A durable, exact reached-point record
precedes os._exit(91); normal completion without reaching it is a test failure.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

# Script execution sets sys.path to tests/solace_core, not the checkout root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from solace_core.integration_claims import IntegrationClaims
from solace_core.integration_commit import IntegrationCommit
from solace_core.integration_operations import IntegrationOperations
from solace_core.integration_publish import IntegrationPublisher
from solace_core.registry import Registry
from solace_core.service import CoreService
from solace_core.storage import StoragePolicy
from solace_core.workspace_paths import CoreConfig
import solace_core.integration_publish as publication
import solace_core.integration_recovery as recovery


class BoundedOutput:
    """Bound Python diagnostics before the parent's captured pipe, including traceback."""
    def __init__(self, stream, limit=65536):
        self.stream, self.remaining = stream, limit
    def write(self, text):
        data = text.encode('utf-8', errors='replace')[:self.remaining]
        self.remaining -= len(data)
        self.stream.buffer.write(data)
        self.stream.flush()
        return len(text)
    def flush(self):
        self.stream.flush()


def main(fixture_path, requested, mode):
    fixture = json.loads(Path(fixture_path).read_text())
    assert Path(fixture_path).is_absolute()
    assert mode in ('apply', 'resume', 'restore-before')
    request = fixture['requests'][mode]
    assert request['method'] == ('integration.apply' if mode == 'apply' else 'integration.recovery.'+mode)
    real_sync = os.fsync
    seen = []
    current = {'ordinal': None, 'restore': False}
    accepted = {}

    def hit(point, **evidence):
        seen.append(point)
        if point.startswith(('intent:', 'restore_intent:')):
            current.update(ordinal=int(point.split(':')[1]), restore=point.startswith('restore_'))
        if point != requested:
            return
        barrier = Path(fixture['barrier'])
        record = dict(point=point, mode=mode, pid=os.getpid(), seen=seen,
                      operation_id=accepted.get('operation_id'), evidence=evidence)
        fd = os.open(barrier, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        try:
            data = json.dumps(record, sort_keys=True).encode()
            while data:
                data = data[os.write(fd, data):]
            real_sync(fd)
        finally:
            os.close(fd)
        parent = os.open(barrier.parent, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            real_sync(parent)
        finally:
            os.close(parent)
        os._exit(91)

    original_init = IntegrationPublisher.__init__
    def publisher_init(self, service, catalog, **kwargs):
        original_init(self, service, catalog, failpoint=hit)

    original_record = IntegrationPublisher._record
    def record(fd, name, value):
        original_record(fd, name, value)
        # Durable marker exists; SQL progress is still one phase behind.
        if name == 'intent.json':
            hit('stage_marker')
        elif name[0].isdigit():
            ordinal, phase, _ = name.split('.')
            hit('marker_'+phase+':'+ordinal)
        elif name.startswith('restore-') and name.endswith(('.intent.json', '.done.json')):
            hit('restore_marker_'+name.split('.')[-2]+':'+name.rsplit('-',1)[1].split('.')[0])

    original_mkdir = os.mkdir
    def mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if str(path).startswith('.solace-integration-'):
            hit('journal_created')
        return result

    def stage_sync(fd):
        real_sync(fd)
        path = Path(os.readlink('/proc/self/fd/'+str(fd)))
        if path.name.isdecimal() and path.parent.name == 'new' and path.parent.parent.name.startswith('.solace-integration-'):
            hit('stage_file_synced:'+path.name)

    def rename_wrapper(original):
        def rename(source, destination, source_fd, destination_fd=None):
            destination_fd = source_fd if destination_fd is None else destination_fd
            syncs = 0
            def sync(fd):
                nonlocal syncs
                syncs += 1
                assert fd == (source_fd if syncs == 1 else destination_fd)
                try:
                    os.stat(source, dir_fd=source_fd, follow_symlinks=False)
                    source_absent = False
                except FileNotFoundError:
                    source_absent = True
                target = os.stat(destination, dir_fd=destination_fd, follow_symlinks=False)
                prefix = 'restore_' if current['restore'] else ''
                edge = 'removing' if syncs == 1 else 'receiving'
                hit(f"{prefix}rename_before_{edge}_sync:{current['ordinal']}",
                    source_absent=source_absent, destination_inode=target.st_ino,
                    directory_inode=os.fstat(fd).st_ino, sync_number=syncs)
                real_sync(fd)
            # The real renameat2 executes before its first os.fsync. Nothing
            # emulates the mutation or skips a production durability boundary.
            with patch.object(os, 'fsync', sync):
                return original(source, destination, source_fd, destination_fd)
        return rename

    original_reserve = IntegrationClaims.reserve
    def reserve(self, db, operation, plan):
        hit('before_reservation')
        return original_reserve(self, db, operation, plan)

    original_release = IntegrationClaims.release
    def release(self, db, operation):
        result = original_release(self, db, operation)
        if mode == 'restore-before': hit('restore_sql_uncommitted')
        return result

    original_finish = IntegrationCommit.finish
    class SQLBoundary:
        def __init__(self, db): self.db, self.ordinal = db, 0
        def __getattr__(self, name): return getattr(self.db, name)
        def execute(self, sql, args=()):
            result = self.db.execute(sql, args)
            if sql.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
                self.ordinal += 1
                hit('final_sql_statement:'+str(self.ordinal), sql=sql.split()[0])
            return result
    def finish(self, db, journal, operation):
        hit('final_sql_before')
        value = original_finish(self, SQLBoundary(db), journal, operation)
        hit('final_sql_uncommitted')
        return value

    policy = StoragePolicy(mountinfo=fixture['mountinfo'], sync_roots=[])
    registry = Registry(fixture['registry'], policy)
    config = CoreConfig(tuple(Path(p) for p in fixture['sync_roots']), Path(fixture['workspace_root']))
    with ExitStack() as stack:
        service = CoreService(registry, config=config)
        stack.callback(service.close)
        catalog = service.catalog(fixture['project'])
        original_notify = catalog.notify
        def notify():
            operation = catalog.find_operation(request)
            if operation and catalog.get_operation(operation['operation_id'])['state'] == 'succeeded':
                hit('reply_lost')
            original_notify()
        for target, name, replacement in (
            (IntegrationPublisher, '__init__', publisher_init),
            (IntegrationPublisher, '_record', staticmethod(record)),
            (IntegrationClaims, 'reserve', reserve),
            (IntegrationClaims, 'release', release),
            (IntegrationCommit, 'finish', finish),
            (publication, '_rename_noreplace', rename_wrapper(publication._rename_noreplace)),
            (recovery, '_rename_noreplace', rename_wrapper(recovery._rename_noreplace)),
            (catalog, 'notify', notify),
            (os, 'mkdir', mkdir),
            (os, 'fsync', stage_sync),
            (service.coordinator(fixture['project']), 'submit', lambda operation, fn: None),
        ):
            stack.enter_context(patch.object(target, name, replacement))
        operations = IntegrationOperations(service, catalog)
        accepted.update(operations.accept(request))
        operations.run(accepted['operation_id'], request)
        outcome = catalog.get_operation(accepted['operation_id'])
        raise AssertionError(f'Failpoint {requested!r} was not reached; outcome={outcome!r}; seen={seen!r}')


if __name__ == '__main__':
    sys.stdout = sys.stderr = BoundedOutput(sys.stdout)
    main(*sys.argv[1:])
