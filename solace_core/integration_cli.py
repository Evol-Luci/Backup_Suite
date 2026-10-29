"""Manual review of immutable results and explicitly approved publication."""
import shlex
import json
from .errors import CoreError, invalid
from .integration_protocol import validate_integration
from .task_cli import unwrap


def add_parsers(commands) -> None:
    integration = commands.add_parser('integration', help='Review saved results, unpublished candidates and applied receipts',
        description='A result is an immutable saved checkpoint. A candidate is unpublished until explicitly approved and applied. A receipt records an applied result. Checks can be run on the exact candidate before approval.')
    actions = integration.add_subparsers(dest='action', required=True)
    for action in ('prepare','show','diff','resolve','export','approve','apply','receipt','abandon'):
        parser = actions.add_parser(action)
        parser.add_argument('project')
        parser.add_argument('task' if action=='prepare' else 'receipt' if action=='receipt' else 'plan')
        if action=='prepare':
            parser.add_argument('--result', required=True, help='Exact immutable saved result ID')
            parser.add_argument('--expected-revision', type=int, help='Pin the contributing task revision; required with --request-id')
            parser.add_argument('--expected-target-revision', type=int, help='Pin the parent task revision for a child result; required with --request-id')
        if action in ('resolve','export','approve','apply','abandon'):
            parser.add_argument('--revision', type=int, required=True, help='Exact reviewed plan revision from integration show')
        if action in ('diff','receipt'):
            parser.add_argument('--after', type=int)
            parser.add_argument('--limit', type=int, help='Metadata paths per page, 1–256 (default 128)')
        if action=='diff':
            parser.add_argument('--revision', type=int, help='Required for subsequent metadata pages and explicit bytes')
            parser.add_argument('--path', help='Exact engine-relative path for a version-byte page')
            parser.add_argument('--version', choices=('base','current','incoming','candidate'))
            parser.add_argument('--offset', type=int, help='Byte offset; JSON data is base64, up to 48 KiB per page')
        if action=='resolve':
            parser.add_argument('--path', required=True)
            choice = parser.add_mutually_exclusive_group(required=True)
            choice.add_argument('--take', choices=('current','incoming','delete'))
            choice.add_argument('--from-file', help='Absolute file path to import as the resolution')
        if action=='export': parser.add_argument('--to', dest='destination', required=True, help='Absolute new review-copy directory')
        if action=='approve':
            parser.add_argument('--token', required=True, help='Exact review token from integration show')
            choice = parser.add_mutually_exclusive_group(required=True)
            choice.add_argument('--accept-unchecked', action='store_true', default=None, help='Acknowledge Solace did not run checks')
            choice.add_argument('--check-run', help='Passing check run ID')
        if action=='apply':
            parser.add_argument('--approval', required=True, help='Exact approval token returned by integration approve')
            parser.add_argument('--writers-paused', action='store_true', required=True, help='Confirm external writers are paused')
        if action in ('prepare','export','apply'): parser.add_argument('--wait', action='store_true')
    from .policy_cli import add_queue_parser
    add_queue_parser(actions)
    from .check_cli import add_integration_parser
    add_integration_parser(actions)
    recovery = actions.add_parser('recovery', help='Inspect or explicitly recover an interrupted apply').add_subparsers(dest='recovery_action', required=True)
    for action in ('show','resume','restore-before'):
        parser = recovery.add_parser(action)
        parser.add_argument('project')
        parser.add_argument('operation', help='Original interrupted apply operation ID, not a recovery operation ID')
        if action=='show':
            parser.add_argument('--after', type=int)
            parser.add_argument('--limit', type=int)
        else:
            parser.add_argument('--writers-paused', action='store_true', required=True)
            parser.add_argument('--wait', action='store_true')
            parser.add_argument('--target-path', help='Recorded target relocation; provide all four path confirmations')
            parser.add_argument('--journal-path')
            parser.add_argument('--confirm-target', dest='confirmed_target')
            parser.add_argument('--confirm-journal', dest='confirmed_journal')


