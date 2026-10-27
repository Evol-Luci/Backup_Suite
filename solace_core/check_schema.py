"""Additive schema-four storage for configured checks; no execution on upgrade."""
import sqlite3

_DIGEST = "length({0})=64 AND {0} NOT GLOB '*[^0-9a-f]*'"
DDL = """
CREATE TABLE check_profiles (
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name)>0),
 active_revision INTEGER NOT NULL CHECK(active_revision>=1),
 retired INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(id,active_revision) REFERENCES check_profile_revisions(profile_id,revision)
 DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE check_profile_revisions (
 profile_id TEXT NOT NULL REFERENCES check_profiles(id), revision INTEGER NOT NULL CHECK(revision>=1),
 digest TEXT NOT NULL CHECK(DIGEST_digest), definition_json TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(profile_id,revision)
);
CREATE TABLE task_check_profiles (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), profile_id TEXT REFERENCES check_profiles(id),
 selected_revision INTEGER NOT NULL CHECK(selected_revision>=1), selected_at TEXT NOT NULL
);
CREATE TABLE check_runs (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(id), candidate_id TEXT NOT NULL REFERENCES checkpoints(id),
 manifest_sha256 TEXT NOT NULL CHECK(DIGEST_manifest_sha256),
 capture_profile_json TEXT NOT NULL, expected_json TEXT NOT NULL,
 profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL,
 profile_digest TEXT NOT NULL CHECK(DIGEST_profile_digest),
 state TEXT NOT NULL CHECK(state IN ('reserved','running','passed','failed','timed_out','cancelled','interrupted','stale')),
 staging_path TEXT NOT NULL UNIQUE, staging_identity_json TEXT,
 cleanup_state TEXT NOT NULL DEFAULT 'pending' CHECK(cleanup_state IN ('pending','cleaned','unsafe')),
 result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision),
 FOREIGN KEY(profile_id,profile_revision) REFERENCES check_profile_revisions(profile_id,revision)
);
CREATE TABLE check_steps (
 run_id TEXT NOT NULL REFERENCES check_runs(id), ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 state TEXT NOT NULL CHECK(state IN ('pending','running','passed','failed','timed_out','cancelled','interrupted','launch_error','skipped')),
 argv_json TEXT NOT NULL, timeout_seconds REAL NOT NULL CHECK(timeout_seconds>0),
 output_cap INTEGER NOT NULL CHECK(output_cap>0),
 started_at TEXT, finished_at TEXT, exit_code INTEGER, signal INTEGER,
 stdout_sha256 TEXT CHECK(stdout_sha256 IS NULL OR DIGEST_stdout_sha256),
 stderr_sha256 TEXT CHECK(stderr_sha256 IS NULL OR DIGEST_stderr_sha256),
 stdout_bytes INTEGER NOT NULL DEFAULT 0 CHECK(stdout_bytes>=0),
 stderr_bytes INTEGER NOT NULL DEFAULT 0 CHECK(stderr_bytes>=0),
 stdout_path TEXT, stderr_path TEXT, details_json TEXT,
 PRIMARY KEY(run_id,ordinal)
);
CREATE TABLE integration_checked_approvals (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
 run_id TEXT NOT NULL REFERENCES check_runs(id), token_sha256 TEXT NOT NULL UNIQUE CHECK(DIGEST_token_sha256),
 binding_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE INDEX check_runs_plan ON check_runs(plan_id,revision,created_at);
CREATE INDEX check_runs_state ON check_runs(state);
"""
for _column in ('digest','manifest_sha256','profile_digest','stdout_sha256','stderr_sha256','token_sha256'):
    DDL = DDL.replace('DIGEST_' + _column, _DIGEST.format(_column))
for _table in ('check_profile_revisions','integration_checked_approvals'):
    for _action in ('UPDATE','DELETE'):
        DDL += f"CREATE TRIGGER {_table}_no_{_action.lower()} BEFORE {_action} ON {_table} BEGIN SELECT RAISE(ABORT,'check evidence is immutable'); END;\n"
DDL += """
CREATE TRIGGER check_runs_terminal_binding BEFORE UPDATE ON check_runs
WHEN OLD.state NOT IN ('reserved','running') AND (
 NEW.id IS NOT OLD.id OR NEW.plan_id IS NOT OLD.plan_id OR NEW.revision IS NOT OLD.revision
 OR NEW.task_id IS NOT OLD.task_id OR NEW.candidate_id IS NOT OLD.candidate_id
 OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256 OR NEW.capture_profile_json IS NOT OLD.capture_profile_json
 OR NEW.expected_json IS NOT OLD.expected_json OR NEW.profile_id IS NOT OLD.profile_id
 OR NEW.profile_revision IS NOT OLD.profile_revision OR NEW.profile_digest IS NOT OLD.profile_digest
 OR NEW.state IS NOT OLD.state OR NEW.result_json IS NOT OLD.result_json
 OR NEW.staging_path IS NOT OLD.staging_path OR NEW.staging_identity_json IS NOT OLD.staging_identity_json
 OR NEW.created_at IS NOT OLD.created_at)
BEGIN SELECT RAISE(ABORT,'terminal check evidence is immutable'); END;
CREATE TRIGGER check_runs_no_delete BEFORE DELETE ON check_runs
BEGIN SELECT RAISE(ABORT,'check evidence is immutable'); END;
CREATE TRIGGER check_steps_terminal_update BEFORE UPDATE ON check_steps
WHEN (SELECT state FROM check_runs WHERE id=OLD.run_id) NOT IN ('reserved','running')
BEGIN SELECT RAISE(ABORT,'terminal check evidence is immutable'); END;
CREATE TRIGGER check_steps_immutable_key BEFORE UPDATE ON check_steps
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.ordinal IS NOT OLD.ordinal
BEGIN SELECT RAISE(ABORT,'check evidence identity is immutable'); END;
CREATE TRIGGER check_steps_terminal_delete BEFORE DELETE ON check_steps
WHEN (SELECT state FROM check_runs WHERE id=OLD.run_id) NOT IN ('reserved','running')
BEGIN SELECT RAISE(ABORT,'terminal check evidence is immutable'); END;
CREATE TRIGGER check_steps_terminal_insert BEFORE INSERT ON check_steps
WHEN (SELECT state FROM check_runs WHERE id=NEW.run_id) NOT IN ('reserved','running')
BEGIN SELECT RAISE(ABORT,'terminal check evidence is immutable'); END;
"""


def create_schema_v4(db: sqlite3.Connection) -> None:
    statement = ''
    for line in DDL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete checks schema statement')
