"""Task-local selection of approved project checks; commands remain project-owned."""
import sqlite3
from .errors import CoreError
from .schema import require_checks
from .tasks import TaskCatalog


class CheckSelection:
    def __init__(self, catalog, *, now=None):
        self.catalog = catalog
        self.tasks = TaskCatalog(catalog, now=now)

    @staticmethod
    def selected(db, task_id: str) -> dict | None:
        # Older catalogs remain readable without introducing schema-four queries.
        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] not in (4, 5):
            return None
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row
        row = cursor.execute('SELECT profile_id,selected_revision,selected_at FROM task_check_profiles WHERE task_id=?', (task_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def policy_profiles(db, task_id, project):
        """Keep task-selected checks separate from mandatory project checks."""
        selected = CheckSelection.selected(db, task_id)
        return dict(required_profile_id=project['required_profile_id'],
                    required_profile_revision=project['required_profile_revision'],
                    required_profile_digest=project['required_profile_digest'],
                    selected_profile_id=selected['profile_id'] if selected else None)

    @staticmethod
    def copy_parent(db, parent_id: str, child_id: str, now: str) -> None:
        selected = CheckSelection.selected(db, parent_id)
        if selected:
            CheckSelection.insert(db, child_id, selected['profile_id'], 1, now)

    @staticmethod
    def insert(db, task_id, profile_id, revision, now):
        from .check_profiles import CheckProfiles
        CheckProfiles.active(db, profile_id)
        db.execute('INSERT INTO task_check_profiles (task_id,profile_id,selected_revision,selected_at) VALUES (?,?,?,?) '
                   'ON CONFLICT(task_id) DO UPDATE SET profile_id=excluded.profile_id,selected_revision=excluded.selected_revision,selected_at=excluded.selected_at',
                   (task_id, profile_id, revision, now))

    def select(self, request: dict) -> dict:
        require_checks(self.catalog)
        params = request['params']
        with self.catalog.connect(write=True) as db:
            cached = self.tasks.lookup_request(request, db)
            if cached is not None:
                return cached
            task = params['task']
            row = self.tasks.assert_revision(db, task, params.get('expected_revision'))
            self.tasks.assert_unclaimed(db, task)
            if row['state'] not in ('created', 'active', 'blocked'):
                raise CoreError('TASK_STATE_INVALID', 'Check selection is frozen for ready or cancelled tasks.',
                                'Resume a ready task, select its checks, then save a new ready result; create a new task if cancelled.')
            revision = row['revision'] + 1
            now = self.tasks.now().isoformat()
            self.insert(db, task, params.get('profile'), revision, now)
            db.execute('UPDATE tasks SET revision=?,updated_at=? WHERE id=?', (revision, now, task))
            result = dict(task_id=task, revision=revision, check_profile=self.selected(db, task))
            self.tasks.event(db, request, 'task.check_profile.selected', row['state'],
                             dict(task_id=task, profile_id=params['profile'], revision=revision))
            self.tasks.save_request(db, request, result)
        self.catalog.notify()
        return result
