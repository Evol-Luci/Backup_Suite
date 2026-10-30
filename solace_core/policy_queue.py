"""Durable policy intents and bounded reconciliation; status reads schedule no work."""
from contextlib import contextmanager
import uuid
import sqlite3

from .errors import CoreError, invalid
from .lineage import Lineage
from .policy_schema import require_policy
from .task_policy import TaskPolicy


# Fixed across process restarts and versions: stage names are part of identity.
REQUEST_NAMESPACE = uuid.UUID('75fc411b-6e13-575c-b8e5-87b4fd95885d')


@contextmanager
def _read(catalog):
    # Polling an idle WAL-format catalog must not create WAL/SHM sidecars.
    from .catalog import readonly_database
    catalog.validate(require_root=False)
    with readonly_database(catalog.database) as db:
        db.row_factory = sqlite3.Row
        yield db


class PolicyQueue:
    @staticmethod
    def enqueue_ready(db, catalog, task_id, checkpoint_id, task_revision, now):
        # Engine publication hooks use tuple rows; keep their connection convention.
        previous_factory = db.row_factory
        db.row_factory = sqlite3.Row
        try:
            return PolicyQueue._enqueue(db, catalog, task_id, checkpoint_id, task_revision, now)
        finally:
            db.row_factory = previous_factory

    @staticmethod
    def _enqueue(db, catalog, task_id, checkpoint_id, task_revision, now):
        effective = TaskPolicy.effective(db, task_id)
        if effective['mode'] != 'checked_queue':
            return None
        task = db.execute('SELECT parent_id FROM tasks WHERE id=?', (task_id,)).fetchone()
        if task['parent_id']:
            target = db.execute('SELECT w.id,w.head_checkpoint,t.revision FROM workspaces w '
                                'JOIN tasks t ON t.id=w.task_id WHERE w.task_id=?', (task['parent_id'],)).fetchone()
            if target is None:
                raise CoreError('WORKSPACE_UNAVAILABLE', 'The parent has no published workspace.', 'Inspect the parent workspace.')
            key, revision, head = 'workspace:' + target['id'], target['revision'], target['head_checkpoint']
        else:
            key, revision, head = 'source', None, Lineage(catalog).source_head(db)
        existing = db.execute('SELECT id FROM integration_policy_intents WHERE task_id=? AND result_checkpoint_id=? AND target_key=?',
                              (task_id, checkpoint_id, key)).fetchone()
        if existing:
            return existing['id']
        intent_id = uuid.uuid4().hex
        db.execute('INSERT INTO integration_policy_intents '
                   '(id,task_id,result_checkpoint_id,target_key,target_revision,target_checkpoint_id,task_revision,policy_revision,'
                   'profile_id,profile_revision,profile_digest,selected_profile_id,stage,prepare_request_id,check_request_id,created_at,updated_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (intent_id, task_id, checkpoint_id, key, revision, head, task_revision, effective['project_policy_revision'],
                    effective['required_profile_id'], effective['required_profile_revision'], effective['required_profile_digest'],
                    effective['selected_profile_id'], 'queued',
                    uuid.uuid5(REQUEST_NAMESPACE, intent_id + ':prepare').hex,
                    uuid.uuid5(REQUEST_NAMESPACE, intent_id + ':check').hex, now, now))
        return intent_id

    @staticmethod
    def _status(db, catalog, row, service=None):
        # Intent columns contain references and bounded worker state only. Never
        # join command output, request receipts, or retained prompt records.
        result = dict(row)
        result.pop('rowid', None)
        actions = {
            'queued': 'Wait for automatic preparation.',
            'preparing': 'Inspect the preparation operation or wait.',
            'checking': 'Inspect the check run or wait.',
            'pending_apply': 'Inspect the plan diff and check run; approve with this check run, pause external writers, then explicitly apply with writers-paused acknowledgement.',
            'pending': 'Inspect the pending reason and plan or check run; resolve the cause and publish a new ready result.',
            'done': 'Inspect the historical integration receipt.',
        }
        result['next_action'] = actions.get(row['stage'], 'Inspect the intent.')
        receipt = db.execute('SELECT id FROM integration_receipts WHERE task_id=? AND result_id=? AND target_key=?',
            (row['task_id'], row['result_checkpoint_id'], row['target_key'])).fetchone()
        result['receipt_id'] = receipt['id'] if receipt else None
        if receipt:
            result.update(stage='done', stale=False, stale_reasons=[], next_action=actions['done'])
            return result
        reasons = []
        task = db.execute('SELECT state,revision FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
        latest = db.execute("SELECT checkpoint_id FROM task_checkpoint_refs WHERE task_id=? AND role='result' ORDER BY created_at DESC,rowid DESC LIMIT 1", (row['task_id'],)).fetchone()
        if task['state'] != 'ready' or task['revision'] != row['task_revision'] or not latest or latest[0] != row['result_checkpoint_id']:
            reasons.append('task_changed')
        effective = TaskPolicy.effective(db, row['task_id'])
        if effective['mode'] != 'checked_queue' or effective['project_policy_revision'] != row['policy_revision']:
            reasons.append('policy_changed')
        if (effective['required_profile_id'], effective['required_profile_revision'], effective['required_profile_digest'], effective['selected_profile_id']) != (row['profile_id'], row['profile_revision'], row['profile_digest'], row['selected_profile_id']):
            reasons.append('profile_binding_changed')
        profile = db.execute('SELECT p.retired,p.active_revision,r.digest FROM check_profiles p LEFT JOIN check_profile_revisions r ON r.profile_id=p.id AND r.revision=p.active_revision WHERE p.id=?', (row['profile_id'],)).fetchone()
        if not profile or profile['retired'] or (profile['active_revision'], profile['digest']) != (row['profile_revision'], row['profile_digest']):
            reasons.append('profile_changed')
        if row['target_key'] == 'source':
            changed = Lineage(catalog).source_head(db) != row['target_checkpoint_id']
        else:
            target = db.execute('SELECT w.head_checkpoint,t.revision FROM workspaces w JOIN tasks t ON t.id=w.task_id WHERE w.id=?', (row['target_key'].split(':', 1)[1],)).fetchone()
            changed = not target or (target['head_checkpoint'], target['revision']) != (row['target_checkpoint_id'], row['target_revision'])
        if changed:
            reasons.append('target_changed')
        if row['plan_id'] and row['stage'] in ('checking', 'pending_apply'):
            plan = db.execute('SELECT current_revision,state FROM integration_plans WHERE id=?', (row['plan_id'],)).fetchone()
            if not plan or plan['current_revision'] != row['plan_revision'] or plan['state'] not in ('prepared', 'approved'):
                reasons.append('plan_changed')
        if row['stage'] == 'pending_apply' and not reasons and service is not None:
            # A target may change on disk without advancing its catalog head.
            # Validate the exact retained plan and passed evidence as approval
            # would, but do not reserve, rerun commands, or mutate the target.
            from .check_approvals import CheckApprovals
            from .integration_claims import IntegrationClaims
            from .integration_plans import IntegrationPlans
            try:
                retained = IntegrationPlans(service, catalog).load(row['plan_id'], row['plan_revision'])
                IntegrationClaims(service, catalog).validate(retained, scan=True)
            except CoreError as error:
                if error.code in ('INTEGRATION_PLAN_STALE', 'INTEGRATION_STALE'):
                    reasons.append('target_changed')
                else:
                    raise
            else:
                try:
                    CheckApprovals.evidence(db, row['run_id'], retained)
                except CoreError as error:
                    if error.code == 'INTEGRATION_PLAN_STALE':
                        reasons.append('check_binding_changed')
                    else:
                        raise
        result.update(stale=bool(reasons), stale_reasons=reasons)
        return result

    @staticmethod
    def list(catalog, after=0, limit=128, *, service):
        require_policy(catalog)
        if type(after) is not int or not 0 <= after <= 2**63-1 or type(limit) is not int or not 1 <= limit <= 256:
            raise invalid('Queue page requires a nonnegative SQLite integer cursor and limit from 1–256.')
        with _read(catalog) as db:
            rows = db.execute('SELECT rowid,* FROM integration_policy_intents WHERE rowid>? ORDER BY rowid LIMIT ?', (after, limit+1)).fetchall()
            page = rows[:limit]
            return dict(intents=[PolicyQueue._status(db, catalog, row, service) for row in page], next_after=page[-1]['rowid'] if len(rows)>limit else None)

    @staticmethod
    def show(catalog, intent_id, *, service):
        require_policy(catalog)
        if not isinstance(intent_id, str) or not 1 <= len(intent_id) <= 128 or '\0' in intent_id:
            raise invalid('Intent ID must be a bounded nonempty string.')
        with _read(catalog) as db:
            row = db.execute('SELECT * FROM integration_policy_intents WHERE id=?', (intent_id,)).fetchone()
            if row is None:
                raise CoreError('POLICY_INTENT_NOT_FOUND', 'This queue intent does not exist.', 'Inspect the policy queue list.')
            return PolicyQueue._status(db, catalog, row, service)

    @staticmethod
    def authenticate(db, catalog, request):
        """Recheck automatic authority inside acceptance and execution boundaries.

        Historical API receipts are looked up before this guard. Older catalogs
        and ordinary manual actors retain their existing behavior.
        """
        import json
        if request['actor'] != 'policy' or db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] != 5:
            return
        method = request['method']
        column = 'prepare_request_id' if method == 'integration.prepare' else 'check_request_id'
        row = db.execute('SELECT * FROM integration_policy_intents WHERE '+column+'=?', (request['id'],)).fetchone()
        def refuse(reason):
            raise CoreError('POLICY_INTENT_STALE', 'Automatic work stopped: '+reason+'.', 'Inspect the queue intent and publish a fresh ready result after resolving its cause.')
        if row is None: refuse('missing intent')
        status = PolicyQueue._status(db, catalog, row)
        if status['stale']: refuse(status['stale_reasons'][0])
        if row['selected_profile_id'] != row['profile_id']: refuse('required profile not selected')
        if row['stage'] in ('pending', 'pending_apply', 'done') or status['receipt_id']: refuse('intent is terminal')
        expected = dict(project=catalog.registration['id'])
        if method == 'integration.prepare':
            expected.update(task=row['task_id'], result=row['result_checkpoint_id'], expected_revision=row['task_revision'])
            if row['target_key'] != 'source': expected['expected_target_revision'] = row['target_revision']
        else:
            operation = db.execute('SELECT * FROM operations WHERE request_id=?', (row['prepare_request_id'],)).fetchone()
            if not operation or operation['state'] != 'succeeded' or operation['actor'] != 'policy' or operation['kind'] != 'integration.prepare':
                refuse('prepare evidence unavailable')
            result = json.loads(operation['result_json'])
            expected.update(plan=result['plan_id'], revision=result['revision'], profile=row['profile_id'])
        if request['params'] != expected: refuse('request binding changed')

    @staticmethod
    def _accepted_updates(db, row):
        """Recover acceptance even if the reply or following intent write was lost."""
        import json
        if row['stage'] not in ('queued', 'preparing'): return {}
        preparing = row['stage'] == 'queued'
        key = 'prepare_request_id' if preparing else 'check_request_id'
        method = 'integration.prepare' if preparing else 'integration.check.run'
        operation = db.execute('SELECT * FROM operations WHERE request_id=? AND actor=? AND kind=?',
            (row[key], 'policy', method)).fetchone()
        if not operation: return {}
        if preparing:
            return dict(stage='preparing', prepare_operation_id=operation['id'], reason=None)
        run_id = json.loads(operation['params_json'])['run_id']
        run = db.execute('SELECT plan_id,revision FROM check_runs WHERE id=?', (run_id,)).fetchone()
        if not run:
            return dict(stage='pending', check_operation_id=operation['id'], run_id=run_id, reason='check_missing')
        return dict(stage='checking', check_operation_id=operation['id'], run_id=run_id,
            plan_id=run['plan_id'], plan_revision=run['revision'], reason=None)

    @staticmethod
    def _try_accept(service, project_id, request):
        # The lookup probe is not a reservation: a publisher may acquire either
        # lock afterward. Hold both try-locks through reentrant API acceptance.
        # No operation completion is awaited while either lock is held.
        if not service.registry.lock.acquire(blocking=False): return None
        try:
            coordinator = service.coordinator(project_id)
            if not coordinator.lock.acquire(blocking=False): return None
            try:
                if service._closed: return None
                return service._dispatch(request)
            finally:
                coordinator.lock.release()
        finally:
            service.registry.lock.release()

    @staticmethod
    def tick(service, project_id, limit=32):
        """Reconcile bounded evidence; never wait for an operation in this FIFO.

        A dedicated scheduler calls this outside the operation executor. Request
        receipts close the crash window between acceptance and intent updates.
        """
        import json
        from .schema import schema_version
        from .errors import classify
        if type(limit) is not int or not 1 <= limit <= 256:
            raise invalid('Worker limit must be from 1–256.')
        if service._closed:
            return
        coordinator = service.coordinator(project_id)
        # Match placement lock ordering; a busy publisher must not block ticks.
        if not service.registry.lock.acquire(blocking=False): return
        try:
            if not coordinator.lock.acquire(blocking=False): return
            try:
                catalog = service.catalog(project_id)
            finally:
                coordinator.lock.release()
        finally:
            service.registry.lock.release()
        if schema_version(catalog) != 5:
            return
        # Volatile cursors provide fairness without rewriting settled intents.
        # Restart begins at row zero; durable stage/request evidence is unchanged.
        with service._guard:
            cursor = service._policy_cursors.get(project_id, 0)
        with _read(catalog) as db:
            rows = db.execute("SELECT rowid,* FROM integration_policy_intents WHERE stage!='done' AND rowid>? ORDER BY rowid LIMIT ?", (cursor, limit)).fetchall()
            if not rows and cursor:
                rows = db.execute("SELECT rowid,* FROM integration_policy_intents WHERE stage!='done' ORDER BY rowid LIMIT ?", (limit,)).fetchall()
        with service._guard:
            service._policy_cursors[project_id] = rows[-1]['rowid'] if rows else 0
        for row in rows:
            if service._closed: return
            updates = {}
            try:
                with _read(catalog) as db:
                    accepted_updates = PolicyQueue._accepted_updates(db, row)
                    receipt = db.execute('SELECT plan_id,revision FROM integration_receipts WHERE task_id=? AND result_id=? AND target_key=?',
                        (row['task_id'], row['result_checkpoint_id'], row['target_key'])).fetchone()
                    status = PolicyQueue._status(db, catalog, row, service) if not receipt else None
                if receipt:
                    updates = dict(stage='done', reason=None, plan_id=receipt['plan_id'], plan_revision=receipt['revision'])
                elif accepted_updates:
                    updates = accepted_updates
                elif status['stale']:
                    updates = dict(stage='pending', reason=status['stale_reasons'][0])
                elif row['stage'] in ('pending', 'pending_apply'):
                    continue
                elif row['selected_profile_id'] != row['profile_id']:
                    updates = dict(stage='pending', reason='check_profile_missing' if not row['selected_profile_id'] else 'check_profile_mismatch')
                elif row['stage'] == 'queued':
                    params = dict(project=project_id, task=row['task_id'], result=row['result_checkpoint_id'], expected_revision=row['task_revision'])
                    if row['target_key'] != 'source': params['expected_target_revision'] = row['target_revision']
                    accepted = PolicyQueue._try_accept(service, project_id, dict(version=1, id=row['prepare_request_id'], actor='policy', method='integration.prepare', params=params))
                    if accepted is None: continue
                    updates = dict(stage='preparing', prepare_operation_id=accepted['operation_id'], reason=None)
                elif row['stage'] == 'preparing':
                    operation = catalog.get_operation(row['prepare_operation_id'])
                    if operation['state'] in ('queued', 'running'): continue
                    if operation['state'] != 'succeeded':
                        updates = dict(stage='pending', reason='prepare_' + operation['state'])
                    else:
                        result = operation['result']
                        updates = dict(plan_id=result['plan_id'], plan_revision=result['revision'])
                        if result.get('already_applied'):
                            updates.update(stage='done', reason=None)
                        elif result.get('conflict_count') or result.get('state') != 'prepared':
                            updates.update(stage='pending', reason='prepare_conflicted')
                        else:
                            accepted = PolicyQueue._try_accept(service, project_id, dict(version=1, id=row['check_request_id'], actor='policy', method='integration.check.run',
                                params=dict(project=project_id, plan=result['plan_id'], revision=result['revision'], profile=row['profile_id'])))
                            if accepted is None: continue
                            updates.update(stage='checking', run_id=accepted['run_id'], check_operation_id=accepted['operation_id'], reason=None)
                elif row['stage'] == 'checking':
                    with _read(catalog) as db:
                        run = db.execute('SELECT r.*, (SELECT state FROM operations WHERE id=?) AS operation_state '
                                         'FROM check_runs AS r WHERE r.id=?',
                                         (row['check_operation_id'], row['run_id'])).fetchone()
                    if run and run['state'] in ('reserved', 'running') and run['operation_state'] in ('queued', 'running'): continue
                    if not run:
                        updates = dict(stage='pending', reason='check_missing')
                    elif run['state'] == 'passed' and run['operation_state'] == 'succeeded':
                        binding = json.loads(run['result_json'])['binding']
                        expected = dict(plan_id=row['plan_id'], revision=row['plan_revision'], task_id=row['task_id'], task_revision=row['task_revision'],
                            profile_id=row['profile_id'], profile_revision=row['profile_revision'], profile_digest=row['profile_digest'])
                        if any(binding.get(k) != v for k, v in expected.items()):
                            updates = dict(stage='pending', reason='check_binding_changed')
                        else:
                            from .check_runs import CheckRuns
                            from .check_profiles import CheckProfiles
                            from .check_approvals import CheckApprovals
                            runs = CheckRuns(service, catalog)
                            plan = runs.plans.load(row['plan_id'], row['plan_revision'])
                            with _read(catalog) as db:
                                profile = CheckProfiles.active(db, row['profile_id'])
                                CheckApprovals.evidence(db, row['run_id'], plan)
                            if runs.binding(plan, profile) != binding:
                                updates = dict(stage='pending', reason='check_binding_changed')
                            else:
                                updates = dict(stage='pending_apply', reason=None)
                    else:
                        updates = dict(stage='pending', reason='check_' + run['state'])
            except Exception as error:
                code = classify(error).code
                # Committed acceptance outranks a lost reply/notification. Never
                # turn it into a retry of an ambiguous command.
                with _read(catalog) as db:
                    accepted_updates = PolicyQueue._accepted_updates(db, row)
                if accepted_updates:
                    updates = accepted_updates
                elif code in ('PROJECT_BUSY', 'TASK_BUSY', 'INTEGRATION_TARGET_BUSY'):
                    updates = dict(reason=code.lower())
                else:
                    updates = dict(stage='pending', reason=code.lower())
            if updates and any(row[k] != v for k, v in updates.items()):
                updates['updated_at'] = catalog._now()
                with catalog.connect(write=True) as db:
                    db.execute('UPDATE integration_policy_intents SET ' + ','.join(k+'=?' for k in updates) + ' WHERE id=? AND stage=?',
                        (*updates.values(), row['id'], row['stage']))