def prepare(client, args, project: str, request_id: str | None = None) -> tuple[str, dict]:
    """Build only pinned API fields; never refresh a review or approval token."""
    if 'integrations.v1' not in unwrap(client.request('core.status', {})).get('capabilities', []):
        raise CoreError('CORE_UPGRADE_REQUIRED', 'The connected core does not support result integration.',
                        'Install/activate a compatible core, then review an explicit project schema upgrade.')
    method = 'integration.'+args.action
    params = dict(project=project)
    if args.action=='prepare':
        revision = args.expected_revision
        target_revision = args.expected_target_revision
        if request_id:
            if revision is None:
                raise invalid('With --request-id, pin --expected-revision and, for a child result, --expected-target-revision so retries send identical arguments.')
        else:
            task = unwrap(client.request('task.show', dict(project=project, task=args.task)))
            if revision is None: revision = task['revision']
            if task['parent_id'] and target_revision is None:
                target_revision = unwrap(client.request('task.show', dict(project=project,task=task['parent_id'])))['revision']
        params.update(task=args.task,result=args.result,expected_revision=revision)
        if target_revision is not None: params['expected_target_revision']=target_revision
    else:
        if args.action=='recovery':
            method += '.'+args.recovery_action
            params['operation']=args.operation
        else:
            key = 'receipt' if args.action=='receipt' else 'plan'
            params[key]=getattr(args,key)
        # This explicit allowlist excludes argparse/UI controls from fingerprints.
        fields = {
            'show': (), 'diff': ('revision','after','limit','path','version','offset'),
            'resolve': ('revision','path','take','from_file'), 'export': ('revision','destination'),
            'approve': ('revision','token','accept_unchecked','check_run'), 'apply': ('revision','approval','writers_paused'),
            'receipt': ('after','limit'), 'abandon': ('revision',),
            'recovery': ('after','limit') if getattr(args,'recovery_action',None)=='show' else
                        ('writers_paused','target_path','journal_path','confirmed_target','confirmed_journal'),
        }
        for field in fields[args.action]:
            value=getattr(args,field,None)
            if value is not None: params[field]=value
    validate_integration(method,params)
    return method,params


def emit(value, method, params, *, socket=None) -> None:
    """Human guidance accompanies the exact bounded API projection."""
    from .cli import emit as emit_envelope
    if value['ok']:
        print(json.dumps(value['result'],indent=2,ensure_ascii=True,allow_nan=False),flush=True)
    else: emit_envelope(value, False)
    checks = value.get('result', {}).get('checks') if value.get('ok') else None
    if isinstance(checks, dict) and checks.get('status'):
        from .check_cli import status_message
        print(status_message(checks['status']), flush=True)
    elif checks == 'not_run_by_solace':
        print('Unchecked: Solace did not run checks for this result.', flush=True)
    elif value.get('ok') and params.get('accept_unchecked'):
        print('Unchecked approval: Solace did not run checks for this approval.', flush=True)
    elif value.get('ok') and params.get('check_run'):
        print('Checked approval uses the selected passing run.', flush=True)
    elif value.get('ok'):
        print('Check evidence is unavailable in this response.', flush=True)
    project=params['project']
    command=['solace','integration']
    if not value['ok']:
        # Keep API status and its next step visible; add a usable inspection command.
        if 'operation' in params: command += ['recovery','show',project,params['operation']]
        elif 'plan' in params: command += ['show',project,params['plan']]
        elif 'task' in params: command += ['prepare',project,params['task'],'--result',params['result']]
        else: command += ['receipt',project,params['receipt']]
    else:
        result=value['result']
        if result.get('state') in ('failed','interrupted'):
            error=result.get('error')
            if error: print(f"{error['code']}: {error['message']}\nNext step: {error['next_step']}",flush=True)
            if result['state']=='interrupted': command += ['recovery','show',project,result['id']]
            elif params.get('plan'): command += ['show',project,params['plan']]
            else: command=['solace','operation','show',project,result['id']]
        elif set(result)=={'operation_id'}:
            command=['solace','operation','show',project,result['operation_id']]
        else:
            summary=result.get('result',result)
            plan=summary.get('plan_id',params.get('plan'))
            if summary.get('receipt_id'):
                command += ['receipt',project,summary['receipt_id']]
                if method=='integration.receipt' and summary.get('next_after') is not None:
                    command += ['--after',str(summary['next_after']),'--limit',str(params.get('limit',128))]
            elif method=='integration.approve':
                command += ['apply',project,plan,'--revision',str(summary['revision']),'--approval',summary['approval'],'--writers-paused','--wait']
            elif method in ('integration.show','integration.resolve') and summary.get('revision'):
                command += ['diff',project,plan,'--revision',str(summary['revision']),'--limit','128']
            elif method=='integration.diff':
                if summary.get('next_offset') is not None:
                    command += ['diff',project,plan,'--revision',str(summary['revision']),'--path='+params['path'],'--version',params['version'],'--offset',str(summary['next_offset'])]
                elif summary.get('next_after') is not None:
                    command += ['diff',project,plan,'--revision',str(summary['revision']),'--after',str(summary['next_after']),'--limit',str(params.get('limit',128))]
                else: command += ['show',project,plan]
            elif method.startswith('integration.recovery.'):
                command += ['recovery','show',project,params['operation']]
                if summary.get('next_after') is not None: command += ['--after',str(summary['next_after']),'--limit',str(params.get('limit',128))]
            elif plan: command += ['show',project,plan]
            else:
                command += ['receipt',project,params['receipt']]
                if summary.get('next_after') is not None: command += ['--after',str(summary['next_after']),'--limit',str(params.get('limit',128))]
    if socket is not None: command += ['--socket',str(socket)]
    print('Next command: '+shlex.join(command+['--json']),flush=True)
