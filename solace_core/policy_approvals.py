"""Transaction-local project policy authentication for human approvals."""
from .check_profiles import CheckProfiles
from .integration_claims import stale
from .project_policy import ProjectPolicy


class PolicyApprovals:
    @staticmethod
    def required(db, task_id):
        # A manual task override never weakens the project-required check rule.
        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] != 5:
            return None
        policy = ProjectPolicy.current(db)
        return policy if policy['required_profile_id'] is not None else None

    @staticmethod
    def stamp(db, selected_run):
        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] != 5:
            return {}
        policy = ProjectPolicy.current(db)
        required = policy['required_profile_id'] is not None
        decision = dict(project_policy_revision=policy['revision'],
                        **{key: policy[key] for key in ('required_profile_id', 'required_profile_revision', 'required_profile_digest')},
                        required_checks_met=required)
        if required:
            if not isinstance(selected_run, dict) or selected_run.get('status') != 'passed':
                stale('Project policy requires passing checks; unchecked approval is not allowed.')
            profile = CheckProfiles.active(db, policy['required_profile_id'])
            for field in ('profile_id', 'profile_revision', 'profile_digest'):
                if selected_run.get(field) != policy['required_' + field]:
                    stale('The selected check run does not match the project-required profile.')
            if profile['revision'] != policy['required_profile_revision'] or profile['digest'] != policy['required_profile_digest']:
                stale('The project-required profile changed after policy confirmation.')
        return decision

    @staticmethod
    def verify(db, approval_binding, plan):
        if db.execute('SELECT schema_version FROM core_metadata').fetchone()[0] != 5:
            return {}
        checks = approval_binding.get('checks')
        if isinstance(checks, dict):
            from .check_approvals import CheckApprovals
            if CheckApprovals.evidence(db, checks.get('run_id'), plan) != checks:
                stale('The policy check evidence changed.')
        decision = PolicyApprovals.stamp(db, checks)
        if approval_binding.get('policy') != decision:
            stale('Project policy changed after approval; review and approve a fresh plan.')
        return decision
