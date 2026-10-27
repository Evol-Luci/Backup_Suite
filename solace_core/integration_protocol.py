"""Strict version-one integration parameter schemas; no filesystem access."""
import re
from .errors import CoreError, invalid

INTEGRATION_PARAMS: dict[str, tuple[set[str], set[str]]] = {
    'integration.prepare': ({'project','task','result','expected_revision'}, {'expected_target_revision'}),
    'integration.show': ({'project','plan'}, set()),
    'integration.diff': ({'project','plan'}, {'revision','after','limit','path','version','offset'}),
    'integration.resolve': ({'project','plan','revision','path'}, {'take','from_file'}),
    'integration.export': ({'project','plan','revision','destination'}, set()),
    'integration.approve': ({'project','plan','revision','token'}, {'accept_unchecked','check_run'}),
    'integration.apply': ({'project','plan','revision','approval','writers_paused'}, set()),
    'integration.receipt': ({'project','receipt'}, {'after','limit'}),
    'integration.abandon': ({'project','plan','revision'}, set()),
    'integration.recovery.show': ({'project','operation'}, {'after','limit'}),
    'integration.recovery.resume': ({'project','operation','writers_paused'}, {'target_path','journal_path','confirmed_target','confirmed_journal'}),
    'integration.recovery.restore-before': ({'project','operation','writers_paused'}, {'target_path','journal_path','confirmed_target','confirmed_journal'}),
}
_IDS = {'project','task','result','plan','receipt','operation','check_run'}
_POSITIVE = {'revision','expected_revision','expected_target_revision'}
_ABSOLUTE = {'from_file','destination','target_path','journal_path','confirmed_target','confirmed_journal'}
_GROUP = {'target_path','journal_path','confirmed_target','confirmed_journal'}


def validate_integration(method: str, params: dict) -> None:
    required, optional = INTEGRATION_PARAMS[method]
    if not required <= params.keys() or params.keys() - required - optional:
        raise invalid('Missing or extra integration parameters.')
    for key, value in params.items():
        if key in _POSITIVE | {'after','offset','limit'}:
            minimum = 1 if key in _POSITIVE | {'limit'} else 0
            if type(value) is not int or value < minimum or (key=='limit' and value>256):
                raise invalid('Integration revisions and page cursors must be integers in their permitted range.')
        elif key in ('writers_paused','accept_unchecked'):
            if type(value) is not bool: raise invalid('Integration acknowledgements must be booleans.')
            if value is not True:
                code = 'INTEGRATION_UNCHECKED' if key=='accept_unchecked' else 'CONFIRMATION_REQUIRED'
                raise CoreError(code,'Explicit integration acknowledgement is required.',
                                'Accept unchecked integration or pause external writers and acknowledge it.')
        elif not isinstance(value,str) or not value or '\0' in value:
            raise invalid('Integration text parameters must be nonempty strings without NUL.')
        elif key in _IDS and re.fullmatch('[0-9a-f]{32}',value) is None:
            raise invalid('Integration IDs must be exact lowercase UUID hex strings.')
        elif key in ('approval','token') and re.fullmatch('[0-9a-f]{64}',value) is None:
            raise invalid('Integration tokens must be exact lowercase SHA256 hex strings.')
        elif key=='path' and (value.startswith('/') or any(p in ('','.','..') for p in value.split('/'))):
            raise invalid('Integration paths must be engine-relative without empty or dot components.')
        elif key in _ABSOLUTE and (not value.startswith('/') or '..' in value.split('/')):
            raise invalid('Integration filesystem paths must be absolute without dot-dot components.')
        elif key=='take' and value not in ('current','incoming','delete'):
            raise invalid('Take must be current, incoming or delete.')
        elif key=='version' and value not in ('base','current','incoming','candidate'):
            raise invalid('Content version must be base, current, incoming or candidate.')
    if method=='integration.approve' and (('accept_unchecked' in params)==('check_run' in params)):
        raise invalid('Approval requires exactly one of accept_unchecked or check_run.')
    if method=='integration.resolve' and (('take' in params)==('from_file' in params)):
        raise invalid('Resolve requires exactly one of take and from_file.')
    if method=='integration.diff':
        if 'path' in params:
            if not {'revision','version'} <= params.keys() or {'after','limit'} & params.keys():
                raise invalid('Content pages require revision and version, without metadata cursors.')
        elif {'offset','version'} & params.keys() or (params.get('after',0)>0 and 'revision' not in params):
            raise invalid('Metadata continuation requires revision; offset and version require content mode.')
    supplied = _GROUP & params.keys()
    if supplied:
        if supplied != _GROUP: raise invalid('Recovery relocation requires all four path confirmations.')
        if params['target_path']!=params['confirmed_target'] or params['journal_path']!=params['confirmed_journal']:
            raise CoreError('CONFIRMATION_REQUIRED','Recovery paths need exact confirmations.','Confirm both supplied absolute paths.')
