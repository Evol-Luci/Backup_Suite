"""Canonical human/agent command surface for the local core."""
import argparse
import json
import os
from pathlib import Path
import sys
from .client import CoreClient
from .errors import CoreError, classify, invalid
from .protocol import response


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise invalid(message)


def exit_code(code):
    if code in ('INVALID_REQUEST','CONFIRMATION_REQUIRED','CURSOR_INVALID','PROMPT_INVALID','TASK_REVISION_CONFLICT'): return 2
    if code in ('CORE_UNAVAILABLE','PROJECT_UNAVAILABLE','STORE_UNAVAILABLE','PROJECT_NOT_FOUND'): return 3
    if code in ('CORRUPT_OBJECT','DATABASE_CORRUPT','SCHEMA_VERSION_UNSUPPORTED','STORE_IDENTITY_MISMATCH'): return 4
    return 1


def emit(value, as_json):
    if as_json:
        print(json.dumps(value,ensure_ascii=True,allow_nan=False),flush=True)
    elif value.get('ok') is False:
        error=value['error']
        print(f"{error['code']}: {error['message']}\nNext step: {error['next_step']}",file=sys.stderr)
    else:
        result=value.get('result',value)
        if isinstance(result,dict) and 'operation_id' in result:
            print(f"Accepted operation {result['operation_id']}. Inspect it with solace operation show.",flush=True)
        else:
            print(json.dumps(result,indent=2,ensure_ascii=True),flush=True)


def _parser():
    parser=Parser(prog='solace',description='Local project registration and recoverable checkpoints')
    parser.add_argument('--json',action='store_true',help='Structured JSON output')
    parser.add_argument('--socket',help='Explicit development socket')
    parser.add_argument('--request-id',help='Reuse this UUID when retrying a mutation')
    commands=parser.add_subparsers(dest='command',required=True,parser_class=Parser)
    core=commands.add_parser('core'); core.add_subparsers(dest='action',required=True).add_parser('status')
    project=commands.add_parser('project'); actions=project.add_subparsers(dest='action',required=True)
    add=actions.add_parser('add');add.add_argument('root');add.add_argument('--store')
    actions.add_parser('list')
    show=actions.add_parser('show');show.add_argument('project');show.add_argument('--watch',action='store_true');show.add_argument('--after',type=int,default=0)
    relink=actions.add_parser('relink');relink.add_argument('project');relink.add_argument('--root',required=True);relink.add_argument('--store',required=True);relink.add_argument('--confirm-root')
    unregister=actions.add_parser('unregister');unregister.add_argument('project')
    from .task_cli import add_parsers
    add_parsers(commands,actions)
    from .integration_cli import add_parsers as add_integration_parsers
    add_integration_parsers(commands)
    checkpoints=commands.add_parser('checkpoint'); actions=checkpoints.add_subparsers(dest='action',required=True)
    for action in ('create','list','verify','restore','usage'):
        p=actions.add_parser(action);p.add_argument('project')
        if action=='list': p.add_argument('--all-workspaces',action='store_true')
        if action=='create': p.add_argument('message');p.add_argument('--exclude',action='append',default=[])
        if action in ('verify','restore'): p.add_argument('checkpoint')
        if action=='restore':p.add_argument('--to',dest='destination',required=True)
    operation=commands.add_parser('operation');p=operation.add_subparsers(dest='action',required=True).add_parser('show')
    p.add_argument('project');p.add_argument('operation');p.add_argument('--watch',action='store_true')
    doctor=commands.add_parser('doctor');doctor.add_argument('project',nargs='?')
    return parser


def _resolve(client, project):
    listing=client.request('project.list',{})
    if not listing['ok']: raise CoreError(**listing['error'])
    for record in listing['result']['projects']:
        if project == record['id'] or str(Path(project).resolve()) == record['root']:
            return record['id']
    # Exact stable IDs can name an unregistered project for idempotent receipt replay.
    if len(project) == 32 and all(c in '0123456789abcdef' for c in project):
        return project
    raise CoreError('PROJECT_NOT_FOUND','The project is not registered.','Use its exact ID or canonical source root.')


