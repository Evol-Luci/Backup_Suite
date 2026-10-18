"""Bounded comparison projections and snapshot-pinned change pages."""
import hashlib
import json
from .errors import CoreError

FIELDS=('added','removed','changed','warnings')
SUMMARY_BYTES=48*1024
PAGE_BYTES=128*1024


def encoded_size(value):
    return len(json.dumps(value,ensure_ascii=True,separators=(',',':')).encode())


def observation_token(report):
    return hashlib.sha256(json.dumps(report,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def summary(report):
    if report is None:return None
    # Idempotent for already bounded durable operation results.
    if 'counts' in report and 'observation' in report:return report
    result={key:value for key,value in report.items() if key not in FIELDS}
    result.update(counts={key:len(report.get(key,[])) for key in FIELDS},observation=observation_token(report))
    remaining=SUMMARY_BYTES
    for key in FIELDS:
        selected=[]
        for value in report.get(key,[]):
            size=encoded_size(value)+1
            if size>remaining or sum(len(result.get(k,[])) for k in FIELDS)+len(selected)>=32:break
            selected.append(value);remaining-=size
        result[key]=selected
    result['truncated']=any(len(result[key])!=result['counts'][key] for key in FIELDS)
    return result


def change_page(report,after=0,limit=128,observation=None):
    if report is None:return None
    token=observation_token(report)
    if observation is not None and observation!=token:
        raise CoreError('OBSERVATION_CHANGED','The cached observation changed between pages.',
                        'Read status from cursor 0 and use its new observation token.')
    total=sum(len(report.get(key,[])) for key in FIELDS)
    if after>total:raise CoreError('CURSOR_INVALID','The change cursor is outside this observation.','Start at cursor 0.')
    entries=[];index=0;size=0
    for key in FIELDS:
        for value in report.get(key,[]):
            if index>=after:
                row=dict(kind=key[:-1] if key=='warnings' else key)
                if key=='changed':row.update(value)
                else:row['detail' if key=='warnings' else 'path']=value
                count=encoded_size(row)+1
                if len(entries)>=limit or size+count>PAGE_BYTES:
                    if not entries:raise CoreError('RESPONSE_TOO_LARGE','One change exceeds the page limit.','Inspect this workspace locally.')
                    return dict(observation=token,entries=entries,total=total,next_after=after+len(entries))
                entries.append(row);size+=count
            index+=1
    return dict(observation=token,entries=entries,total=total,next_after=None)


def operation_view(value):
    """Also bound results written by an earlier candidate before this fix."""
    result=value.get('result')
    if isinstance(result,dict):
        if 'added' in result: value['result']=summary(result)
        elif isinstance(result.get('comparison'),dict):value['result']=dict(result,comparison=summary(result['comparison']))
    params=value.get('params')
    if isinstance(params,dict):
        for key in ('workspace','parent_workspace'):
            if isinstance(params.get(key),dict):
                params[key]={k:v for k,v in params[key].items() if k!='observed_json'}
    return value


def decision_page(db,plan_id,revision,*,after=0,limit=128,summary_fields=None):
    """Page immutable decisions by offset, bounding the complete encoded object."""
    from .errors import invalid
    if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=256:
        raise invalid('Decision cursors must be nonnegative integers; limits must be 1–256.')
    total=db.execute('SELECT COUNT(*) FROM integration_decisions WHERE plan_id=? AND revision=?',(plan_id,revision)).fetchone()[0]
    if after>total:
        raise CoreError('CURSOR_INVALID','The decision cursor exceeds retained history.','Start at cursor zero.')
    result=dict(summary_fields or {},revision=revision,entries=[],total=total,next_after=None)
    rows=db.execute('SELECT path,kind,reason,decision_json FROM integration_decisions WHERE plan_id=? AND revision=? '
                    'ORDER BY ordinal LIMIT ? OFFSET ?',(plan_id,revision,limit,after))
    for row in rows:
        entry=dict(path=row['path'],kind=row['kind'],reason=row['reason'],**json.loads(row['decision_json']))
        candidate=dict(result,entries=result['entries']+[entry],next_after=after+len(result['entries'])+1)
        if encoded_size(candidate)>PAGE_BYTES:
            if not result['entries']:
                raise CoreError('RESPONSE_TOO_LARGE','One decision exceeds the page byte limit.','Export the retained plan to inspect complete versions.')
            break
        result=candidate
    consumed=after+len(result['entries'])
    result['next_after']=consumed if consumed<total and result['entries'] else None
    return result
