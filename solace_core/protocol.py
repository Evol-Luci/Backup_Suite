"""Bounded newline-delimited JSON with strict request validation."""
import hashlib
import json
import uuid
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
    required, optional = PARAMS[method]
    if not required <= params.keys() or params.keys() - required - optional:
        raise invalid('Missing or extra method parameters.')
    for key, item in params.items():
        if key == 'after':
            if type(item) is not int or item < 0:
                raise invalid('The cursor must be a nonnegative integer.')
        elif key == 'exclusions':
            if not isinstance(item, list) or any(not isinstance(x, str) or '\0' in x for x in item):
                raise invalid('Exclusions must be strings.')
        elif not isinstance(item, str) or '\0' in item or (key not in ('message','confirmed_root') and not item):
            raise invalid('Parameter must be a nonempty string.')
    for key in ('root','store','destination'):
        if key in params and not Path(params[key]).is_absolute():
            raise invalid('API filesystem paths must be absolute.')
    return value


def fingerprint(method, params, actor):
    return hashlib.sha256(json.dumps(dict(method=method, params=params, actor=actor), sort_keys=True,
                                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def response(request_id, result=None, error=None):
    value = dict(version=1, id=request_id, ok=error is None)
    value['result' if error is None else 'error'] = result if error is None else error.as_dict()
    return value
