"""Additive schema-3 DDL, executed inside the caller's transaction."""
import sqlite3

DDL = """
CREATE TABLE integration_plans (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 result_id TEXT NOT NULL REFERENCES checkpoints(id), target_key TEXT NOT NULL,
 target_task_id TEXT REFERENCES tasks(id),
 state TEXT NOT NULL CHECK(state IN
 ('conflicted','prepared','approved','applying','applied','recovery_required','restored','abandoned')),
 current_revision INTEGER NOT NULL CHECK(current_revision>=1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE integration_revisions (
 plan_id TEXT NOT NULL REFERENCES integration_plans(id), revision INTEGER NOT NULL CHECK(revision>=1),
 base_id TEXT NOT NULL REFERENCES checkpoints(id),
 incoming_id TEXT NOT NULL REFERENCES checkpoints(id),
 current_id TEXT NOT NULL REFERENCES checkpoints(id),
 candidate_id TEXT NOT NULL REFERENCES checkpoints(id),
 previous_receipt_id TEXT REFERENCES integration_receipts(id),
 profile_json TEXT NOT NULL, expected_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 review_token TEXT NOT NULL, validated_at TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(plan_id,revision)
);
CREATE TABLE integration_decisions (
 plan_id TEXT NOT NULL, revision INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 path TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT, decision_json TEXT NOT NULL,
 PRIMARY KEY(plan_id,revision,ordinal), UNIQUE(plan_id,revision,path),
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE TABLE integration_approvals (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
 token_sha256 TEXT NOT NULL UNIQUE, binding_json TEXT NOT NULL,
 actor TEXT NOT NULL, accepted_unchecked INTEGER NOT NULL CHECK(accepted_unchecked=1),
 created_at TEXT NOT NULL,
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE TABLE integration_claims (
 path TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
 kind TEXT NOT NULL CHECK(kind IN ('target','journal','export')),
 identity_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE integration_journals (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id), plan_id TEXT NOT NULL,
 revision INTEGER NOT NULL, journal_path TEXT NOT NULL UNIQUE, identity_json TEXT,
 target_identity_json TEXT NOT NULL, intent_json TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN
 ('reserved','staged','applying','files_ready','committed','recovery_required','restored','abandoned')),
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE TABLE integration_actions (
 operation_id TEXT NOT NULL REFERENCES integration_journals(operation_id),
 ordinal INTEGER NOT NULL, path TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('mkdir','move_old','publish_new','rmdir')),
 phase TEXT NOT NULL CHECK(phase IN ('planned','intent','done')),
 old_json TEXT, new_json TEXT, slot TEXT, observed_json TEXT,
 PRIMARY KEY(operation_id,ordinal)
);
CREATE TABLE integration_receipts (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(id),
 result_id TEXT NOT NULL REFERENCES checkpoints(id), target_key TEXT NOT NULL,
 operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id),
 before_id TEXT NOT NULL REFERENCES checkpoints(id),
 after_id TEXT NOT NULL REFERENCES checkpoints(id),
 details_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(task_id,result_id,target_key),
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE TABLE integration_recoveries (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 original_operation_id TEXT NOT NULL REFERENCES integration_journals(operation_id),
 mode TEXT NOT NULL CHECK(mode IN ('resume','restore-before')),
 result_json TEXT, location_json TEXT NOT NULL
);
CREATE TABLE integration_refs (
 checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id), owner_id TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('base','incoming','current','candidate','before','after','export')),
 created_at TEXT NOT NULL, PRIMARY KEY(checkpoint_id,owner_id,role)
);
CREATE TABLE integration_exports (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
 path TEXT NOT NULL UNIQUE, identity_json TEXT,
 phase TEXT NOT NULL CHECK(phase IN ('reserved','staged','published','interrupted')),
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE INDEX integration_receipts_target ON integration_receipts(task_id,target_key,created_at);
CREATE INDEX integration_actions_phase ON integration_actions(operation_id,phase);
CREATE INDEX integration_claims_operation ON integration_claims(operation_id);
CREATE TRIGGER integration_receipts_no_update BEFORE UPDATE ON integration_receipts
BEGIN SELECT RAISE(ABORT,'integration receipts are immutable'); END;
CREATE TRIGGER integration_receipts_no_delete BEFORE DELETE ON integration_receipts
BEGIN SELECT RAISE(ABORT,'integration receipts are immutable'); END;
"""


def create_schema_v3(db: sqlite3.Connection) -> None:
    statement = ''
    for line in DDL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete integration schema statement')
