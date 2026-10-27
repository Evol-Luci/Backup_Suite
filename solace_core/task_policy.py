"""Revision-bound owner confirmation for task integration policy overrides."""
from datetime import datetime, timedelta, timezone
import hmac
import json
import uuid

from .check_profiles import AUTHORITY_WARNING, CheckProfiles, PREVIEW_LIFETIME_SECONDS
from .check_selection import CheckSelection
from .errors import CoreError, invalid
from .integration_snapshots import canonical
from .policy_schema import require_policy
from .project_policy import ProjectPolicy
from .tasks import TaskCatalog


class TaskPolicy:
    def __init__(self, catalog, *, now=None):
        self.catalog = catalog
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.tasks = TaskCatalog(catalog, now=self.now)
        self.profiles = CheckProfiles(catalog, now=self.now)

    @staticmethod
    def effective(db, task_id):
        if db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
            raise CoreError("TASK_NOT_FOUND", "Task was not found.", "Inspect the task list.")
        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] != 5:
            return dict(mode='manual', override='inherit', project_policy_revision=None, override_project_policy_revision=None,
                        required_profile_id=None, required_profile_revision=None,
                        required_profile_digest=None, selected_profile_id=None)
        project = ProjectPolicy.current(db)
        row = db.execute('SELECT mode,project_policy_revision FROM task_integration_policy WHERE task_id=?', (task_id,)).fetchone()
        override = row['mode'] if row else 'inherit'
        override_revision = row['project_policy_revision'] if row else None
        mode = project['mode'] if override == 'inherit' else override
        # Owner confirmation belongs to one project-policy revision. Preserve
        # stale evidence, but require confirmation again to override the default.
        if override == 'checked_queue' and override_revision != project['revision']:
            mode = project['mode']
        if mode == 'checked_queue' and project['mode'] == 'manual' and not project['allow_task_opt_in']:
            mode = 'manual'
        return dict(mode=mode, override=override, project_policy_revision=project['revision'],
                    override_project_policy_revision=override_revision,
                    **CheckSelection.policy_profiles(db, task_id, project))

    def show(self, task_id):
        with self.catalog.connect() as db:
            return self.effective(db, task_id)

    def _binding(self, db, params):
        task = params['task']
        row = self.tasks.assert_revision(db, task, params.get('expected_revision'))
        self.tasks.assert_unclaimed(db, task)
        if row['state'] not in ('created', 'active', 'blocked'):
            raise CoreError('TASK_STATE_INVALID', 'Task policy is frozen for ready or cancelled tasks.',
                            'Resume the task, change its policy, then save a new ready result.')
        mode = params.get('mode')
        if mode not in ('inherit', 'manual', 'checked_queue'):
            raise invalid('Task policy mode must be inherit, manual or checked_queue.')
        project = ProjectPolicy.current(db)
        active_profile = None
        if mode == 'checked_queue':
            if not project['allow_task_opt_in']:
                raise CoreError('POLICY_TASK_OPT_IN_DISABLED', 'The project does not allow task opt-in.',
                                'Ask the owner to enable task opt-in in project policy.')
            if not project['required_profile_id']:
                raise invalid('Task opt-in requires a project-required approved profile.')
            active_profile = self.profiles.active(db, project['required_profile_id'])
            if (active_profile['revision'] != project['required_profile_revision']
                    or active_profile['digest'] != project['required_profile_digest']):
                raise CoreError('POLICY_PROFILE_STALE', 'The required profile changed since project policy confirmation.',
                                'Review and reconfirm project policy before confirming task opt-in.')
        return dict(identity=self.profiles._identity(), task_id=task, task_revision=row['revision'],
                    mode=mode, project_policy=project, active_profile=active_profile, effective=self.effective(db, task))

    def preview(self, request):
        require_policy(self.catalog)
        with self.catalog.connect(write=True) as db:
            cached = self.tasks.lookup_request(request, db)
            if cached is not None:
                return cached
            binding = self._binding(db, request['params'])
            result = dict(binding=binding, authority_warning=AUTHORITY_WARNING,
                          next_revision=binding['task_revision'] + 1,
                          plan_token=uuid.uuid4().hex + ':' + request['id'],
                          expires_at=(self.now() + timedelta(seconds=PREVIEW_LIFETIME_SECONDS)).isoformat())
            self.tasks.save_request(db, request, result)
            return result

    def apply(self, request):
        require_policy(self.catalog)
        with self.catalog.connect(write=True) as db:
            cached = self.tasks.lookup_request(request, db)
            if cached is not None:
                return cached
            params = request['params']
            if params.get('confirmed') is not True:
                raise CoreError('POLICY_CONFIRMATION_REQUIRED', 'Explicit owner confirmation is required.', 'Confirm a fresh task policy preview.')
            token = params.get('plan_token')
            preview = {}
            if isinstance(token, str) and len(token) <= 512 and ':' in token:
                receipt = db.execute('SELECT result_json FROM task_requests WHERE request_id=?', (token.split(':', 1)[1],)).fetchone()
                preview = json.loads(receipt[0]) if receipt else {}
            if (not isinstance(preview.get('plan_token'), str) or not hmac.compare_digest(preview['plan_token'], token)
                    or 'binding' not in preview or 'expires_at' not in preview
                    or self.now() >= datetime.fromisoformat(preview['expires_at'])):
                raise CoreError('POLICY_PREVIEW_STALE', 'Task policy preview is unknown or expired.', 'Review a fresh task policy preview.')
            binding = self._binding(db, params)
            if canonical(binding) != canonical(preview['binding']):
                raise CoreError('POLICY_PREVIEW_STALE', 'Task or project policy changed after preview.', 'Review a fresh task policy preview.')
            now = self.now().isoformat()
            db.execute('INSERT INTO task_integration_policy VALUES (?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET mode=excluded.mode, project_policy_revision=excluded.project_policy_revision, selected_at=excluded.selected_at',
                       (params['task'], params['mode'], binding['project_policy']['revision'], now))
            revision = binding['task_revision'] + 1
            db.execute('UPDATE tasks SET revision=?,updated_at=? WHERE id=?', (revision, now, params['task']))
            result = dict(task_id=params['task'], revision=revision, integration_policy=self.effective(db, params['task']))
            self.tasks.event(db, request, 'task.integration_policy.apply', 'succeeded', result)
            self.tasks.save_request(db, request, result)
        self.catalog.notify()
        return result
