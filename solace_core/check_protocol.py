"""Strict version-one configured-check request schemas."""
import re
from .errors import invalid

CHECK_PARAMS = {
 'project.check-profile.list': ({'project'}, {'after','limit'}),
 'project.check-profile.show': ({'project','profile_id'}, set()),
 'project.check-profile.preview': ({'project','action'}, {'profile_id','definition'}),
 'project.check-profile.apply': ({'project','action','plan_token','confirmed'}, {'profile_id','definition'}),
 'task.check-profile.select': ({'project','task','profile','expected_revision'}, set()),
 'integration.check.run': ({'project','plan','revision','profile'}, set()),
 'integration.check.show': ({'project','run'}, set()),
 'integration.check.list': ({'project','plan'}, {'after','limit'}),
 'integration.check.cancel': ({'project','run'}, set()),
}

def validate_check(method: str, params: dict) -> None:
    required, optional = CHECK_PARAMS[method]
    if not required <= params.keys() or params.keys()-required-optional: raise invalid('Missing or extra check parameters.')
    for key,value in params.items():
        if key in ('project','profile_id','profile','task','plan','run'):
            if not isinstance(value,str) or re.fullmatch(r'[0-9a-f]{32}',value) is None: raise invalid('Check IDs must be exact lowercase UUID hex strings.')
        elif key in ('revision','expected_revision','after','limit'):
            if type(value) is not int or value < (0 if key=='after' else 1) or value>2**63-1 or (key=='limit' and value>256): raise invalid('Invalid check revision or page bounds.')
        elif key=='plan_token':
            if not isinstance(value,str) or re.fullmatch(r'[0-9a-f]{32}:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',value) is None: raise invalid('Invalid preview token.')
        elif key=='confirmed':
            if value is not True: raise invalid('Explicit confirmation is required.')
        elif key=='action':
            if value not in ('create','update','retire'): raise invalid('Invalid profile action.')
        elif key=='definition':
            from .check_profiles import validate_profile
            validate_profile(value)
    if method in ('project.check-profile.preview','project.check-profile.apply'):
        action=params['action']
        if ('profile_id' in params)!=(action in ('update','retire')) or ('definition' in params)!=(action in ('create','update')): raise invalid('Profile action requires its exact fields.')
