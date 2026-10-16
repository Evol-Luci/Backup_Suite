"""Task/workspace argument grammar, pinned mutations and bounded operation waits."""
from pathlib import Path
import time
from .errors import CoreError,invalid
from .prompts import validate_prompt


def add_parsers(commands,project_actions):
    upgrade=project_actions.add_parser('upgrade');upgrade.add_argument('project')
    choice=upgrade.add_mutually_exclusive_group(required=True)
    choice.add_argument('--preview',action='store_true');choice.add_argument('--apply',action='store_true')
    upgrade.add_argument('--plan-token')
    policy=project_actions.add_parser('prompt-policy').add_subparsers(dest='policy_action',required=True)
    for action in ('show','set'):
        p=policy.add_parser(action);p.add_argument('project')
        if action=='set':
            p.add_argument('retention_policy',choices=('days','retain','none'));p.add_argument('--days',type=int);p.add_argument('--revision',type=int)
    task=commands.add_parser('task').add_subparsers(dest='action',required=True)
    for action in ('create','list','show','start','block','resume','ready','cancel'):
        p=task.add_parser(action);p.add_argument('project')
        if action=='create':
            p.add_argument('title');p.add_argument('--parent');p.add_argument('--parent-revision',type=int)
            p.add_argument('--prompt-file');p.add_argument('--prompt-retention');p.add_argument('--exclude',action='append')
        elif action=='list': p.add_argument('--parent')
        else: p.add_argument('task')
        if action in ('start','block','resume','ready','cancel'): p.add_argument('--revision',type=int)
        if action=='block': p.add_argument('--reason',required=True)
        if action=='start': p.add_argument('--from',dest='from_checkpoint');p.add_argument('--parent-revision',type=int)
        if action in ('start','ready'): p.add_argument('--wait',action='store_true')
        if action=='show': p.add_argument('--watch',action='store_true');p.add_argument('--after',type=int,default=0)
    prompt=task.add_parser('prompt').add_subparsers(dest='prompt_action',required=True)
    for action in ('show','delete'):
        p=prompt.add_parser(action);p.add_argument('project');p.add_argument('task')
        if action=='delete': p.add_argument('--revision',type=int)
    workspace=commands.add_parser('workspace').add_subparsers(dest='action',required=True)
    for action in ('status','checkpoint','relink'):
        p=workspace.add_parser(action);p.add_argument('project');p.add_argument('task');p.add_argument('--wait',action='store_true')
        if action=='status':
            p.add_argument('--refresh',action='store_true');p.add_argument('--after',type=int)
            p.add_argument('--limit',type=int);p.add_argument('--observation')
        else: p.add_argument('--revision',type=int)
        if action=='checkpoint':p.add_argument('message')
        if action=='relink':
            p.add_argument('--path',required=True);p.add_argument('--confirm-path',required=True);p.add_argument('--checkpoint',required=True);p.add_argument('--writers-paused',action='store_true',required=True)
    recovery=workspace.add_parser('recovery').add_subparsers(dest='recovery_action',required=True)
    for action in ('show','resume','abandon'):
        p=recovery.add_parser(action);p.add_argument('project');p.add_argument('operation')
        if action!='show':p.add_argument('--task',required=True);p.add_argument('--revision',type=int)
        if action=='resume':p.add_argument('--wait',action='store_true')
    cleanup=workspace.add_parser('staging').add_subparsers(dest='staging_action',required=True).add_parser('cleanup')
    cleanup.add_argument('project');cleanup.add_argument('operation');choice=cleanup.add_mutually_exclusive_group(required=True)
    choice.add_argument('--preview',action='store_true');choice.add_argument('--apply',action='store_true');cleanup.add_argument('--plan-token')


def unwrap(value):
    if not value['ok']: raise CoreError(**value['error'])
    return value['result']


def revision(client,project,task,pinned,request_id):
    if pinned is None and request_id:
        raise invalid('With --request-id, pin --revision (and --parent-revision for child actions) so retries send identical arguments.')
    return pinned if pinned is not None else unwrap(client.request('task.show',dict(project=project,task=task)))['revision']


