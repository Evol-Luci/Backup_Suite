"""Owner-confirmed, revision-bound project integration policy."""
from datetime import datetime, timedelta, timezone
import hmac
import json
import uuid

from .check_profiles import CheckProfiles, PREVIEW_LIFETIME_SECONDS
from .errors import CoreError, invalid
from .integration_snapshots import canonical
from .policy_schema import require_policy
from .tasks import TaskCatalog


def _error(code, message):
    return CoreError(code, message, 'Review and confirm a fresh project integration policy preview.')


class ProjectPolicy:
    def __init__(self, catalog, *, now=None):
        self.catalog = catalog
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.tasks = TaskCatalog(catalog, now=self.now)
        self.profiles = CheckProfiles(catalog, now=self.now)

    @staticmethod
    def current(db):
        row = dict(db.execute('SELECT * FROM project_integration_policy WHERE singleton=1').fetchone())
        row.pop('singleton')
        row['allow_task_opt_in'] = bool(row['allow_task_opt_in'])
        return row

    def show(self):
        require_policy(self.catalog)
        with self.catalog.connect() as db:
            return self.current(db)

    def _binding(self, db, params):
        mode = params.get('mode')
        opt_in = params.get('allow_task_opt_in')
        profile_id = params.get('required_profile')
        if mode not in ('manual', 'checked_queue') or type(opt_in) is not bool:
            raise invalid('Policy requires mode manual or checked_queue and boolean allow_task_opt_in.')
        if profile_id is None and (mode == 'checked_queue' or opt_in):
            raise invalid('Checked queue and task opt-in require an active approved profile.')
        profile = self.profiles.active(db, profile_id) if profile_id is not None else None
        return dict(identity=self.profiles._identity(), previous_revision=self.current(db)['revision'],
                    mode=mode, allow_task_opt_in=opt_in, required_profile_id=profile_id,
                    required_profile_revision=profile['revision'] if profile else None,
                    required_profile_digest=profile['digest'] if profile else None)

    def preview(self, request):
        require_policy(self.catalog)
        with self.catalog.connect(write=True) as db:
            cached = self.tasks.lookup_request(request, db)
            if cached is not None:
                return cached
            binding = self._binding(db, request['params'])
            result = dict(binding=binding, next_revision=binding['previous_revision'] + 1,
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
                raise _error('POLICY_CONFIRMATION_REQUIRED', 'Explicit local owner confirmation is required; actor labels are not authority.')
            token = params.get('plan_token')
            if not isinstance(token, str) or len(token) > 512 or ':' not in token:
                raise _error('POLICY_PREVIEW_STALE', 'A fresh issued policy preview token is required.')
            receipt = db.execute('SELECT result_json FROM task_requests WHERE request_id=?', (token.split(':', 1)[1],)).fetchone()
            preview = json.loads(receipt[0]) if receipt else {}
            if (not isinstance(preview.get('plan_token'), str)
                    or not hmac.compare_digest(preview['plan_token'], token)
                    or 'expires_at' not in preview or 'binding' not in preview
                    or self.now() >= datetime.fromisoformat(preview['expires_at'])):
                raise _error('POLICY_PREVIEW_STALE', 'The policy preview token is unknown or expired.')
            try:
                binding = self._binding(db, params)
            except CoreError as exc:
                if exc.code in ('CHECK_PROFILE_RETIRED', 'CHECK_PROFILE_NOT_FOUND'):
                    raise _error('POLICY_PREVIEW_STALE', 'The required profile is no longer active.') from exc
                raise
            if canonical(binding) != canonical(preview['binding']):
                raise _error('POLICY_PREVIEW_STALE', 'The policy, profile revision or store identity changed after preview.')
            db.execute('UPDATE project_integration_policy SET revision=?, mode=?, required_profile_id=?, '
                       'required_profile_revision=?, required_profile_digest=?, allow_task_opt_in=?, actor=?, updated_at=? WHERE singleton=1',
                       (binding['previous_revision'] + 1, binding['mode'], binding['required_profile_id'],
                        binding['required_profile_revision'], binding['required_profile_digest'],
                        int(binding['allow_task_opt_in']), request['actor'], self.now().isoformat()))
            result = self.current(db)
            self.tasks.event(db, request, 'project.integration_policy.apply', 'succeeded', result)
            self.tasks.save_request(db, request, result)
            return result
