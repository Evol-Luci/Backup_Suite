"""Bounded newline-delimited JSON with strict request validation."""
import hashlib
import json
import uuid
import re
from pathlib import Path
from .errors import CoreError, invalid

MAX_FRAME = 1024 * 1024
METHODS = {'core.status', 'doctor', 'project.add', 'project.list', 'project.show',
           'project.relink', 'project.unregister', 'checkpoint.create', 'checkpoint.list',
           'checkpoint.verify', 'checkpoint.restore', 'checkpoint.usage', 'operation.show', 'event.subscribe'}
PARAMS = {
    'core.status': (set(), set()), 'doctor': (set(), {'project'}),
    'project.add': ({'root'}, {'store'}), 'project.list': (set(), set()),
    'project.show': ({'project'}, set()), 'project.relink': ({'project', 'root', 'store', 'confirmed_root'}, set()),
    'project.unregister': ({'project'}, set()), 'checkpoint.create': ({'project', 'message'}, {'exclusions'}),
    'checkpoint.list': ({'project'}, set()), 'checkpoint.verify': ({'project', 'checkpoint'}, set()),
    'checkpoint.restore': ({'project', 'checkpoint', 'destination'}, set()),
    'checkpoint.usage': ({'project'}, set()), 'operation.show': ({'project', 'operation'}, set()),
    'event.subscribe': ({'project'}, {'after'}),
}

TASK_PARAMS = {
    'project.upgrade': ({'project','preview'},{'plan_token'}),
    'project.prompt-policy.show': ({'project'},set()),
    'project.prompt-policy.set': ({'project','policy','expected_revision'},{'days'}),
    'task.create': ({'project','title'},{'parent','expected_parent_revision','prompt','prompt_policy','prompt_days','exclusions'}),
    'task.list': ({'project'},{'parent','after','limit'}), 'task.show': ({'project','task'},set()),
    'task.start': ({'project','task','expected_revision'},{'from_checkpoint','expected_parent_revision'}),
    'task.block': ({'project','task','expected_revision','reason'},set()),
    'task.resume': ({'project','task','expected_revision'},set()),
    'task.cancel': ({'project','task','expected_revision'},set()),
    'workspace.relink': ({'project','task','expected_revision','path','confirmed_path','checkpoint','writers_paused'},set()),
    'task.ready': ({'project','task','expected_revision'},set()),
    'task.prompt.show': ({'project','task'},set()),
    'task.prompt.delete': ({'project','task','expected_revision'},set()),
    'workspace.status': ({'project','task'},{'refresh','after','limit','observation'}),
    'workspace.recovery.show': ({'project','operation'},set()),
    'workspace.recovery.resume': ({'project','operation','task','expected_revision'},set()),
    'workspace.recovery.abandon': ({'project','operation','task','expected_revision'},set()),
    'workspace.staging.cleanup': ({'project','operation','preview'},{'plan_token'}),
    'workspace.checkpoint': ({'project','task','expected_revision','message'},set()),
}
PARAMS.update(TASK_PARAMS)
PARAMS['checkpoint.list']=({'project'},{'all_workspaces'})
METHODS.update(TASK_PARAMS)
from .integration_protocol import INTEGRATION_PARAMS, validate_integration
PARAMS.update(INTEGRATION_PARAMS)
METHODS.update(INTEGRATION_PARAMS)
from .check_protocol import CHECK_PARAMS, validate_check
PARAMS.update(CHECK_PARAMS)
METHODS.update(CHECK_PARAMS)