def prepare(client,args,project,request_id=None):
    method=args.command+'.'+args.action;params=dict(project=project)
    capability='workspaces.v1' if args.command=='workspace' else 'tasks.v1'
    if args.command=='project':capability='project-upgrade.v1' if args.action=='upgrade' else 'prompt-retention.v1'
    capabilities=unwrap(client.request('core.status',{})).get('capabilities',[])
    if capability not in capabilities:
        raise CoreError('CORE_UPGRADE_REQUIRED','The connected core does not support this command.','Install/activate a compatible core, then review any project schema upgrade.')
    if method=='project.upgrade':
        params['preview']=args.preview
        if args.plan_token is not None: params['plan_token']=args.plan_token
    elif method=='project.prompt-policy':
        method+='.'+args.policy_action
        if args.policy_action=='set':
            if args.revision is None and request_id: raise invalid('Pin --revision when using --request-id for a policy change.')
            params.update(policy=args.retention_policy,expected_revision=args.revision if args.revision is not None else unwrap(client.request('project.prompt-policy.show',params))['revision'])
            if args.days is not None:params['days']=args.days
    elif method=='task.create':
        params['title']=args.title
        if args.parent:
            params.update(parent=args.parent,expected_parent_revision=revision(client,project,args.parent,args.parent_revision,request_id))
        elif args.parent_revision is not None: raise invalid('A root task has no parent revision.')
        if args.exclude is not None: params['exclusions']=args.exclude
        if args.prompt_file:
            try:
                with Path(args.prompt_file).open('rb') as source: data=source.read(131073)
                text=data.decode('utf-8','strict');validate_prompt(text)
            except UnicodeError:
                raise CoreError('PROMPT_INVALID','The prompt file is not valid UTF-8 text.','Use a UTF-8 file without binary content.')
            params['prompt']=text
        if args.prompt_retention:
            policy=args.prompt_retention
            if policy in ('retain','none'):params['prompt_policy']=policy
            else:
                try: days=int(policy)
                except ValueError: raise invalid('Prompt retention must be retain, none or a positive day count.')
                if days<1: raise invalid('Prompt retention days must be positive.')
                params.update(prompt_policy='days',prompt_days=days)
    elif method=='task.list':
        if args.parent:params['parent']=args.parent
    elif method=='workspace.staging':
        method='workspace.staging.cleanup';params.update(operation=args.operation,preview=args.preview)
        if args.plan_token is not None:params['plan_token']=args.plan_token
    elif method=='workspace.recovery':
        method+='.'+args.recovery_action;params['operation']=args.operation
        if args.recovery_action!='show':
            params.update(task=args.task,expected_revision=revision(client,project,args.task,args.revision,request_id))
    else:
        params['task']=args.task
        if method=='task.prompt':method+='.'+args.prompt_action
        needs_revision=(method in ('task.start','task.block','task.resume','task.ready','task.cancel','task.prompt.delete','workspace.checkpoint','workspace.relink'))
        if needs_revision:params['expected_revision']=revision(client,project,args.task,args.revision,request_id)
        if method=='task.start':
            task=unwrap(client.request('task.show',dict(project=project,task=args.task)))
            if task['parent_id']:
                params['expected_parent_revision']=revision(client,project,task['parent_id'],args.parent_revision,request_id)
            elif args.parent_revision is not None: raise invalid('A root task has no parent revision.')
            if args.from_checkpoint:params['from_checkpoint']=args.from_checkpoint
        if method=='task.block':params['reason']=args.reason
        if method=='workspace.status':
            params['refresh']=args.refresh
            for key in ('after','limit','observation'):
                if getattr(args,key) is not None:params[key]=getattr(args,key)
        if method=='workspace.checkpoint':params['message']=args.message
        if method=='workspace.relink':params.update(path=str(Path(args.path).absolute()),confirmed_path=args.confirm_path,checkpoint=args.checkpoint,writers_paused=args.writers_paused)
    return method,params


def wait_for_operation(client,project,accepted,*,timeout=60.0):
    operation=accepted['result']['operation_id'];deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=client.request('operation.show',dict(project=project,operation=operation))
        if not result['ok'] or result['result']['state'] not in ('queued','running'):return result
        time.sleep(min(0.1,max(0,deadline-time.monotonic())))
    return dict(accepted,wait_timed_out=True)
