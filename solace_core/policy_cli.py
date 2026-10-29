"""Explicit policy previews, confirmations and queue inspection."""
from .errors import CoreError
from .policy_protocol import validate_policy
from .task_cli import unwrap


def add_policy_parser(actions, scope):
    policy=actions.add_parser('policy').add_subparsers(dest='policy_action',required=True)
    for action in ('show','preview','apply'):
        parser=policy.add_parser(action)
        parser.add_argument('project')
        if scope=='task': parser.add_argument('task')
        if action!='show':
            parser.add_argument('--mode',required=True,choices=('inherit','manual','checked_queue') if scope=='task' else ('manual','checked_queue'))
            if scope=='task': parser.add_argument('--revision',type=int,required=True)
            else:
                parser.add_argument('--required-profile')
                choice=parser.add_mutually_exclusive_group(required=True)
                choice.add_argument('--allow-task-opt-in',dest='allow_task_opt_in',action='store_true')
                choice.add_argument('--no-allow-task-opt-in',dest='allow_task_opt_in',action='store_false')
        if action=='apply':
            parser.add_argument('--plan-token',required=True)
            parser.add_argument('--confirmed',action='store_true',required=True)


def add_queue_parser(actions):
    queue=actions.add_parser('queue').add_subparsers(dest='queue_action',required=True)
    for action in ('list','show'):
        parser=queue.add_parser(action);parser.add_argument('project')
        if action=='show':parser.add_argument('intent')
        else:
            parser.add_argument('--after',type=int);parser.add_argument('--limit',type=int)


def prepare(client,args,project,request_id=None):
    if 'policy.v1' not in unwrap(client.request('core.status',{})).get('capabilities',[]):
        raise CoreError('CORE_UPGRADE_REQUIRED','The connected core does not support integration policy.','Activate a compatible core and review an explicit project upgrade.')
    params=dict(project=project)
    if args.command=='integration':
        method='integration.queue.'+args.queue_action
        fields=('intent','after','limit')
    else:
        method=args.command+'.policy.'+args.policy_action
        fields=('task','mode','required_profile','allow_task_opt_in','plan_token','confirmed')
        if getattr(args,'revision',None) is not None:params['expected_revision']=args.revision
    for field in fields:
        value=getattr(args,field,None)
        if value is not None:params[field]=value
    validate_policy(method,params)
    return method,params


def emit(value,method,params):
    from .cli import emit as emit_envelope
    emit_envelope(value,False)
    if value.get('ok'):
        if method.endswith('.preview'):
            print('Review the binding and OS-authority warning. Apply the identical fields with --plan-token and --confirmed.')
        elif method.startswith('integration.queue.'):
            print('Queued checks never publish files. Inspect the plan and check run; explicitly approve, pause writers, then apply.')
        else:
            print('Manual publication still requires explicit review, approval and writers-paused acknowledgement.')
