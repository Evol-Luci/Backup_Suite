"""Configured check commands and strict API request construction."""
import json
from pathlib import Path
from .errors import CoreError, invalid
from .task_cli import unwrap, revision
from .check_protocol import validate_check

def add_parsers(commands, project_actions):
    actions=project_actions.add_parser('check-profile').add_subparsers(dest='check_action',required=True)
    for name in ('list','show','preview','apply'):
        p=actions.add_parser(name);p.add_argument('project')
        if name=='list': p.add_argument('--after',type=int);p.add_argument('--limit',type=int)
        if name=='show': p.add_argument('profile_id')
        if name in ('preview','apply'):
            p.add_argument('profile_action',choices=('create','update','retire'));p.add_argument('--profile-id');p.add_argument('--definition',help='JSON profile file')
        if name=='apply': p.add_argument('--plan-token',required=True);p.add_argument('--confirmed',action='store_true',required=True)

def add_task_parser(actions):
    p=actions.add_parser('check-profile').add_subparsers(dest='check_action',required=True).add_parser('select')
    p.add_argument('project');p.add_argument('task');p.add_argument('profile');p.add_argument('--revision',type=int)

def add_integration_parser(actions):
    checks=actions.add_parser('check').add_subparsers(dest='check_action',required=True)
    for name in ('run','show','list','cancel'):
        p=checks.add_parser(name);p.add_argument('project');p.add_argument('plan' if name in ('run','list') else 'run')
        if name=='run': p.add_argument('--revision',type=int,required=True);p.add_argument('--profile',required=True);p.add_argument('--wait',action='store_true')
        if name=='list': p.add_argument('--after',type=int);p.add_argument('--limit',type=int)

def prepare(client,args,project,request_id=None):
    if 'checks.v1' not in unwrap(client.request('core.status',{})).get('capabilities',[]):
        raise CoreError('CORE_UPGRADE_REQUIRED','The connected core does not support configured checks.','Activate a compatible core.')
    params={'project':project}
    if args.command=='project':
        method='project.check-profile.'+args.check_action
        for field in ('after','limit','profile_id','plan_token','confirmed'):
            value=getattr(args,field,None)
            if value is not None: params[field]=value
        if args.check_action in ('preview','apply'):
            params['action']=args.profile_action
            if args.definition:
                try: params['definition']=json.loads(Path(args.definition).read_text())
                except (OSError,ValueError) as error: raise invalid('Profile definition must be a readable JSON file.') from error
    elif args.command=='task':
        method='task.check-profile.select'
        params.update(task=args.task,profile=args.profile,expected_revision=revision(client,project,args.task,args.revision,request_id))
    else:
        method='integration.check.'+args.check_action
        for field in ('plan','run','revision','profile','after','limit'):
            value=getattr(args,field,None)
            if value is not None: params[field]=value
    validate_check(method,params)
    return method,params

def status_message(status):
    return {'passed':'Checks passed for this candidate.', 'failed':'Checks failed; inspect the run.', 'stale':'Check evidence is stale; run checks again.', 'not_run_by_solace':'Unchecked: Solace did not run checks.'}.get(status,'Check status: '+str(status))


def emit(value, method, params):
    from .cli import emit as emit_envelope
    emit_envelope(value, False)
    if not value.get('ok'): return
    result=value['result']
    state=result.get('result', {}).get('state', result.get('state'))
    if state in ('passed','failed','stale','not_run_by_solace'):
        print(status_message(state),flush=True)
    for run in result.get('runs',[]):
        if run.get('state') in ('passed','failed','stale'):
            print(f"{run['id']}: {status_message(run['state'])}",flush=True)
