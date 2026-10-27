"""Durable ready-result evidence; reading this queue never schedules work."""
import uuid
import sqlite3

from .errors import CoreError, invalid
from .lineage import Lineage
from .policy_schema import require_policy
from .task_policy import TaskPolicy


# Fixed across process restarts and versions: stage names are part of identity.
REQUEST_NAMESPACE = uuid.UUID('75fc411b-6e13-575c-b8e5-87b4fd95885d')


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
    def _status(db, catalog, row):
        # Intent columns contain references and bounded worker state only. Never
        # join command output, request receipts, or retained prompt records.
        result = dict(row)
        result.pop('rowid', None)
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
        result.update(stale=bool(reasons), stale_reasons=reasons)
        return result

    @staticmethod
    def list(catalog, after=0, limit=128):
        require_policy(catalog)
        if type(after) is not int or not 0 <= after <= 2**63-1 or type(limit) is not int or not 1 <= limit <= 256:
            raise invalid('Queue page requires a nonnegative SQLite integer cursor and limit from 1–256.')
        with catalog.connect() as db:
            rows = db.execute('SELECT rowid,* FROM integration_policy_intents WHERE rowid>? ORDER BY rowid LIMIT ?', (after, limit+1)).fetchall()
            page = rows[:limit]
            return dict(intents=[PolicyQueue._status(db, catalog, row) for row in page], next_after=page[-1]['rowid'] if len(rows)>limit else None)

    @staticmethod
    def show(catalog, intent_id):
        require_policy(catalog)
        if not isinstance(intent_id, str) or not 1 <= len(intent_id) <= 128 or '\0' in intent_id:
            raise invalid('Intent ID must be a bounded nonempty string.')
        with catalog.connect() as db:
            row = db.execute('SELECT * FROM integration_policy_intents WHERE id=?', (intent_id,)).fetchone()
            if row is None:
                raise CoreError('POLICY_INTENT_NOT_FOUND', 'This queue intent does not exist.', 'Inspect the policy queue list.')
            return PolicyQueue._status(db, catalog, row)
