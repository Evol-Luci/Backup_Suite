"""Local-owner confirmation and immutable, bounded command profile revisions.

The confirmation ceremony is a workflow boundary, not isolation from other
processes running as the same OS user. Actor strings are audit labels only.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import re
import sqlite3
import uuid

from .errors import CoreError, invalid
from .integration_snapshots import canonical
from .schema import require_checks
from .tasks import TaskCatalog

ENVIRONMENT_TEMPLATE = dict(PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8',
                            HOME='{run_home}', TMPDIR='{run_tmpdir}')
MAX_DEFINITION_BYTES = 65536
PREVIEW_LIFETIME_SECONDS = 900
AUTHORITY_WARNING = ('These commands run with your local OS user authority and may access '
                     'files and network outside the candidate copy. This is not a sandbox. '
                     'Output may contain secrets and remains in the local project store. '
                     'Confirm only commands you approve for this project.')


def _text(value, maximum, label, *, printable=False, empty=False):
    if (not isinstance(value, str) or (not value and not empty) or '\0' in value
            or len(value) > maximum or (printable and not value.isprintable())):
        raise invalid(f'{label} must be a bounded string without NUL characters.')
    return value


def validate_profile(value: dict) -> dict:
    """Return the exact effective definition, rejecting unsupported runtime knobs."""
    if not isinstance(value, dict) or set(value) != {'version', 'name', 'steps', 'environment'}:
        raise invalid('A profile requires version, name, steps and environment only.')
    if type(value['version']) is not int or value['version'] != 1:
        raise invalid('Check profile version must be 1.')
    name = _text(value['name'], 80, 'Profile name', printable=True)
    steps = value['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise invalid('A profile requires 1–32 ordered steps.')
    normalized, names = [], set()
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'name', 'argv', 'timeout_seconds', 'output_limit_bytes'}:
            raise invalid('Each step requires name, argv, timeout_seconds and output_limit_bytes only; its directory is the candidate root.')
        step_name = _text(step['name'], 80, 'Step name', printable=True)
        if step_name in names:
            raise invalid('Step names must be distinct.')
        names.add(step_name)
        argv = step['argv']
        if not isinstance(argv, list) or not 1 <= len(argv) <= 256:
            raise invalid('Step argv must contain 1–256 nonempty strings; shell strings are not parsed.')
        argv = [_text(arg, 16384, 'Command argument') for arg in argv]
        timeout, cap = step['timeout_seconds'], step['output_limit_bytes']
        if type(timeout) not in (int, float) or not 1 <= timeout <= 3600 or not math.isfinite(timeout):
            raise invalid('Step timeout must be a finite number from 1–3600 seconds.')
        if type(cap) is not int or not 1024 <= cap <= 1048576:
            raise invalid('Step output_limit_bytes must be 1024–1048576 per stream.')
        normalized.append(dict(name=step_name, argv=argv, timeout_seconds=timeout, output_limit_bytes=cap))
    additions = value['environment']
    if not isinstance(additions, dict) or len(additions) > 64:
        raise invalid('Profile environment must contain at most 64 variables.')
    environment = dict(ENVIRONMENT_TEMPLATE)
    for key, entry in additions.items():
        if not isinstance(key, str) or len(key) > 128 or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise invalid('Environment names must be valid variable names of at most 128 characters.')
        _text(entry, 16384, 'Environment value', empty=True)
        if key in ('HOME', 'TMPDIR') and entry != ENVIRONMENT_TEMPLATE[key]:
            raise invalid('HOME and TMPDIR are fixed to owned private run directories.')
        environment[key] = entry
    if len(environment) > 64:
        raise invalid('Effective profile environment must contain at most 64 variables.')
    result = dict(version=1, name=name, steps=normalized, environment=environment)
    if len(canonical(result).encode('utf-8')) > MAX_DEFINITION_BYTES:
        raise invalid('The effective profile definition must fit in 65536 bytes.')
    return result


def _error(code, message):
    return CoreError(code, message, 'Inspect the current profile and review a fresh profile preview.')


class CheckProfiles:
    def __init__(self, catalog, *, now=None):
        self.catalog = catalog
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.tasks = TaskCatalog(catalog, now=self.now)

    @staticmethod
    def _row(db, profile_id):
        _text(profile_id, 128, 'Profile ID')
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row
        row = cursor.execute('SELECT * FROM check_profiles WHERE id=?', (profile_id,)).fetchone()
        if row is None:
            raise _error('CHECK_PROFILE_NOT_FOUND', 'This check profile does not exist in this project.')
        return dict(row)

    @staticmethod
    def _read(db, row, revision=None):
        revision = row['active_revision'] if revision is None else revision
        if type(revision) is not int or revision < 1:
            raise invalid('Profile revision must be a positive integer.')
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row
        retained = cursor.execute('SELECT * FROM check_profile_revisions WHERE profile_id=? AND revision=?',
                                  (row['id'], revision)).fetchone()
        if retained is None:
            raise _error('CHECK_PROFILE_REVISION_NOT_FOUND', 'This profile revision does not exist.')
        return dict(profile_id=row['id'], revision=revision, active_revision=row['active_revision'],
                    retired=bool(row['retired']), digest=retained['digest'],
                    definition=json.loads(retained['definition_json']), actor=retained['actor'],
                    created_at=retained['created_at'])

    @staticmethod
    def active(db, profile_id: str) -> dict:
        row = CheckProfiles._row(db, profile_id)
        if row['retired']:
            raise _error('CHECK_PROFILE_RETIRED', 'This check profile is retired and unavailable for new runs.')
        return CheckProfiles._read(db, row)

    def _identity(self):
        stat = self.catalog.store.stat()
        database = self.catalog.database.stat()
        registration = self.catalog.registration
        return dict(project=registration['id'], store_uuid=registration['store_uuid'],
                    mount_id=registration['mount_id'], store=str(self.catalog.store),
                    store_device=stat.st_dev, store_inode=stat.st_ino,
                    database_device=database.st_dev, database_inode=database.st_ino)

    def _busy(self, db, profile_id):
        # Protect even recovery-required journals: file publication may already
        # have happened and receipt commit must still authenticate its binding.
        if db.execute("SELECT 1 FROM integration_journals j "
                      "JOIN integration_checked_approvals a ON a.plan_id=j.plan_id AND a.revision=j.revision "
                      "JOIN check_runs r ON r.id=a.run_id "
                      "WHERE r.profile_id=? AND j.phase NOT IN ('committed','restored','abandoned') LIMIT 1",
                      (profile_id,)).fetchone():
            raise _error('CHECK_PROFILE_BUSY', 'An active checked apply or recovery journal still uses this profile.')

    def _binding(self, db, params):
        action = params.get('action')
        if action not in ('create', 'update', 'retire'):
            raise invalid('Profile action must be create, update or retire.')
        profile_id = params.get('profile_id')
        if action == 'create':
            if profile_id is not None:
                raise invalid('Create must not supply a profile ID.')
            row, previous, next_revision = None, None, 1
        else:
            row = self._row(db, profile_id)
            if row['retired']:
                raise _error('CHECK_PROFILE_RETIRED', 'This check profile is already retired.')
            self._busy(db, profile_id)
            previous = dict(revision=row['active_revision'], retired=row['retired'])
            next_revision = row['active_revision'] + 1
        if action == 'retire':
            if 'definition' in params:
                raise invalid('Retire does not accept a new command definition.')
            definition = self._read(db, row)['definition']
        else:
            definition = validate_profile(params.get('definition'))
        duplicate = db.execute('SELECT id FROM check_profiles WHERE name=?', (definition['name'],)).fetchone()
        if duplicate and duplicate[0] != profile_id:
            raise _error('CHECK_PROFILE_NAME_EXISTS', 'A profile with this name already exists, including retained retired profiles.')
        return dict(identity=self._identity(), action=action, profile_id=profile_id, previous=previous,
                    creation_generation=db.execute('SELECT COALESCE(MAX(rowid),0) FROM check_profiles').fetchone()[0] if action == 'create' else None,
                    next_revision=next_revision, definition=definition,
                    digest=hashlib.sha256(canonical(definition).encode('utf-8')).hexdigest())

    def preview(self, request: dict) -> dict:
        require_checks(self.catalog)
        with self.catalog.connect(write=True) as db:
            cached = self.tasks.lookup_request(request, db)
            if cached is not None:
                return cached
            binding = self._binding(db, request['params'])
            result = dict(profile_id=binding['profile_id'], name=binding['definition']['name'],
                          next_revision=binding['next_revision'], digest=binding['digest'],
                          commands=binding['definition']['steps'], environment=binding['definition']['environment'],
                          working_directory='candidate_root', executable_lookup='PATH from the effective environment; no shell parsing',
                          authority_warning=AUTHORITY_WARNING,
                          plan_token=uuid.uuid4().hex + ':' + request['id'],
                          expires_at=(self.now() + timedelta(seconds=PREVIEW_LIFETIME_SECONDS)).isoformat(),
                          binding=binding)
            self.tasks.save_request(db, request, result)
            return result

    def apply(self, request: dict) -> dict:
        require_checks(self.catalog)
        with self.catalog.connect(write=True) as db:
            cached = self.tasks.lookup_request(request, db)
            if cached is not None:
                return cached
            params = request['params']
            if params.get('confirmed') is not True:
                raise _error('CHECK_PROFILE_CONFIRMATION_REQUIRED', 'Explicit local owner confirmation is required; an actor label is not authority.')
            token = params.get('plan_token')
            if not isinstance(token, str) or len(token) > 512 or ':' not in token:
                raise _error('CHECK_PROFILE_PREVIEW_STALE', 'A fresh issued profile preview token is required.')
            receipt = db.execute('SELECT result_json FROM task_requests WHERE request_id=?', (token.split(':', 1)[1],)).fetchone()
            preview = json.loads(receipt[0]) if receipt else {}
            if (not isinstance(preview.get('plan_token'), str)
                    or not hmac.compare_digest(preview['plan_token'], token)
                    or 'expires_at' not in preview or 'binding' not in preview
                    or self.now() >= datetime.fromisoformat(preview['expires_at'])):
                raise _error('CHECK_PROFILE_PREVIEW_STALE', 'The profile preview token is unknown or expired.')
            binding = self._binding(db, params)
            if canonical(binding) != canonical(preview['binding']):
                raise _error('CHECK_PROFILE_PREVIEW_STALE', 'The proposed commands, store identity or active revision changed after preview.')
            now = self.now().isoformat()
            profile_id = binding['profile_id'] or uuid.uuid4().hex
            definition = binding['definition']
            if binding['action'] == 'create':
                db.execute('INSERT INTO check_profiles VALUES (?,?,1,0,?,?)',
                           (profile_id, definition['name'], now, now))
            else:
                db.execute('UPDATE check_profiles SET name=?,active_revision=?,retired=?,updated_at=? WHERE id=?',
                           (definition['name'], binding['next_revision'], int(binding['action'] == 'retire'), now, profile_id))
            db.execute('INSERT INTO check_profile_revisions VALUES (?,?,?,?,?,?)',
                       (profile_id, binding['next_revision'], binding['digest'], canonical(definition), request['actor'], now))
            result = self._read(db, self._row(db, profile_id))
            self.tasks.event(db, request, 'check_profile.' + binding['action'], 'succeeded',
                             dict(profile_id=profile_id, revision=result['revision'], digest=result['digest']))
            self.tasks.save_request(db, request, result)
            return result

    def show(self, profile_id: str, revision=None) -> dict:
        require_checks(self.catalog)
        with self.catalog.connect() as db:
            return self._read(db, self._row(db, profile_id), revision)

    def list(self, after: int = 0, limit: int = 128) -> dict:
        require_checks(self.catalog)
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 256:
            raise invalid('Profile page requires a nonnegative SQLite integer cursor and limit from 1–256.')
        with self.catalog.connect() as db:
            rows = db.execute('SELECT rowid,id,name,active_revision,updated_at FROM check_profiles '
                              'WHERE rowid>? AND retired=0 ORDER BY rowid LIMIT ?', (after, limit + 1)).fetchall()
            page = rows[:limit]
            return dict(profiles=[dict(profile_id=r['id'], name=r['name'], revision=r['active_revision'], updated_at=r['updated_at']) for r in page],
                        next_after=page[-1]['rowid'] if len(rows) > limit else None)
