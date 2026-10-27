"""Additive storage for explicit integration policy and durable intents."""
from datetime import datetime, timezone

DDL = """
CREATE TABLE project_integration_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL CHECK(revision>=1),
 mode TEXT NOT NULL CHECK(mode IN ('manual','checked_queue')),
 required_profile_id TEXT, required_profile_revision INTEGER, required_profile_digest TEXT,
 allow_task_opt_in INTEGER NOT NULL CHECK(allow_task_opt_in IN (0,1)),
 actor TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(required_profile_id,required_profile_revision) REFERENCES check_profile_revisions(profile_id,revision),
 CHECK((required_profile_id IS NULL AND required_profile_revision IS NULL AND required_profile_digest IS NULL)
    OR (required_profile_id IS NOT NULL AND required_profile_revision IS NOT NULL AND required_profile_digest IS NOT NULL))
);
CREATE TABLE task_integration_policy (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), mode TEXT NOT NULL CHECK(mode IN ('inherit','manual','checked_queue')),
 project_policy_revision INTEGER NOT NULL, selected_at TEXT NOT NULL
);
CREATE TABLE integration_policy_intents (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), result_checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
 target_key TEXT NOT NULL, target_revision INTEGER, task_revision INTEGER NOT NULL,
 policy_revision INTEGER NOT NULL, profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL, profile_digest TEXT NOT NULL,
 stage TEXT NOT NULL, prepare_request_id TEXT NOT NULL UNIQUE, check_request_id TEXT NOT NULL UNIQUE,
 prepare_operation_id TEXT, check_operation_id TEXT, plan_id TEXT, plan_revision INTEGER, run_id TEXT,
 reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(task_id,result_checkpoint_id,target_key)
);
"""

def create_schema_v5(db):
    for statement in DDL.split(';'):
        if statement.strip():
            db.execute(statement)
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO project_integration_policy VALUES (1,1,'manual',NULL,NULL,NULL,0,'system',?)", (now,))


def require_policy(catalog):
    from .schema import schema_version
    from .errors import CoreError
    if schema_version(catalog) != 5:
        raise CoreError('PROJECT_UPGRADE_REQUIRED', 'This project needs the integration policy catalog upgrade.',
                        'Review project upgrade --preview before applying its token.')
