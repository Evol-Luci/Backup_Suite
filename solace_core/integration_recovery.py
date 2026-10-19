"""Explicit recovery of retained publication evidence; restart is metadata only."""
from dataclasses import asdict
import json
import hashlib
import copy
import os
from pathlib import Path
import stat
import uuid

from .errors import CoreError, classify
from .integration_publish import IntegrationPublisher, _identity, _match, _stat, _refuse
from .integration_snapshots import canonical, entry_from_json
from .integration_types import Identity, Journal
from .observations import encoded_size
from .protocol import fingerprint
from .schema import require_integrations
from .workspace_paths import directory_identity
from .workspace_trees import _rename_noreplace

_TERMINAL = ('committed', 'restored', 'abandoned')
_GROUP = ('target_path', 'journal_path', 'confirmed_target', 'confirmed_journal')


class IntegrationRecovery:
    def __init__(self, service, catalog):
        self.service, self.catalog = service, catalog
        self.publisher = IntegrationPublisher(service, catalog)
        self.claims = self.publisher.claims

    @staticmethod
    def classify_interrupted(db) -> list[dict]:
        """No path probes or file writes: unavailable storage remains deferred."""
        rows = [dict(r) for r in db.execute("SELECT operation_id,plan_id,phase FROM integration_journals WHERE phase NOT IN ('committed','restored','abandoned')")]
        for row in rows:
            db.execute("UPDATE integration_plans SET state='recovery_required' WHERE id=?", (row['plan_id'],))
            if row['phase'] != 'reserved':
                db.execute("UPDATE integration_journals SET phase='recovery_required' WHERE operation_id=?", (row['operation_id'],))
        return rows

    def _journal(self, original):
        row = self.publisher._row(original)
        if row['phase'] in _TERMINAL: _refuse('This publication is already terminal; inspect its retained outcome.')
        if row['identity_json'] is None: _refuse('The journal has no recorded physical identity; preserve unidentified entries.')
        return Journal(original, row['plan_id'], row['revision'], Path(row['journal_path']),
                       Identity(**json.loads(row['identity_json'])), Identity(**json.loads(row['target_identity_json'])))

    def show(self, operation_id: str, *, after: int = 0, limit: int = 128) -> dict:
        require_integrations(self.catalog)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 256:
            raise CoreError('INVALID_REQUEST', 'Invalid recovery page.', 'Use a nonnegative cursor and limit 1–256.')
        row = self.publisher._row(operation_id)
        actions = self.publisher._actions(operation_id)
        if after > len(actions): raise CoreError('INVALID_REQUEST', 'Recovery cursor is out of range.', 'Start at cursor zero.')
        available = all(Path(p).is_dir() for p in (row['journal_path'], json.loads(row['target_identity_json'])['path']))
        result = dict(operation_id=operation_id, plan_id=row['plan_id'], revision=row['revision'],
                      state=row['phase'] if row['phase'] in _TERMINAL else 'recovery_required',
                      validation='required' if available else 'deferred', total=len(actions), entries=[], next_after=None)
        for action in actions[after:after+limit]:
            entry = {k:action[k] for k in ('ordinal','path','kind','phase')}
            proposal = dict(result, entries=result['entries']+[entry], next_after=after+len(result['entries'])+1)
            if encoded_size(proposal) > 128*1024:
                if not result['entries']: raise CoreError('RESPONSE_TOO_LARGE', 'Recovery action exceeds the page limit.', 'Inspect the retained journal locally.')
                break
            result['entries'].append(entry)
        end = after+len(result['entries'])
        if end < len(actions): result['next_after'] = end
        return result

    def accept(self, request: dict) -> dict:
        existing = self.catalog.find_operation(request)
        if existing is not None: return existing
        require_integrations(self.catalog)
        p = request['params']; mode = request['method'].removeprefix('integration.recovery.')
        if mode not in ('resume','restore-before') or p.get('writers_paused') is not True:
            raise CoreError('CONFIRMATION_REQUIRED', 'Recovery requires paused external writers.', 'Pause writers and acknowledge writers_paused.')
        supplied = [name in p for name in _GROUP]
        if any(supplied) and not all(supplied):
            raise CoreError('INVALID_REQUEST', 'Relocation requires all four path confirmations.', 'Supply target_path, journal_path and their exact confirmations together.')
        if all(supplied):
            for key in ('target','journal'):
                value = p[key+'_path']
                if not isinstance(value,str) or not Path(value).is_absolute() or '..' in Path(value).parts or '\0' in value or p['confirmed_'+key] != value:
                    raise CoreError('CONFIRMATION_REQUIRED', 'Relocation paths require exact absolute confirmation.', 'Confirm both supplied absolute paths.')
        with self.service.registry.lock, self.service.coordinator(self.catalog.registration['id']).lock:
            self.service.registry.get(self.catalog.registration['id'])
            with self.catalog.connect(write=True) as db:
                existing = self.claims.tasks.lookup_request(request, db)
                if existing is not None: return existing
                original = db.execute('SELECT state FROM operations WHERE id=?',(p['operation'],)).fetchone()
                journal = db.execute('SELECT phase FROM integration_journals WHERE operation_id=?',(p['operation'],)).fetchone()
                if not original or original[0] != 'interrupted' or not journal or journal[0] in _TERMINAL:
                    _refuse('Recovery requires an interrupted, unresolved publication.')
                if db.execute("SELECT 1 FROM integration_recoveries r JOIN operations o ON o.id=r.operation_id WHERE r.original_operation_id=? AND o.state IN ('queued','running')",(p['operation'],)).fetchone():
                    _refuse('Another recovery request already owns this publication.')
                prior = db.execute('SELECT location_json,mode FROM integration_recoveries WHERE original_operation_id=? ORDER BY rowid DESC LIMIT 1',(p['operation'],)).fetchone()
                previous = json.loads(prior[0]) if prior else {}
                if previous.get('restoration') and mode != 'restore-before': _refuse('Restoration has started; explicitly finish restoration before preparing a fresh plan.')
                operation = uuid.uuid4().hex; now = self.catalog._now()
                location = dict(format=1, phase='accepted', mapping=previous.get('mapping'), restoration=previous.get('restoration'),
                                requested={k:p[k] for k in _GROUP if k in p} or (previous.get('requested',{}) if previous.get('mapping') else {}))
                db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (operation,request['id'],fingerprint(request['method'],p,request['actor']),request['method'],request['actor'],'queued',now,now,None,None,canonical(p)))
                db.execute('INSERT INTO integration_recoveries VALUES (?,?,?,?,?)',(operation,p['operation'],mode,None,canonical(location)))
                self.catalog._event(db,dict(id=operation,actor=request['actor']),'operation.queued','queued',{})
        self.catalog.notify()
        return dict(operation_id=operation)

    def _load(self, operation):
        with self.catalog.connect() as db:
            row = db.execute('SELECT * FROM integration_recoveries WHERE operation_id=?',(operation,)).fetchone()
        if row is None: _refuse('Recovery has no durable request link.')
        return dict(row), json.loads(row['location_json'])

    def _save(self, operation, location):
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE integration_recoveries SET location_json=? WHERE operation_id=?',(canonical(location),operation))

    @staticmethod
    def _read_optional(fd, name):
        try: descriptor = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError: return None
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 16*1024*1024: _refuse('Invalid recovery marker.')
            chunks=[];remaining=before.st_size
            while remaining:
                chunk=os.read(descriptor,min(remaining,65536))
                if not chunk: _refuse('Recovery marker was truncated.')
                chunks.append(chunk);remaining-=len(chunk)
            after=os.fstat(descriptor)
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns): _refuse('Recovery marker changed during validation.')
            value=json.loads(b''.join(chunks))
            if not _match(_stat(fd,name),_identity(before)): _refuse('Recovery marker changed while reading.')
            return value
        finally: os.close(descriptor)

    @staticmethod
    def _record(action):
        return dict(ordinal=action['ordinal'],path=action['path'],kind=action['kind'],
                    old=json.loads(action['old_json']),new=json.loads(action['new_json']),slot=action['slot'])

    def _physical(self, held, journal, space, name):
        if space not in ('target','old','new','retained') or not name or name.startswith('/') or any(p in ('','.','..') for p in name.split('/')) or '\0' in name:
            _refuse('Invalid recorded recovery endpoint.')
        if space!='target' and '/' in name: _refuse('A private slot must be a single name.')
        root = Path(journal.target.path) if space == 'target' else journal.path if space == 'retained' else journal.path/space
        path = root/name
        try: fd = held.open(path.parent)
        except (FileNotFoundError,NotADirectoryError): return None, path.name, None
        return fd,path.name,_stat(fd,path.name)

    def _state(self, intent, actions):
        state = {('target',p):dict(identity=i,entry=next(e for e in intent['current_entries'] if e['path']==p)) for p,i in intent['current_identities'].items()}
        state.update({('target',p):dict(identity=i,entry=None) for p,i in intent['directories'].items() if p != '.'})
        for action in actions:
            for endpoint in (json.loads(action['old_json']),json.loads(action['new_json'])):
                state.setdefault((endpoint['space'],endpoint['name']),None)
            if action['kind']=='publish_new':
                endpoint=json.loads(action['old_json']);state[(endpoint['space'],endpoint['name'])]=endpoint
        for action in actions:
            if action['phase'] != 'done': continue
            before,after=json.loads(action['old_json']),json.loads(action['new_json'])
            state[(before['space'],before['name'])]=None
            state[(after['space'],after['name'])]=None if action['kind']=='rmdir' else dict(identity=json.loads(action['observed_json'])['destination'],entry=after['entry'])
        return state

    def _verify_state(self, held, journal, intent, actions):
        state=self._state(intent,actions)
        for (space,name), expected in state.items():
            fd,leaf,meta=self._physical(held,journal,space,name)
            if not _match(meta, expected['identity'] if expected else None): _refuse('Action evidence has a missing, replaced or ambiguous entry.')
            if expected and expected['entry']:
                self.publisher._content(fd,leaf,entry_from_json(expected['entry']),expected['identity'])
        # Check the entire capture scope, including unchanged paths and unexpected leaves.
        target_entries=tuple(entry_from_json(value['entry']) for (space,_),value in state.items() if space=='target' and value and value['entry'])
        self.publisher._verify_scope(journal,intent,target_entries)
        # Replacement inventories also preserve excluded, special and empty entries.
        for name in intent['expected']['directory_inventories']:
            expected=state.get(('target',name))
            if expected and expected['entry'] is None:
                fd=held.open(Path(journal.target.path)/name)
                wanted={Path(path).relative_to(name).parts[0] for (space,path),value in state.items()
                        if space=='target' and value and path!=name and Path(path).is_relative_to(name)}
                if set(os.listdir(fd)) != wanted: _refuse('A replacement directory contains an unrecognized entry.')
        held.check()

    def _reconcile(self, journal, *, persist):
        with self.publisher._session(journal) as (held,intent,row):
            actions=self.publisher._actions(journal.operation_id)
            self.publisher._validate_actions(intent,actions)
            fd=held.open(journal.path); incomplete=False; lag=[]
            for action in actions:
                record=self._record(action)
                marker=self._read_optional(fd,f"{action['ordinal']}.intent.json")
                done=self._read_optional(fd,f"{action['ordinal']}.done.json")
                if marker is not None:
                    for key in ('old','new'):
                        base=dict(marker[key]);base.pop('parent_identity',None)
                        expected=dict(record[key]);expected.pop('parent_identity',None)
                        if base != expected: _refuse('Action marker differs from retained intent.')
                    if any(marker[k]!=record[k] for k in ('ordinal','path','kind','slot')): _refuse('Action marker identity changed.')
                    if action['phase']!='planned' and marker!=record: _refuse('Recorded action parents changed.')
                    record=marker
                    action.update(old_json=canonical(marker['old']),new_json=canonical(marker['new']))
                elif action['phase']!='planned' or done is not None: _refuse('A progressed action lacks its durable intent.')
                if done is not None:
                    if done.get('record')!=record or not isinstance(done.get('observed'),dict): _refuse('Invalid completed-action marker.')
                    observed=done['observed']; wanted=record['new']['identity']
                    if observed.get('source') is not None or (action['kind']!='mkdir' and observed.get('destination')!=wanted): _refuse('Completed action has inconsistent identity evidence.')
                    if action['kind']=='mkdir' and not observed.get('destination'): _refuse('Created directory has no durable observed identity.')
                    if action['phase']=='done' and json.loads(action['observed_json'])!=observed: _refuse('SQL and durable completion marker disagree.')
                    if incomplete: _refuse('Completed actions are not an ordered prefix.')
                    action.update(phase='done',observed_json=canonical(observed));lag.append((action,record,done))
                elif action['phase']=='done': _refuse('Completed action marker is missing.')
                else:
                    if marker is not None:
                        if incomplete: _refuse('Multiple unresolved action intents are ambiguous.')
                        before,after=record['old'],record['new']
                        sf,sn,sm=self._physical(held,journal,before['space'],before['name'])
                        df,dn,dm=self._physical(held,journal,after['space'],after['name'])
                        for endpoint,parent in ((before,sf),(after,df)):
                            if parent is None or not _match(os.fstat(parent),endpoint.get('parent_identity')): _refuse('An action parent changed.')
                        kind=action['kind']
                        completed=(kind in ('move_old','publish_new') and sm is None and _match(dm,after['identity']) or kind=='rmdir' and sm is None)
                        before_ok=(_match(sm,before['identity']) and (kind not in ('move_old','publish_new') or dm is None))
                        if kind=='mkdir' and sm is not None: _refuse('A created directory has no recorded inode; ownership is ambiguous.')
                        if completed:
                            observed=dict(source=None,destination=after['identity'])
                            action.update(phase='done',observed_json=canonical(observed))
                            lag.append((action,record,dict(record=record,observed=observed)))
                        elif before_ok: action['phase']='intent'
                        else: _refuse('An interrupted action has no unambiguous pre/post state.')
                    incomplete=True
            self._verify_state(held,journal,intent,actions)
            if persist:
                # Sync reconciled renames before recording their completion.
                for descriptor in held.fds.values(): os.fsync(descriptor)
                for action,record,done in lag:
                    name=f"{action['ordinal']}.done.json"
                    if self._read_optional(fd,name) is None: self.publisher._record(fd,name,done)
                with self.catalog.connect(write=True) as db:
                    for action in actions:
                        db.execute('UPDATE integration_actions SET phase=?,old_json=?,new_json=?,observed_json=? WHERE operation_id=? AND ordinal=?',
                                   (action['phase'],action['old_json'],action['new_json'],action['observed_json'],journal.operation_id,action['ordinal']))
            return actions

    def continuation(self, journal):
        with self.catalog.connect() as db:
            rows=db.execute("SELECT r.location_json FROM integration_recoveries r JOIN operations o ON o.id=r.operation_id WHERE r.original_operation_id=? AND r.mode='resume' AND o.state='running'",(journal.operation_id,)).fetchall()
        if len(rows)!=1 or json.loads(rows[0][0]).get('phase')!='reconciled': _refuse('Publication continuation lacks explicit durable recovery authorization.')
        self._reconcile(journal,persist=False)

    def run(self, operation_id: str, request: dict) -> None:
        existing=self.catalog.find_operation(request)
        if existing != dict(operation_id=operation_id): _refuse('Recovery request does not match its accepted operation.')
        operation=self.catalog.get_operation(operation_id)
        if operation['state'] in ('succeeded','failed','interrupted'): return
        recovery,location=self._load(operation_id)
        with self.service.registry.lock, self.service.coordinator(self.catalog.registration['id']).lock:
            operation=self.catalog.get_operation(operation_id)
            if operation['state'] in ('succeeded','failed','interrupted'): return
            if operation['state']=='queued': self.catalog.transition(operation_id,'running')
        try:
            if location['requested']: self._handoff(operation_id,recovery,location)
            reserved=self.publisher._row(recovery['original_operation_id'])
            if reserved['identity_json'] is None:
                if reserved['phase']!='reserved' or os.path.lexists(reserved['journal_path']): _refuse('An unidentified journal entry requires conflict inspection.')
                plan=self.publisher.plans.load(reserved['plan_id'],reserved['revision'])
                self.claims.validate(plan,scan=True)
                if recovery['mode']=='restore-before':
                    self._finish_restore(operation_id,reserved,json.loads(reserved['intent_json']),location)
                    self.catalog.notify()
                    return
                self.publisher.stage(recovery['original_operation_id'],plan)
            journal=self._journal(recovery['original_operation_id'])
            if not location.get('restoration'):
                actions=self._reconcile(journal,persist=True)
                location['phase']='reconciled';self._save(operation_id,location)
            if recovery['mode']=='resume':
                with self.catalog.connect(write=True) as db:
                    db.execute("UPDATE integration_journals SET phase='applying' WHERE operation_id=?",(journal.operation_id,))
                self.publisher.publish(journal)
                self.publisher.verify_final(journal)
                from .integration_commit import IntegrationCommit
                with self.service.registry.lock, self.service.coordinator(self.catalog.registration['id']).lock, self.catalog.connect(write=True) as db:
                    IntegrationCommit(self.service,self.catalog).finish(db,journal,operation_id)
            else: self._restore(operation_id,journal,location)
        except Exception as error:
            with self.catalog.connect(write=True) as db:
                row=db.execute('SELECT state FROM operations WHERE id=?',(operation_id,)).fetchone()
                if row[0]=='running': self.catalog._transition(db,operation_id,'interrupted',error=classify(error).as_dict())
            if isinstance(error,CoreError): raise
            raise CoreError('RECOVERY_REQUIRED','Recovery stopped; retained storage requires inspection.','Reconnect the original storage or supply an explicit verified relocation.') from error
        self.catalog.notify()

    def _handoff(self, operation, recovery, location):
        original=recovery['original_operation_id'];row=self.publisher._row(original)
        intent=json.loads(row['intent_json'])
        if intent.get('format')!=1 or row['identity_json'] is None: _refuse('An unidentified journal cannot be relocated.')
        requested=location['requested']
        target=Path(requested['target_path']);journal_path=Path(requested['journal_path'])
        if location.get('mapping') is None:
            # Record exact old/new physical identities before changing any catalog path.
            mapping=dict(format=1,project_id=self.catalog.registration['id'],original_operation_id=original,confirmed_by=operation,
                         intent_sha256=hashlib.sha256(canonical(intent).encode()).hexdigest(),
                         old=dict(target=intent['target'],journal=intent['journal'],expected=intent['expected']),
                         new=dict(target=directory_identity(target,self.catalog.policy),
                                  journal=directory_identity(journal_path,self.catalog.policy),
                                  store=directory_identity(self.catalog.store,self.catalog.policy),
                                  ancestors=[directory_identity(p,self.catalog.policy) for p in target.parents]),state='intent')
            with self.service.registry.lock, self.service.coordinator(self.catalog.registration['id']).lock:
                proposed=Journal(original,row['plan_id'],row['revision'],journal_path,Identity(**mapping['new']['journal']),Identity(**mapping['new']['target']))
                self._validate_mapping(intent,proposed,mapping,requested)
                self._handoff_ownership(row,intent,proposed)
                location['mapping']=mapping;location['phase']='location_intent';self._save(operation,location)
            self.publisher._hit('location_intent')
        mapping=location['mapping']
        if mapping['new']['target']['path']!=str(target) or mapping['new']['journal']['path']!=str(journal_path):
            _refuse('The recovery request cannot replace its recorded location mapping.')
        journal=Journal(original,row['plan_id'],row['revision'],journal_path,Identity(**mapping['new']['journal']),Identity(**mapping['new']['target']))
        self._validate_mapping(intent,journal,mapping,requested)
        if mapping['state']=='applied': return
        # The immutable marker, slots, every old/new state and current protected
        # boundaries must be proven before publishing any new path metadata.
        if location.get('restoration'):
            with self.publisher._session(journal) as (held,physical,_):
                self._inspect_restoration(held,journal,physical,location['restoration'])
        else:
            self._reconcile(journal,persist=False)
        with self.service.registry.lock, self.service.coordinator(self.catalog.registration['id']).lock:
            self._validate_mapping(intent,journal,mapping,requested)
            owner=self._handoff_ownership(row,intent,journal)
            old_expected=intent['expected'];new_expected=self._mapped_expected(mapping)
            actual=self.publisher.plans._boundaries(original)
            # Metadata paths have not yet moved; compare using only typed store and
            # exact workspace-root substitutions, never arbitrary string replacement.
            actual=sorted(set(self._map_owned_path(p,mapping) for p in actual) |
                ({mapping['old']['target']['path']} if owner is not None and mapping['old']['target']['path'] in actual else set()))
            if actual!=new_expected['protected_roots']: _refuse('Protected boundaries changed during relocation.')
            mapping['state']='applied';location['phase']='location_applied'
            with self.catalog.connect(write=True) as db:
                db.execute('UPDATE integration_journals SET journal_path=?,identity_json=?,target_identity_json=? WHERE operation_id=?',
                           (str(journal.path),canonical(asdict(journal.identity)),canonical(asdict(journal.target)),original))
                for kind,identity in (('target',journal.target),('journal',journal.identity)):
                    db.execute('UPDATE integration_claims SET path=?,identity_json=? WHERE operation_id=? AND kind=?',
                               (identity.path,canonical(asdict(identity)),original,kind))
                if owner is not None:
                    identity=asdict(journal.target)
                    db.execute('UPDATE workspaces SET path=?,mount_id=?,device=?,inode=? WHERE id=? AND task_id=?',
                               (identity['path'],identity['mount_id'],identity['device'],identity['inode'],owner,old_expected['target']['task_id']))
                db.execute('UPDATE integration_recoveries SET location_json=? WHERE operation_id=?',(canonical(location),operation))
                self.catalog._event(db,dict(id=operation,actor='core'),'integration.location.reconciled','running',
                                   dict(original_operation_id=original,old=mapping['old']['target']['path'],new=str(target)))
        self.publisher._hit('location_committed')

    def _handoff_ownership(self, row, intent, journal):
        original=journal.operation_id;target=Path(journal.target.path);journal_path=journal.path
        self.claims.assert_access(target,operation_id=original)
        target_key=intent['expected']['target']['key']
        owner=target_key.split(':',1)[1] if target_key.startswith('workspace:') else None
        from .workspace_paths import overlap
        for record in self.service.registry.known_registrations():
            if record['id']!=self.catalog.registration['id'] and (overlap(target,Path(record['root'])) or overlap(journal_path,Path(record['root']))):
                _refuse('A relocated target or journal overlaps another registered project.')
        for path,path_owner in self.service.workspace_paths.managed_locations():
            if path==Path(row['journal_path']): continue
            if path==Path(json.loads(row['target_identity_json'])['path']) and path_owner in (None,owner): continue
            if owner is not None and path_owner==owner: continue
            if overlap(journal_path,path) or overlap(target,path):
                _refuse('A relocated location overlaps retained Solace ownership.')
        self.claims.assert_access(journal_path,operation_id=original)
        return owner

    @staticmethod
    def _map_owned_path(path, mapping):
        old=mapping['old'];new=mapping['new'];value=Path(path)
        old_store=Path(old['expected']['store_identity']['path'])
        if value.is_relative_to(old_store): return str(Path(new['store']['path'])/value.relative_to(old_store))
        if old['expected']['target']['task_id'] is not None and path==old['target']['path']: return new['target']['path']
        return path

    @classmethod
    def _mapped_expected(cls, mapping):
        expected=copy.deepcopy(mapping['old']['expected'])
        expected['target']['identity']=mapping['new']['target']
        expected['store_identity']=mapping['new']['store']
        expected['ancestors']=mapping['new']['ancestors']
        retained=expected['protected_roots']
        expected['protected_roots']=sorted(set(cls._map_owned_path(p,mapping) for p in retained) |
            ({mapping['old']['target']['path']} if expected['target']['task_id'] is not None and mapping['old']['target']['path'] in retained else set()))
        return expected

    def _validate_mapping(self, intent, journal, mapping, requested):
        if (set(mapping)!={'format','project_id','original_operation_id','confirmed_by','intent_sha256','old','new','state'} or
            mapping['format']!=1 or mapping['state'] not in ('intent','applied') or
            mapping['project_id']!=self.catalog.registration['id'] or mapping['original_operation_id']!=journal.operation_id or
            mapping['intent_sha256']!=hashlib.sha256(canonical(intent).encode()).hexdigest() or
            mapping['old']!=dict(target=intent['target'],journal=intent['journal'],expected=intent['expected']) or
            set(mapping['new'])!={'target','journal','store','ancestors'}): _refuse('The relocation mapping is incomplete or differs from immutable evidence.')
        with self.catalog.connect() as db:
            authority=db.execute('SELECT o.params_json,r.original_operation_id FROM operations o JOIN integration_recoveries r ON r.operation_id=o.id WHERE o.id=?',(mapping['confirmed_by'],)).fetchone()
        if authority is None or authority['original_operation_id']!=journal.operation_id: _refuse('Relocation has no original confirming request.')
        params=json.loads(authority['params_json'])
        if {k:params[k] for k in _GROUP if k in params}!=requested: _refuse('Relocation differs from its durable confirming request.')
        new=mapping['new'];old=mapping['old']
        if (new['target']!=asdict(journal.target) or new['journal']!=asdict(journal.identity) or
            requested!=dict(target_path=journal.target.path,journal_path=str(journal.path),confirmed_target=journal.target.path,confirmed_journal=str(journal.path))):
            _refuse('Relocation mapping differs from the accepted exact confirmations.')
        for key,path,original in (('target',Path(journal.target.path),old['target']),('journal',journal.path,old['journal']),('store',self.catalog.store,old['expected']['store_identity'])):
            actual=directory_identity(path,self.catalog.policy)
            if actual!=new[key] or any(actual[k]!=original[k] for k in ('device','inode')): _refuse('Relocation does not identify the recorded physical directory.')
        if new['ancestors']!=[directory_identity(p,self.catalog.policy) for p in Path(journal.target.path).parents]: _refuse('Relocation ancestor mapping changed.')
        if journal.path.parent!=Path(journal.target.path).parent or journal.identity.device!=journal.target.device:
            _refuse('The relocated journal must remain a same-filesystem target sibling.')
        logical=old['expected']['target']
        if logical['task_id'] is None:
            if journal.target.path!=self.catalog.registration['root']: _refuse('A source recovery must use its explicitly registered root.')
        else:
            self.service.workspace_paths.validate(Path(journal.target.path))
            with self.catalog.connect() as db:
                workspace=db.execute('SELECT task_id,path,device,inode FROM workspaces WHERE id=?',(logical['key'].split(':',1)[1],)).fetchone()
            if (not workspace or workspace['task_id']!=logical['task_id'] or workspace['path'] not in (old['target']['path'],new['target']['path']) or
                workspace['device']!=old['target']['device'] or workspace['inode']!=old['target']['inode']): _refuse('The moved workspace no longer has its recorded logical owner.')

    def physical_intent(self, intent, journal):
        """Return a checked physical view, leaving original intent/approval immutable."""
        if intent['target']==asdict(journal.target) and intent['journal']==asdict(journal.identity) and intent['expected']['store_identity']==directory_identity(self.catalog.store,self.catalog.policy): return intent
        with self.catalog.connect() as db:
            rows=db.execute('SELECT r.location_json,o.params_json,o.state FROM integration_recoveries r JOIN operations o ON o.id=r.operation_id WHERE r.original_operation_id=? ORDER BY r.rowid DESC',(journal.operation_id,)).fetchall()
        for row in rows:
            location=json.loads(row['location_json']);mapping=location.get('mapping')
            if not mapping: continue
            if mapping.get('state')=='intent' and row['state']!='running': _refuse('A pending relocation requires a new explicit recovery request.')
            requested=location.get('requested',{})
            if not requested: _refuse('The inherited mapping lost its exact confirmations.')
            self._validate_mapping(intent,journal,mapping,requested)
            value=copy.deepcopy(intent);value.update(target=asdict(journal.target),journal=asdict(journal.identity),
                parent_identity=mapping['new']['ancestors'][0],expected=self._mapped_expected(mapping))
            return value
        _refuse('Changed physical locations lack a verified recovery mapping.')

    def reconcile_location(self, previous: dict, request_id: str) -> None:
        """Record pending logical relocation; ordinary restart never adopts files."""
        with self.catalog.connect(write=True) as db:
            self.catalog._event(db,dict(id=None,actor='core'),'integration.location.pending','interrupted',dict(request_id=request_id,previous=previous))

    @staticmethod
    def _restore_steps(operation, actions):
        steps=[]
        for action in reversed(actions):
            if action['phase']!='done': continue
            before,after=json.loads(action['old_json']),json.loads(action['new_json'])
            observed=json.loads(action['observed_json'])['destination']
            if action['kind']=='publish_new':
                source=dict(after,identity=observed);destination=dict(space='retained',name='restore-'+operation+'-'+str(action['ordinal']),identity=observed,entry=after['entry']);kind='move'
            elif action['kind']=='move_old': source=after;destination=before;kind='move'
            elif action['kind']=='mkdir': source=dict(after,identity=observed);destination=dict(after,identity=None);kind='rmdir'
            else: source=dict(before,identity=None);destination=dict(before,identity=None);kind='mkdir'
            steps.append(dict(ordinal=len(steps),kind=kind,source=source,destination=destination,phase='planned',observed=None,
                              mode=stat.S_IMODE(before['identity']['mode']) if action['kind']=='rmdir' else None))
        return steps

    def _inspect_restoration(self, held, journal, intent, restoration):
        """Authenticate and normalize inverse evidence without writing any files."""
        actions=self.publisher._actions(journal.operation_id)
        self.publisher._validate_actions(intent,actions)
        fd=held.open(journal.path)
        # Forward progress was reconciled before restoration began. Authenticate
        # those markers, but never demand their superseded physical post-state.
        for action in actions:
            record=self._record(action)
            saved=self._read_optional(fd,f"{action['ordinal']}.intent.json")
            done=self._read_optional(fd,f"{action['ordinal']}.done.json")
            if action['phase']=='planned':
                if saved is not None or done is not None: _refuse('Unreconciled publication evidence during restoration.')
            elif saved!=record: _refuse('Publication intent changed during restoration.')
            if action['phase']=='done':
                if done!=dict(record=record,observed=json.loads(action['observed_json'])):
                    _refuse('Publication completion changed during restoration.')
            elif done is not None: _refuse('Unreconciled publication completion during restoration.')
        restored=copy.deepcopy(restoration)
        with self.catalog.connect() as db:
            owner=db.execute('SELECT original_operation_id,mode FROM integration_recoveries WHERE operation_id=?',(restored['owner'],)).fetchone()
        if owner is None or tuple(owner)!=(journal.operation_id,'restore-before'):
            _refuse('Restoration lacks its original recovery owner.')
        expected=self._restore_steps(restored['owner'],actions)
        if len(expected)!=len(restored['steps']): _refuse('The restoration action list changed.')
        lag=[];incomplete=False
        for step,wanted in zip(restored['steps'],expected):
            if any(step.get(k)!=v for k,v in wanted.items() if k not in ('phase','observed')):
                _refuse('A restoration step differs from the exact inverse publication.')
            if step['phase'] not in ('planned','intent','done'): _refuse('Invalid restoration progress.')
            marker='restore-'+restored['owner']+'-'+str(step['ordinal'])
            before,after=step['source'],step['destination']
            record=dict(kind=step['kind'],source=before,destination=after,mode=step['mode'])
            saved=self._read_optional(fd,marker+'.intent.json')
            done=self._read_optional(fd,marker+'.done.json')
            if saved is not None and saved!=record: _refuse('Restoration intent marker changed.')
            if saved is None and step['phase']!='planned': _refuse('Restoration progress lacks its durable intent.')
            if done is not None:
                if saved is None or done.get('record')!=record: _refuse('Restoration completion marker changed.')
                observed=done.get('observed')
                if (step['kind']!='mkdir' and observed!=after['identity']) or (step['kind']=='mkdir' and not observed):
                    _refuse('Restoration completion does not identify its recorded version.')
                if step['phase']=='done' and step['observed']!=observed:
                    _refuse('Restoration SQL and marker progress disagree.')
                step.update(phase='done',observed=observed)
            elif step['phase']=='done': _refuse('A completed restoration action lost its durable marker.')
            elif saved is not None:
                if incomplete: _refuse('Multiple unresolved restoration intents are ambiguous.')
                sf,sn,sm=self._physical(held,journal,before['space'],before['name'])
                df,dn,dm=self._physical(held,journal,after['space'],after['name'])
                if sf is None or df is None: _refuse('A restoration parent is unavailable.')
                completed=(step['kind']=='move' and sm is None and _match(dm,after['identity']) or step['kind']=='rmdir' and sm is None)
                if step['kind']=='mkdir' and sm is not None: _refuse('A restored directory lacks a recorded inode; preserve it for inspection.')
                if completed:
                    step.update(phase='done',observed=after['identity'])
                    lag.append((marker,record,step['observed']))
                else: step['phase']='intent'
            if step['phase']=='done':
                if incomplete: _refuse('Completed restoration actions are not an ordered prefix.')
            else: incomplete=True
        self._verify_restoration(held,journal,intent,actions,restored)
        return restored,lag

    def _restore(self, operation, journal, location):
        with self.publisher._session(journal) as (held,intent,row):
            actions=self.publisher._actions(journal.operation_id)
            self.publisher._validate_actions(intent,actions)
            if not location.get('restoration'):
                steps=self._restore_steps(operation,actions)
                location['restoration']=dict(owner=operation,steps=steps)
                location['phase']='restoring';self._save(operation,location)
            restoration,lag=self._inspect_restoration(held,journal,intent,location['restoration'])
            fd=held.open(journal.path)
            # Validation is read-only during handoff. Persist normalized inverse
            # progress here, after syncing both directories of every lagged move.
            for descriptor in held.fds.values(): os.fsync(descriptor)
            for marker,record,observed in lag:
                self.publisher._record(fd,marker+'.done.json',dict(record=record,observed=observed))
            location['restoration']=restoration;self._save(operation,location)
            for step in restoration['steps']:
                if step['phase']=='done': continue
                marker='restore-'+restoration['owner']+'-'+str(step['ordinal'])
                before,after=step['source'],step['destination']
                sf,sn,sm=self._physical(held,journal,before['space'],before['name'])
                df,dn,dm=self._physical(held,journal,after['space'],after['name'])
                if sf is None or df is None: _refuse('A restoration parent is unavailable.')
                record=dict(kind=step['kind'],source=before,destination=after,mode=step['mode'])
                saved=self._read_optional(fd,marker+'.intent.json')
                self._verify_restoration(held,journal,intent,actions,restoration)
                if not _match(sm,before['identity']): _refuse('The restoration source changed.')
                if step['kind']=='move':
                    self.publisher._content(sf,sn,entry_from_json(before['entry']),before['identity'])
                    if dm is not None: _refuse('Another writer occupies the restoration destination.')
                elif step['kind']=='rmdir':
                    if os.listdir(held.open(Path(journal.target.path)/before['name'])): _refuse('An operation-created directory is no longer empty.')
                if saved is None: self.publisher._record(fd,marker+'.intent.json',record)
                step['phase']='intent';self._save(operation,location)
                self.publisher._hit('restore_intent:'+str(step['ordinal']));held.check()
                if not _match(_stat(sf,sn),before['identity']): _refuse('The restoration source changed after durable intent.')
                if step['kind']=='move': _rename_noreplace(sn,dn,sf,df)
                elif step['kind']=='mkdir':
                    os.mkdir(dn,mode=step['mode'],dir_fd=df)
                    os.fchmod(held.open(Path(journal.target.path)/after['name']),step['mode'])
                else:
                    path=Path(journal.target.path)/before['name']
                    removed=held.open(path);os.rmdir(sn,dir_fd=sf);os.fsync(removed);held.forget(path)
                self.publisher._hit('restore_changed:'+str(step['ordinal']))
                os.fsync(sf);os.fsync(df);held.check()
                meta=_stat(df,dn);observed=_identity(meta) if meta else None
                if step['kind']=='move': self.publisher._content(df,dn,entry_from_json(after['entry']),after['identity'],sync=True)
                if step['kind']=='rmdir' and meta is not None: _refuse('A removed directory was reoccupied.')
                if step['kind']=='mkdir':
                    directory=held.open(Path(journal.target.path)/after['name']);os.fsync(directory)
                    if not _match(os.fstat(directory),observed): _refuse('A recreated directory changed.')
                self.publisher._record(fd,marker+'.done.json',dict(record=record,observed=observed))
                step.update(phase='done',observed=observed);self._save(operation,location)
                self.publisher._hit('restore_progress:'+str(step['ordinal']))
            self._verify_restoration(held,journal,intent,actions,restoration)
            self.publisher._verify_scope(journal,intent,tuple(entry_from_json(e) for e in intent['current_entries']))
            for descriptor in held.fds.values(): os.fsync(descriptor)
            self.publisher._hit('restore_verified')
            self._verify_restoration(held,journal,intent,actions,restoration)
            self.publisher._verify_scope(journal,intent,tuple(entry_from_json(e) for e in intent['current_entries']))
            self._finish_restore(operation,row,intent,location)

    def _finish_restore(self, operation, row, intent, location):
        with self.service.registry.lock, self.service.coordinator(self.catalog.registration['id']).lock, self.catalog.connect(write=True) as db:
            self._restoration_metadata(db,row,intent)
            result=dict(operation_id=operation,original_operation_id=row['operation_id'],plan_id=row['plan_id'],
                        revision=row['revision'],state='restored',before_id=intent['current']['checkpoint_id'],requires_fresh_plan=True)
            db.execute("UPDATE integration_plans SET state='restored',updated_at=? WHERE id=?",(self.catalog._now(),row['plan_id']))
            db.execute("UPDATE integration_journals SET phase='restored' WHERE operation_id=?",(row['operation_id'],))
            db.execute('UPDATE integration_recoveries SET result_json=?,location_json=? WHERE operation_id=?',(canonical(result),canonical(location),operation))
            self.catalog._transition(db,operation,'succeeded',result=result)
            self.claims.release(db,row['operation_id'])

    def _restoration_metadata(self, db, row, intent):
        """Recheck pinned metadata and exact original-owner claims before release."""
        current=db.execute('SELECT * FROM integration_journals WHERE operation_id=?',(row['operation_id'],)).fetchone()
        if current is None or any(current[key]!=row[key] for key in ('plan_id','revision','journal_path','identity_json','target_identity_json','intent_json')):
            _refuse('Restoration journal ownership or intent changed.')
        expected=intent['expected'];target=expected['target']
        plan=db.execute('SELECT * FROM integration_plans WHERE id=?',(row['plan_id'],)).fetchone()
        revision=db.execute('SELECT expected_json FROM integration_revisions WHERE plan_id=? AND revision=?',(row['plan_id'],row['revision'])).fetchone()
        if (not plan or not revision or plan['current_revision']!=row['revision'] or plan['state'] not in ('applying','recovery_required') or
            json.loads(revision[0])!=json.loads(row['intent_json'])['expected']): _refuse('The retained restoration review changed.')
        params=dict(task=plan['task_id'],result=plan['result_id'],expected_revision=expected['source_revision'])
        if target['task_id'] is not None: params['expected_target_revision']=target['revision']
        _,fresh_target,base,previous=self.publisher.plans._inputs(db,params)
        fresh=self.publisher.plans._expected(db,params,fresh_target,self.publisher.plans._boundaries(row['operation_id']))
        if (base!=intent['base']['checkpoint_id'] or previous!=intent['previous_receipt_id'] or
            {k:v for k,v in fresh.items() if k!='directory_inventories'}!={k:v for k,v in expected.items() if k!='directory_inventories'}):
            _refuse('Pinned restoration ownership, heads or protected boundaries changed.')
        owned={r[0] for r in db.execute('SELECT task_id FROM task_claims WHERE operation_id=?',(row['operation_id'],))}
        if owned!={plan['task_id'],*([target['task_id']] if target['task_id'] else [])}: _refuse('Restoration no longer owns its exact tasks.')
        claims=[dict(r) for r in db.execute('SELECT * FROM integration_claims WHERE operation_id=?',(row['operation_id'],))]
        if len(claims)!=2 or {r['kind'] for r in claims}!={'target','journal'}: _refuse('Restoration no longer owns its exact paths.')
        for claim in claims:
            identity=json.loads(row['target_identity_json'] if claim['kind']=='target' else row['identity_json'] or 'null')
            path=identity['path'] if identity else row['journal_path']
            if claim['path']!=path or json.loads(claim['identity_json'] or 'null')!=identity: _refuse('Restoration path claims changed.')
        self.claims.assert_access(Path(target['identity']['path']),operation_id=row['operation_id'])

    def _verify_restoration(self, held, journal, intent, actions, restoration):
        state=self._state(intent,actions)
        for step in restoration['steps']:
            before,after=step['source'],step['destination']
            state.setdefault((after['space'],after['name']),None)
            if step['phase']=='done':
                state[(before['space'],before['name'])]=None
                state[(after['space'],after['name'])]=None if step['kind']=='rmdir' else dict(identity=step['observed'],entry=after['entry'])
        for (space,name),expected in state.items():
            fd,leaf,meta=self._physical(held,journal,space,name)
            if not _match(meta,expected['identity'] if expected else None): _refuse('Restoration evidence has a missing, replaced or ambiguous entry.')
            if expected and expected['entry']: self.publisher._content(fd,leaf,entry_from_json(expected['entry']),expected['identity'])
        entries=tuple(entry_from_json(value['entry']) for (space,_),value in state.items() if space=='target' and value and value['entry'])
        self.publisher._verify_scope(journal,intent,entries)
        held.check()
