"""Strict wire grammar for owner-confirmed policy and read-only queue inspection."""
import re
from .errors import invalid

POLICY_PARAMS = {
    'project.policy.show': ({'project'}, set()),
    'project.policy.preview': ({'project','mode','allow_task_opt_in'}, {'required_profile'}),
    'project.policy.apply': ({'project','mode','allow_task_opt_in','plan_token','confirmed'}, {'required_profile'}),
    'task.policy.show': ({'project','task'}, set()),
    'task.policy.preview': ({'project','task','mode','expected_revision'}, set()),
    'task.policy.apply': ({'project','task','mode','expected_revision','plan_token','confirmed'}, set()),
    'integration.queue.list': ({'project'}, {'after','limit'}),
    'integration.queue.show': ({'project','intent'}, set()),
}


def validate_policy(method, params):
    required, optional = POLICY_PARAMS[method]
    if not required <= params.keys() or params.keys() - required - optional:
        raise invalid('Missing or extra policy parameters.')
    for key, value in params.items():
        if key in ('project','task','intent','required_profile'):
            if not isinstance(value,str) or re.fullmatch(r'[0-9a-f]{32}',value) is None:
                raise invalid('Policy identifiers must be exact lowercase UUID hex strings.')
        elif key in ('confirmed','allow_task_opt_in'):
            if type(value) is not bool: raise invalid('Policy confirmation and opt-in must be booleans.')
            if key=='confirmed' and value is not True: raise invalid('Apply requires explicit owner confirmation.')
        elif key in ('after','limit','expected_revision'):
            if type(value) is not int or value < (0 if key=='after' else 1) or (key=='limit' and value>256):
                raise invalid('Invalid policy revision or queue page bound.')
        elif key=='mode':
            choices=('inherit','manual','checked_queue') if method.startswith('task.') else ('manual','checked_queue')
            if not isinstance(value,str) or value not in choices: raise invalid('Invalid integration policy mode.')
        elif key=='plan_token':
            if not isinstance(value,str) or re.fullmatch(r'[0-9a-f]{32}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',value) is None:
                raise invalid('Use the exact token issued by policy preview.')
    if method.startswith('project.policy.') and (params.get('mode')=='checked_queue' or params.get('allow_task_opt_in')) and 'required_profile' not in params:
        raise invalid('Checked queue or task opt-in requires an approved required profile.')