def encode_frame(value):
    try:
        data = (json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as error:
        raise invalid() from error
    if len(data) > MAX_FRAME:
        raise invalid('The response or request exceeds the frame limit.')
    return data


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def decode_frame(data):
    if len(data) > MAX_FRAME or not data.endswith(b'\n'):
        raise invalid('Invalid JSON frame size or delimiter.')
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise invalid('The frame must contain finite UTF-8 JSON.') from error
    if not isinstance(value, dict):
        raise invalid('A frame must be a JSON object.')
    return value


def validate_request(value):
    if set(value) != {'version', 'id', 'method', 'params', 'actor'}:
        raise invalid('The request envelope has missing or extra fields.')
    if type(value['version']) is not int:
        raise invalid('The protocol version must be an integer.')
    if value['version'] != 1:
        raise CoreError('PROTOCOL_VERSION_UNSUPPORTED', 'This protocol version is unsupported.', 'Use a version 1 client.')
    try:
        if not isinstance(value['id'], str) or len(value['id']) > 36:
            raise ValueError()
        uuid.UUID(value['id'])
    except (ValueError, AttributeError):
        raise invalid('The request ID must be a UUID.')
    actor = value['actor']
    if not isinstance(actor, str) or not actor or len(actor) > 128 or not actor.isprintable():
        raise invalid('Actor must be a short printable label.')
    method, params = value['method'], value['params']
    if not isinstance(method, str) or method not in METHODS or not isinstance(params, dict):
        raise invalid('Unknown method or invalid parameters.')
    if method in CHECK_PARAMS:
        validate_check(method, params)
        return value
    if method in INTEGRATION_PARAMS:
        validate_integration(method, params)
        return value
    required, optional = PARAMS[method]
    if not required <= params.keys() or params.keys() - required - optional:
        raise invalid('Missing or extra method parameters.')
    for key, item in params.items():
        if key in ('preview','refresh','all_workspaces','writers_paused'):
            if type(item) is not bool: raise invalid('Expected a boolean parameter.')
        elif key in ('expected_revision','expected_parent_revision','days','prompt_days'):
            if type(item) is not int or item<1: raise invalid('Expected a positive integer parameter.')
        elif key == 'prompt':
            from .prompts import validate_prompt
            if not isinstance(item,str): raise invalid('Prompt must be text.')
            validate_prompt(item)
        elif key == 'after':
            if type(item) is not int or item < 0:
                raise invalid('The cursor must be a nonnegative integer.')
        elif key == 'limit':
            if type(item) is not int or not 1<=item<=256:raise invalid('Page size must be an integer from 1 to 256.')
        elif key == 'exclusions':
            if not isinstance(item, list) or any(not isinstance(x, str) or '\0' in x for x in item):
                raise invalid('Exclusions must be strings.')
        elif not isinstance(item, str) or '\0' in item or (key not in ('message','confirmed_root') and not item):
            raise invalid('Parameter must be a nonempty string.')
    for key in ('root','store','destination','path','confirmed_path'):
        if key in params and not Path(params[key]).is_absolute():
            raise invalid('API filesystem paths must be absolute.')
    if method in TASK_PARAMS:
        if method=='workspace.status':
            if params.get('refresh') and any(key in params for key in ('after','limit','observation')):
                raise invalid('Refresh does not accept cached observation paging arguments.')
            if params.get('after',0)>0 and 'observation' not in params:
                raise invalid('Subsequent change pages require the observation token.')
            if 'observation' in params and re.fullmatch(r'[0-9a-f]{64}',params['observation']) is None:
                raise invalid('Observation tokens must be exact SHA256 hex strings.')
        for key in ('project','task','parent','operation','checkpoint','from_checkpoint'):
            if key in params and re.fullmatch(r'[0-9a-f]{32}',params[key]) is None:
                raise invalid('Task API identifiers must be exact lowercase UUID hex strings.')
        if method in ('project.upgrade','workspace.staging.cleanup'):
            if params['preview'] and 'plan_token' in params:
                raise invalid('Preview does not accept an apply token.')
            if not params['preview'] and 'plan_token' not in params:
                raise invalid('Apply requires the reviewed preview token.')
        if 'plan_token' in params and re.fullmatch(r'[0-9a-f]{64}',params['plan_token']) is None:
            raise invalid('Plan tokens must be exact SHA256 hex strings.')
        if method=='task.create':
            from .tasks import profile
            title=params['title']
            if not title.strip() or len(title)>200 or not title.isprintable():raise invalid('Title must contain 1–200 printable characters.')
            if ('parent' in params)!=('expected_parent_revision' in params):raise invalid('Child creation requires its parent revision.')
            if 'parent' in params and 'exclusions' in params:raise invalid('Children inherit their capture profile.')
            if 'exclusions' in params:profile(params['exclusions'])
            if 'prompt_policy' in params or 'prompt_days' in params:
                from .prompts import validate_policy
                validate_policy(params.get('prompt_policy'),params.get('prompt_days'))
        if method=='project.prompt-policy.set':
            from .prompts import validate_policy
            validate_policy(params['policy'],params.get('days'))
        if method=='task.block' and len(params['reason'])>1024:raise invalid('Block reason is too long.')
        if method=='workspace.relink' and params['writers_paused'] is not True:
            raise CoreError('CONFIRMATION_REQUIRED','External writers must be paused for relink.','Pause them and explicitly acknowledge it.')
    return value


def fingerprint(method, params, actor):
    return hashlib.sha256(json.dumps(dict(method=method, params=params, actor=actor), sort_keys=True,
                                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def response(request_id, result=None, error=None):
    value = dict(version=1, id=request_id, ok=error is None)
    value['result' if error is None else 'error'] = result if error is None else error.as_dict()
    return value
