"""Checked approval evidence and transaction-local reauthentication."""
import hashlib
import json
import secrets
import uuid
from .check_profiles import CheckProfiles
from .check_selection import CheckSelection
from .integration_claims import stale
from .integration_snapshots import canonical


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

class CheckApprovals:
    def __init__(self, service, catalog):
        self.service, self.catalog = service, catalog

    @staticmethod
    def evidence(db, run_id, plan):
        row = db.execute('SELECT * FROM check_runs WHERE id=?',(run_id,)).fetchone()
        if not row or row['state'] != 'passed': stale('The selected check run did not pass.')
        run = dict(row)
        result = json.loads(run['result_json'] or '{}')
        saved = result.get('binding')
        revision = db.execute('SELECT * FROM integration_revisions WHERE plan_id=? AND revision=?',(plan.id,plan.revision)).fetchone()
        task = db.execute('SELECT revision,state FROM tasks WHERE id=?',(plan.task_id,)).fetchone()
        try:
            profile = CheckProfiles.active(db,run['profile_id'])
            selected = CheckSelection.selected(db,plan.task_id)
            fresh = dict(plan_id=plan.id,revision=plan.revision,review_token=revision['review_token'],
                candidate_id=revision['candidate_id'],manifest_sha256=revision['manifest_sha256'],
                capture_profile_sha256=digest(json.loads(revision['profile_json'])),expected_sha256=digest(json.loads(revision['expected_json'])),
                task_id=plan.task_id,task_revision=task['revision'],profile_id=profile['profile_id'],
                profile_revision=profile['revision'],profile_digest=profile['digest'])
            steps = [dict(r) for r in db.execute('SELECT * FROM check_steps WHERE run_id=? ORDER BY ordinal',(run_id,))]
            if (digest(profile['definition']) != profile['digest'] or saved != fresh or not selected or selected['profile_id'] != profile['profile_id'] or task['state'] != 'ready'
                or any(run[k] != fresh[k] for k in ('plan_id','revision','task_id','candidate_id','manifest_sha256','profile_id','profile_revision','profile_digest'))
                or json.loads(run['capture_profile_json']) != json.loads(revision['profile_json'])
                or json.loads(run['expected_json']) != json.loads(revision['expected_json'])
                or len(steps) != len(profile['definition']['steps'])): stale('The check binding is no longer current.')
            summaries = []
            for index, (step, definition) in enumerate(zip(steps,profile['definition']['steps'])):
                details = json.loads(step['details_json'] or '{}')
                if (step['ordinal'] != index or step['state'] != 'passed' or details.get('output_complete') is not True
                    or json.loads(step['argv_json']) != definition['argv'] or step['timeout_seconds'] != definition['timeout_seconds']
                    or step['output_cap'] != definition['output_limit_bytes']): stale('The check step evidence changed.')
                summary = {k:step[k] for k in ('ordinal','state','timeout_seconds','output_cap','started_at','finished_at','exit_code','signal','stdout_sha256','stderr_sha256','stdout_bytes','stderr_bytes')}
                summary.update(name=definition['name'],argv_sha256=digest(json.loads(step['argv_json'])),evidence_sha256=digest(step))
                summaries.append(summary)
            return dict(status='passed',run_id=run_id,profile_id=profile['profile_id'],profile_revision=profile['revision'],
                        profile_digest=profile['digest'],candidate_digest=plan.candidate.manifest_sha256,binding=saved,steps=summaries)
        except (KeyError, TypeError, ValueError, IndexError):
            stale('The retained check evidence is malformed.')

    def approve(self, request, plan, binding):
        p = request['params']
        with self.catalog.connect(write=True) as db:
            header = db.execute('SELECT current_revision,state FROM integration_plans WHERE id=?',(plan.id,)).fetchone()
            if not header or header['current_revision'] != plan.revision or header['state'] != 'prepared': stale('The plan changed during approval.')
            evidence = self.evidence(db,p['check_run'],plan)
            if evidence['binding']['review_token'] != binding['review_token'] or evidence['binding']['expected_sha256'] != digest(binding['expected']): stale('The check binding changed during approval.')
            from .tasks import TaskCatalog
            tasks = TaskCatalog(self.catalog)
            cached = tasks.lookup_request(request,db)
            if cached is not None: return cached
            token = secrets.token_hex(32); now = self.catalog._now()
            checked = dict(binding,checks=evidence)
            db.execute('INSERT INTO integration_checked_approvals VALUES (?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,plan.id,plan.revision,p['check_run'],hashlib.sha256(token.encode()).hexdigest(),canonical(checked),request['actor'],now))
            db.execute("UPDATE integration_plans SET state='approved',updated_at=? WHERE id=?",(now,plan.id))
            db.execute('UPDATE integration_revisions SET validated_at=? WHERE plan_id=? AND revision=?',(now,plan.id,plan.revision))
            result = dict(plan_id=plan.id,revision=plan.revision,manifest_sha256=plan.candidate.manifest_sha256,approval=token,checks=evidence)
            tasks.event(db,request,'integration.approved','succeeded',dict(plan_id=plan.id,revision=plan.revision))
            tasks.save_request(db,request,result)
        self.catalog.notify()
        return result

    @staticmethod
    def lookup(db, plan_id, revision, raw_token):
        hashed = hashlib.sha256(raw_token.encode()).hexdigest()
        rows = []
        for table in ('integration_approvals','integration_checked_approvals') if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] in (4, 5) else ('integration_approvals',):
            row = db.execute('SELECT * FROM '+table+' WHERE token_sha256=?',(hashed,)).fetchone()
            if row: rows.append((table,row))
        if len(rows)!=1: stale('The supplied approval is missing or ambiguous.')
        table,row = rows[0]
        if row['plan_id'] != plan_id or row['revision'] != revision or not secrets.compare_digest(row['token_sha256'],hashed) or (table=='integration_approvals' and row['accepted_unchecked'] != 1): stale('The supplied approval does not authorize this plan revision.')
        return dict(id=row['id'],token_sha256=row['token_sha256'],binding=json.loads(row['binding_json']))

    @staticmethod
    def verify(db, selected, plan):
        row = db.execute('SELECT * FROM integration_checked_approvals WHERE id=?',(selected['id'],)).fetchone()
        if not row or row['plan_id'] != plan.id or row['revision'] != plan.revision or selected != dict(id=row['id'],token_sha256=row['token_sha256'],binding=json.loads(row['binding_json'])): stale('The checked approval changed.')
        evidence = CheckApprovals.evidence(db,row['run_id'],plan)
        if selected['binding'].get('checks') != evidence: stale('The retained check evidence changed.')
        return evidence