def _doctor(client, project, connectivity_error=None):
    result=response(None, error=connectivity_error) if client is None else client.request('doctor',{'project':project} if project else {})
    if result['ok']:
        result['result']['core']={'ok':True}
        return result
    from .registry import Registry
    from .diagnostics import inspect_registry
    data=Path(os.environ.get('XDG_DATA_HOME') or str(Path.home()/'.local/share'))
    try:
        registry=Registry(data/'solace/registry.sqlite3',read_only=True)
        report=inspect_registry(registry,project)
    except Exception as error:
        report=dict(ok=False,registry={'ok':False,'error':classify(error).as_dict()},projects=[])
    report.update(ok=False,core={'ok':False,'error':result['error']})
    return response(result['id'],result=report)


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    common=Parser(add_help=False)
    common.add_argument('--json',action='store_true');common.add_argument('--socket');common.add_argument('--request-id')
    as_json='--json' in argv
    try:
        globals_,remaining=common.parse_known_args(argv)
        as_json=globals_.json
        args=_parser().parse_args(remaining)
        if args.command=='doctor':
            try:
                client=CoreClient(globals_.socket)
                result=_doctor(client,args.project)
            except Exception as error:
                result=_doctor(None,args.project,classify(error))
            emit(result,as_json)
            return 0 if result['result']['ok'] else 3
        client=CoreClient(globals_.socket)
        params={}
        if hasattr(args,'project'):params['project']=_resolve(client,args.project)
        method=args.command+'.'+args.action
        if method=='project.add':
            params={'root':str(Path(args.root).resolve())}
            if args.store:params['store']=str(Path(args.store).resolve())
        if method=='project.relink':
            root=str(Path(args.root).resolve())
            confirmed=args.confirm_root
            if confirmed is None and sys.stdin.isatty():
                print(f'Relink source root to {root}? [y/N] ',end='',file=sys.stderr,flush=True)
                if input().strip().lower()=='y':confirmed=root
            params.update(root=root,store=str(Path(args.store).resolve()),confirmed_root=confirmed or '')
        if args.command in ('task','workspace') or (args.command=='project' and args.action in ('upgrade','prompt-policy')):
            from .task_cli import prepare
            method,params=prepare(client,args,params['project'],globals_.request_id)
        if args.command=='integration':
            from .integration_cli import prepare
            method,params=prepare(client,args,params['project'],globals_.request_id)
        if args.command=='checkpoint':
            if args.action=='list' and args.all_workspaces:
                from .task_cli import unwrap
                if 'tasks.v1' not in unwrap(client.request('core.status',{})).get('capabilities',[]):
                    raise CoreError('CORE_UPGRADE_REQUIRED','The core cannot list task history.','Activate a compatible core.')
                params['all_workspaces']=True
            if args.action=='create':params.update(message=args.message,exclusions=args.exclude)
            if args.action in ('verify','restore'):params['checkpoint']=args.checkpoint
            if args.action=='restore':params['destination']=str(Path(args.destination).absolute())
        if args.command=='operation':params['operation']=args.operation
        result=client.request(method,params,request_id=globals_.request_id)
        if result['ok'] and getattr(args,'wait',False) and 'operation_id' in result['result']:
            from .task_cli import wait_for_operation
            result=wait_for_operation(client,params['project'],result)
        if result['ok'] and method=='task.prompt.show' and not as_json:
            text=result['result'].get('body')
            if text is None:
                raise CoreError('PROMPT_UNAVAILABLE','Prompt text is '+result['result']['status']+'.','Inspect prompt metadata with --json.')
            sys.stdout.buffer.write(text.encode('utf-8'));sys.stdout.buffer.flush()
        elif args.command=='integration' and not as_json:
            from .integration_cli import emit as emit_integration
            emit_integration(result,method,params)
        else: emit(result,as_json)
        if not result['ok']:return exit_code(result['error']['code'])
        if getattr(args,'wait',False) and result['result'].get('state') in ('failed','interrupted'):return 1
        if getattr(args,'watch',False):
            if args.command=='operation' and result['result']['state'] not in ('queued','running'):
                return 0 if result['result']['state']=='succeeded' else 1
            for event in client.subscribe(params['project'],getattr(args,'after',0)):
                if args.command=='task':
                    emit(client.request('task.show',dict(project=params['project'],task=params['task'])),as_json)
                else: emit(response(None,result=event),as_json)
                if args.command=='operation' and event['operation_id']==args.operation and event['state'] in ('succeeded','failed','interrupted'):
                    return 0 if event['state']=='succeeded' else 1
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        structured=classify(error);emit(response(None,error=structured),as_json)
        return exit_code(structured.code)


if __name__=='__main__':
    sys.exit(main())
