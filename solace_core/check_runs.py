"""Core-owned, replayable execution of an exact retained integration candidate."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
import uuid

from .check_process import CheckExecutor, process_start_time
from .check_profiles import CheckProfiles
from .check_selection import CheckSelection
from .check_staging import CheckStaging
from .errors import CoreError, classify
from .integration_claims import IntegrationClaims
from .integration_plans import IntegrationPlans, _directory
from .integration_snapshots import canonical
from .protocol import fingerprint
from .schema import require_checks
from .tasks import TaskCatalog
from .workspace_paths import directory_identity


_ACTIVE = ('reserved', 'running')
log = logging.getLogger(__name__)


def _error(message):
    return CoreError('CHECK_RUN_INVALID', message, 'Inspect the retained run, task selection and current prepared plan.')


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class CheckRuns:
    def __init__(self, service, catalog):
        self.service, self.catalog = service, catalog
        self.plans = IntegrationPlans(service, catalog)
        self.claims = IntegrationClaims(service, catalog)
        self.tasks = TaskCatalog(catalog)
        self.coordinator = service.coordinator(catalog.registration['id'])
        self.staging = CheckStaging(service, catalog)

    def binding(self, plan, profile):
        if plan.state not in ('prepared','approved') or plan.conflicts:
            raise _error('Check bindings require a prepared or approved, conflict-free plan.')
        validated = self.claims.validate(plan, scan=True)
        with self.catalog.connect() as db:
            selected = CheckSelection.selected(db, plan.task_id)
            active = CheckProfiles.active(db, profile['profile_id'])
            task = self.tasks.row(db, plan.task_id)
            row = db.execute('SELECT profile_json FROM integration_revisions WHERE plan_id=? AND revision=?',
                             (plan.id, plan.revision)).fetchone()
        if not selected or selected['profile_id'] != profile['profile_id']:
            raise _error('The task has not selected this approved check profile.')
        if (active['revision'], active['digest']) != (profile['revision'], profile['digest']):
            raise _error('The active check profile revision changed.')
        return dict(plan_id=plan.id, revision=plan.revision, review_token=validated['review_token'],
            candidate_id=plan.candidate.checkpoint_id, manifest_sha256=plan.candidate.manifest_sha256,
            capture_profile_sha256=_digest(json.loads(row['profile_json'])),
            expected_sha256=_digest(validated['expected']), task_id=plan.task_id,
            task_revision=task['revision'], profile_id=profile['profile_id'],
            profile_revision=profile['revision'], profile_digest=profile['digest'])

    def _lookup(self, request, db):
        cached = self.tasks.lookup_request(request, db)
        if cached is not None and request["method"] == "integration.check.run":
            row = db.execute("SELECT result_json FROM task_requests WHERE request_id=?", (request["id"],)).fetchone()
            if row is not None: return json.loads(row[0])
        return cached

    def start(self, request):
        require_checks(self.catalog)
        with self.service.registry.lock, self.coordinator.lock:
            with self.catalog.connect() as db:
                cached = self._lookup(request, db)
            if cached is not None: return cached
            if self.service._closed: raise _error('The core is shutting down.')
            p = request['params']
            plan = self.plans.load(p['plan'], p['revision'])
            if plan.state != 'prepared' or plan.conflicts:
                raise _error('New checks require a prepared, conflict-free plan.')
            with self.catalog.connect() as db: profile = CheckProfiles.active(db, p['profile'])
            binding = self.binding(plan, profile)
            run_id, operation = uuid.uuid4().hex, uuid.uuid4().hex
            accepted = dict(operation_id=operation, run_id=run_id)
            now = self.catalog._now()
            with self.catalog.connect(write=True) as db:
                # Recheck request and profile under the reservation transaction.
                cached = self._lookup(request, db)
                if cached is not None: return cached
                active = CheckProfiles.active(db, p['profile'])
                if (active['revision'], active['digest']) != (profile['revision'],profile['digest']):
                    raise _error('The active check profile revision changed.')
                row = db.execute('SELECT profile_json,expected_json FROM integration_revisions WHERE plan_id=? AND revision=?',
                                 (plan.id,plan.revision)).fetchone()
                db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (operation, request['id'], fingerprint(request['method'],p,request['actor']), request['method'],
                     request['actor'], 'queued', now, now, None, None, canonical(dict(run_id=run_id))))
                db.execute('INSERT INTO check_runs(id,plan_id,revision,task_id,candidate_id,manifest_sha256,capture_profile_json,expected_json,profile_id,profile_revision,profile_digest,state,staging_path,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (run_id,plan.id,plan.revision,plan.task_id,plan.candidate.checkpoint_id,plan.candidate.manifest_sha256,
                     row['profile_json'],row['expected_json'],profile['profile_id'],profile['revision'],profile['digest'],
                     'reserved',str(self.catalog.store/'check-runs'/run_id),
                     canonical(dict(operation_id=operation,binding=binding,process=None)),now,now))
                self.catalog._event(db,dict(id=operation,actor=request['actor']),'check.reserved','queued',accepted)
                self.tasks.save_request(db, request, accepted)
            with self.service._guard:
                event = threading.Event()
                if self.service._closed: event.set()
                self.service._check_cancellations[(self.catalog.registration['id'], run_id)] = event
            self.coordinator.submit(operation, lambda: self.run(operation, request))
        self.catalog.notify()
        return accepted

    def _row(self, run_id, db):
        row = db.execute('SELECT * FROM check_runs WHERE id=?', (run_id,)).fetchone()
        if row is None: raise _error('This check run does not exist.')
        return dict(row)

    def show(self, run_id):
        require_checks(self.catalog)
        with self.catalog.connect(metadata=True) as db:
            row = self._row(run_id, db)
            steps = [dict(r) for r in db.execute('SELECT * FROM check_steps WHERE run_id=? ORDER BY ordinal', (run_id,))]
        result = json.loads(row.pop('result_json') or '{}')
        row.update(result)
        for key in ('capture_profile_json','expected_json','staging_identity_json'): row.pop(key, None)
        for step in steps:
            step['argv'] = json.loads(step.pop('argv_json'))
            step['details'] = json.loads(step.pop('details_json') or '{}')
        row['steps'] = steps
        return row

    def list(self, plan_id, after=0, limit=128):
        require_checks(self.catalog)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 256:
            raise _error('Use a nonnegative cursor and a page size from 1 through 256.')
        with self.catalog.connect(metadata=True) as db:
            rows = [dict(r) for r in db.execute('SELECT rowid AS cursor,id,state,revision,profile_id,profile_revision,cleanup_state,created_at FROM check_runs WHERE plan_id=? AND rowid>? ORDER BY rowid LIMIT ?',
                                               (plan_id,after,limit+1))]
        return dict(runs=rows[:limit], next_after=rows[limit-1]['cursor'] if len(rows)>limit else None)

    def cancel(self, request):
        require_checks(self.catalog)
        with self.coordinator.lock:
            with self.catalog.connect(write=True) as db:
                cached = self._lookup(request,db)
                if cached is not None: return cached
                row = self._row(request['params']['run'],db)
                result = dict(run_id=row['id'], state='cancelling' if row['state'] in _ACTIVE else row['state'])
                if row['state'] in _ACTIVE:
                    with self.service._guard:
                        event = self.service._check_cancellations.get((self.catalog.registration['id'], row['id']))
                        if event is not None: event.set()
                self.tasks.save_request(db, request, result)
            self.catalog.notify()
            return result

    def _capture(self, stage, ordinal, result):
        # Hold all ancestors; O_EXCL prevents replacement/truncation of retained files.
        with self.catalog.connect() as db:
            identity = json.loads(self._row(stage.run_id,db)['staging_identity_json'])['evidence']
        if directory_identity(stage.evidence,self.catalog.policy) != identity:
            raise _error('The retained evidence directory changed identity.')
        paths = {}
        with _directory(stage.evidence) as fd:
            meta = os.fstat(fd)
            if (meta.st_dev, meta.st_ino) != (identity['device'],identity['inode']):
                raise _error('The retained evidence directory changed identity.')
            for stream in ('stdout','stderr'):
                name = f'{ordinal:02d}.{stream}'
                output = os.open(name, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
                with os.fdopen(output,'wb') as handle:
                    os.fchmod(handle.fileno(),0o600)
                    handle.write(getattr(result,stream)); handle.flush(); os.fsync(handle.fileno())
                paths[stream] = str(stage.evidence/name)
            os.fsync(fd)
        if directory_identity(stage.evidence,self.catalog.policy) != identity:
            raise _error('The retained evidence directory changed identity.')
        return paths

    def run(self, operation_id, request):
        with self.catalog.connect() as db:
            run_id = self._lookup(request, db)['run_id']
        key = (self.catalog.registration['id'],run_id)
        with self.service._guard: cancel = self.service._check_cancellations[key]
        state, error, stage = 'failed', None, None
        try:
            with self.catalog.connect() as db:
                row = self._row(run_id,db)
                if row['state'] not in _ACTIVE: return
                retained = json.loads(row['result_json'])
                profile = CheckProfiles._read(db, CheckProfiles._row(db,row['profile_id']), row['profile_revision'])
            plan = self.plans.load(row['plan_id'],row['revision'])
            if plan.state != 'prepared' or plan.conflicts:
                raise _error('Execution requires a prepared, conflict-free plan.')
            with self.coordinator.lock:
                if self.binding(plan,profile) != retained['binding']:
                    raise _error('The check binding changed before execution.')
            stage = self.staging.stage(run_id, plan)
            with self.catalog.connect(write=True) as db:
                db.execute("UPDATE check_runs SET state='running',updated_at=? WHERE id=?",(self.catalog._now(),run_id))
                self.catalog._transition(db,operation_id,'running')
            executor = CheckExecutor(profile['definition']['environment'])
            state = 'passed'
            for ordinal, step in enumerate(profile['definition']['steps']):
                if cancel.is_set():
                    state = 'cancelled'; break
                def on_start(pid, identity):
                    retained['process'] = dict(pid=pid,start_time=identity,ordinal=ordinal)
                    with self.catalog.connect(write=True) as db:
                        db.execute('UPDATE check_runs SET result_json=?,updated_at=? WHERE id=? AND state=\'running\'',
                                   (canonical(retained),self.catalog._now(),run_id))
                result = executor.execute(step,stage,cancel,on_start)
                paths = self._capture(stage,ordinal,result)
                details = asdict(result)
                for field in ('stdout','stderr'): details.pop(field)
                # Exceptions in on_start leave the durable process marker intact.
                retained['process'] = None
                if not result.output_complete: retained['cleanup_blocked'] = 'output_incomplete'
                with self.catalog.connect(write=True) as db:
                    db.execute('INSERT INTO check_steps(run_id,ordinal,state,argv_json,timeout_seconds,output_cap,started_at,finished_at,exit_code,signal,stdout_sha256,stderr_sha256,stdout_bytes,stderr_bytes,stdout_path,stderr_path,details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (run_id,ordinal,result.status,canonical(step['argv']),step['timeout_seconds'],step['output_limit_bytes'],
                         result.started_at,result.finished_at,result.exit_code,result.signal,result.stdout_sha256,result.stderr_sha256,
                         result.stdout_bytes,result.stderr_bytes,paths['stdout'],paths['stderr'],canonical(details)))
                    db.execute('UPDATE check_runs SET result_json=?,updated_at=? WHERE id=?',
                               (canonical(retained),self.catalog._now(),run_id))
                if result.status != 'passed' or not result.output_complete:
                    state = result.status if result.status in ('failed','cancelled','timed_out') else 'failed'
                    if result.status == 'launch_error': error = _error('The configured check command could not be launched.').as_dict()
                    break
            with self.service.registry.lock, self.coordinator.lock:
                if cancel.is_set(): state = 'cancelled'
                if state == 'passed':
                    try:
                        if self.binding(self.plans.load(row['plan_id'],row['revision']),profile) != retained['binding']:
                            state = 'stale'
                    except CoreError: state = 'stale'
                self._finish(run_id,operation_id,state,error)
        except Exception as exc:
            error = classify(exc).as_dict()
            # A prelaunch stale plan is a stale run, not a command failure.
            state = 'stale' if (error['code'] in ('INTEGRATION_PLAN_STALE','CHECK_PROFILE_RETIRED')
                                or (stage is None and error['code']=='CHECK_RUN_INVALID')) else 'failed'
            self._finish(run_id,operation_id,state,error)
        finally:
            try:
                if stage is not None:
                    with self.catalog.connect() as db: finished = self._row(run_id,db)
                    blocked = json.loads(finished['result_json'] or '{}').get('cleanup_blocked')
                    if finished['state'] not in _ACTIVE and not blocked: self.staging.cleanup(run_id)
            except Exception as cleanup_error:
                log.error('check cleanup retained run=%s exception=%s',run_id,type(cleanup_error).__name__)
            finally:
                with self.service._guard: self.service._check_cancellations.pop(key,None)
                self.catalog.notify()

    def _metadata_matches(self, db, run, binding):
        """The final write transaction authenticates metadata again after the scan."""
        try:
            profile = CheckProfiles.active(db,run['profile_id'])
            selected = CheckSelection.selected(db,run['task_id'])
            task = self.tasks.row(db,run['task_id'])
            header = db.execute('SELECT current_revision,state FROM integration_plans WHERE id=?',
                                (run['plan_id'],)).fetchone()
            revision = db.execute('SELECT * FROM integration_revisions WHERE plan_id=? AND revision=?',
                                  (run['plan_id'],run['revision'])).fetchone()
            fresh = dict(plan_id=run['plan_id'],revision=run['revision'],review_token=revision['review_token'],
                candidate_id=revision['candidate_id'],manifest_sha256=revision['manifest_sha256'],
                capture_profile_sha256=_digest(json.loads(revision['profile_json'])),
                expected_sha256=_digest(json.loads(revision['expected_json'])),
                task_id=run['task_id'],task_revision=task['revision'],profile_id=profile['profile_id'],
                profile_revision=profile['revision'],profile_digest=profile['digest'])
            steps = db.execute('SELECT state,details_json FROM check_steps WHERE run_id=? ORDER BY ordinal',(run['id'],)).fetchall()
            return (fresh == binding and header['current_revision']==run['revision'] and header['state']=='prepared'
                    and selected is not None and selected['profile_id']==run['profile_id'] and task['state']=='ready'
                    and len(steps)==len(profile['definition']['steps'])
                    and all(step['state']=='passed' and json.loads(step['details_json']).get('output_complete') is True for step in steps))
        except CoreError:
            return False

    def _finish(self, run_id, operation_id, state, error):
        with self.catalog.connect(write=True) as db:
            row = self._row(run_id,db)
            if row['state'] not in _ACTIVE: return
            retained = json.loads(row['result_json'])
            if state == 'passed' and not self._metadata_matches(db, row, retained['binding']):
                state = 'stale'
            retained['error'] = error
            db.execute('UPDATE check_runs SET state=?,result_json=?,cleanup_state=?,updated_at=? WHERE id=?',
                       (state,canonical(retained),'unsafe' if retained.get('cleanup_blocked') else row['cleanup_state'],self.catalog._now(),run_id))
            operation = db.execute('SELECT state FROM operations WHERE id=?',(operation_id,)).fetchone()[0]
            if operation == 'queued': self.catalog._transition(db,operation_id,'running')
            self.catalog._transition(db,operation_id,'failed' if error else 'succeeded',
                                     result=dict(run_id=run_id,state=state),error=error,kind='check.'+state)
        self.catalog.notify()

    @staticmethod
    def recover(db):
        """No pass is reconstructed. Uncertain survivors retain their owned paths."""
        cursor = db.cursor(); cursor.row_factory = sqlite3.Row
        if cursor.execute('SELECT schema_version FROM core_metadata').fetchone()[0] != 4: return
        now = datetime.now(timezone.utc).isoformat()
        for row in cursor.execute("SELECT * FROM check_runs WHERE state IN ('reserved','running')").fetchall():
            result = json.loads(row['result_json'] or '{}')
            process = result.get('process')
            survivor = 'none_recorded'
            if process:
                survivor = 'identity_uncertain'
                pid = process.get('pid'); identity = process.get('start_time')
                if type(pid) is int and pid>1 and type(identity) is int and process_start_time(pid)==identity:
                    try:
                        if os.getpgid(pid)==pid:
                            os.killpg(pid,signal.SIGTERM)
                            time.sleep(CheckExecutor.TERM_GRACE_SECONDS)
                            if process_start_time(pid)==identity and os.getpgid(pid)==pid:
                                os.killpg(pid,signal.SIGKILL)
                            survivor = 'signalled_retained'
                    except (ProcessLookupError,PermissionError): pass
            result.update(recovery=dict(survivor=survivor,cleanup='retained'),
                          error=CoreError('OPERATION_INTERRUPTED','The core stopped before committing check results.',
                                          'Inspect retained evidence; start a fresh check run.').as_dict())
            operation = result.get('operation_id')
            db.execute("UPDATE check_steps SET state='interrupted' WHERE run_id=? AND state IN ('pending','running')",(row['id'],))
            db.execute("UPDATE check_runs SET state='interrupted',result_json=?,cleanup_state=?,updated_at=? WHERE id=?",
                       (canonical(result),'unsafe' if process else 'pending',now,row['id']))
            if operation:
                op = cursor.execute('SELECT * FROM operations WHERE id=?',(operation,)).fetchone()
                if op and op['state'] in ('queued','running'):
                    db.execute("UPDATE operations SET state='interrupted',updated_at=?,error_json=?,result_json=? WHERE id=?",
                        (now,canonical(result['error']),canonical(dict(run_id=row['id'],state='interrupted')),operation))
                    project = db.execute('SELECT project_id FROM core_metadata').fetchone()[0]
                    db.execute('INSERT INTO events(project_id,operation_id,actor,created_at,kind,state,details_json) VALUES (?,?,?,?,?,?,?)',
                               (project,operation,op['actor'],now,'check.interrupted','interrupted',canonical(dict(run_id=row['id']))))
