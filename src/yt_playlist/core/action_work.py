"""Relational action admission and recovery, with exact legacy evidence files.

The Action interface still exposes historical text formats. Queried ownership,
operation order, backup availability and progress come from these SQL owners.
"""
import hashlib
import json
from uuid import uuid4

from . import collection_recovery, playlist_scopes
from .evidence_ledger import canonical

OPERATIONS = {'backup':'restore_backup','keeper':'restore_keeper','copy':'delete_copy'}


def available(conn):
    return any(row[1]=='idempotency_key' for row in conn.execute('PRAGMA table_info(actions)'))


def imported_pending(action, step):
    """The pre98 journal did not distinguish an unsent call from interruption."""
    return (action['idempotency_key'] == 'legacy-action:' + str(action['id'])
            and step['operation'] == 'delete_playlist' and step['phase'] == 'pending'
            and step['outcome'] == 'not_sent')


def require_known_remote_outcome(conn, account, remote):
    """Fence forward edits while the installed deletion owner needs readback."""
    if available(conn):
        uncertain = conn.execute(
            'SELECT 1 FROM action_targets t JOIN action_steps s ON s.target_id=t.id '
            'JOIN actions a ON a.id=s.action_id WHERE t.account_id=? '
            'AND t.provider_playlist_id_snapshot=? AND a.status NOT IN (\'undone\',\'cancelled\') '
            "AND (s.outcome='unknown' OR (s.operation='delete_playlist' AND s.phase='pending' "
            "AND s.outcome='not_sent' AND a.idempotency_key='legacy-action:'||a.id)) LIMIT 1",
            (account, remote)).fetchone()
    else:
        uncertain = conn.execute(
            "SELECT 1 FROM actions a WHERE a.status='delete_pending' AND EXISTS ("
            "SELECT 1 FROM json_each(a.undo_json,'$.deletions') d "
            "WHERE json_extract(d.value,'$.identity')=? AND json_extract(d.value,'$.ytm')=? "
            "AND json_extract(d.value,'$.phase') IN ('pending','uncertain')) LIMIT 1",
            (account, remote)).fetchone()
    if uncertain:
        raise ValueError('The prior remote outcome is unknown; reconcile it before another write')


def decode(raw, *, draft=False):
    if raw is None:
        return None
    if draft:
        if not isinstance(raw,str):
            raise ValueError('A merge draft signature requires text')
        return raw
    value = json.loads(raw)
    if not isinstance(value,(dict,list)):
        raise ValueError('Unsupported action document')
    return value


def backup_reference(filename, *, legacy=False):
    if filename is None:
        return (None,None,None,'none'),None
    if not isinstance(filename,str) or not filename:
        raise ValueError('Action backup requires a filename')
    try:
        from .action_backup_files import verified
        encoded,_,_ = verified(filename)
    except FileNotFoundError:
        if legacy:
            return (filename,None,None,'missing_historical'),None
        raise ValueError('Action backup is missing; remote mutation was not admitted') from None
    value = json.loads(encoded)
    if (not isinstance(value,dict) or value.get('snapshot_version',1) not in (1,2)
            or not isinstance(value.get('tracks'),list) or not value.get('ytm_playlist_id')):
        raise ValueError('Unsupported playlist recovery backup')
    return (filename,hashlib.sha256(encoded).hexdigest(),len(encoded),'verified'),value


def _target(conn, action_id, role, position, *, account=None, remote=None, membership=None, legacy=False):
    prior = conn.execute('SELECT * FROM action_targets WHERE action_id=? AND role=? AND position=?',
                         (action_id,role,position)).fetchone()
    if (prior and prior['resolution_reason']=='membership_snapshot_mismatch' and account is None
            and prior['unresolved_membership_ref_id']==membership
            and prior['provider_playlist_id_snapshot']==remote):
        return prior['id']
    from . import entity_names
    scope_column,account_column = entity_names.membership_columns(conn)
    retained = conn.execute(f'SELECT {scope_column} AS playlist_scope_id,{account_column} AS account_id FROM playlist_membership_refs WHERE id=?',
                            (membership,)).fetchone() if membership is not None else None
    scope = None
    reason = None
    if retained and retained['playlist_scope_id'] is not None:
        scope_key,remote_column,_ = entity_names.scope_columns(conn)
        owner = conn.execute(f'SELECT {remote_column} FROM playlist_scopes WHERE {scope_key}=?',(retained['playlist_scope_id'],)).fetchone()
        if (account is not None and account!=retained['account_id']) or (remote is not None and remote!=owner[0]):
            if not legacy:
                raise ValueError('Action target disagrees with its retained membership')
            # Early action history used reusable local playlist IDs. Preserve
            # that captured ID without assigning the newer membership's account.
            retained = None
            reason = 'membership_snapshot_mismatch'
        else:
            account,remote,scope = retained['account_id'],owner[0],retained['playlist_scope_id']
    elif membership is not None:
        reason = 'missing_membership'
    if remote is None:
        # A historical local identifier cannot be resolved from a new membership
        # with the same display title or a replacement account.
        remote = f'@unresolved-membership:{membership}'
    if not isinstance(remote,str) or not remote:
        raise ValueError('Action target requires its captured remote identifier')
    if account is not None and type(account) is not int:
        raise ValueError('Action account requires an integer identity')
    known = conn.execute(f'SELECT id FROM {entity_names.accounts(conn)} WHERE id=?',(account,)).fetchone() if account is not None else None
    if not known:
        reason = reason or ('missing_account' if account is not None else 'unrecorded_account')
    elif scope is None and not remote.startswith('@unresolved-membership:'):
        scope = playlist_scopes.ensure(conn,remote,account)
    values = (known[0] if known else None,None if known else account,scope,remote,
              membership if retained else None,None if retained else membership,reason)
    columns = ('account_id','unresolved_account_id','playlist_scope_id','provider_playlist_id_snapshot',
               'membership_ref_id','unresolved_membership_ref_id','resolution_reason')
    if prior:
        if tuple(prior[column] for column in columns)!=values:
            raise ValueError('A committed action target cannot change')
        return prior['id']
    return conn.execute('INSERT INTO action_targets(action_id,role,position,'+','.join(columns)+') '
                        'VALUES (?,?,?,?,?,?,?,?,?,?)',(action_id,role,position,*values)).lastrowid


def _step(conn,action_id,position,operation,value,now,*,target=None,backup=None,legacy=False):
    if not isinstance(value,dict):
        raise ValueError('An action recovery step requires an object')
    if backup is not None:
        from . import action_backup_files
        backup = action_backup_files.relocate(conn,action_id,backup)
    phase = value.get('phase','pending')
    action = conn.execute('SELECT id,idempotency_key FROM actions WHERE id=?', (action_id,)).fetchone()
    if ((legacy and phase=='pending' and operation=='delete_playlist') or imported_pending(
            action, {'operation':operation, 'phase':phase, 'outcome':'not_sent'})):
        # The old journal committed pending before the provider call, without a
        # separate transport-admission checkpoint. It cannot prove an unsent call.
        phase = 'uncertain'
    outcome = ('unknown' if phase in ('uncertain','creating','deleting') or
               (phase=='restoring' and value.get('observed') is None) else
               'complete' if phase=='complete' else 'confirmed_absent' if phase=='remote_complete' and
               operation in ('delete_playlist','delete_copy') else 'confirmed_present' if phase in ('failed','remote_complete','restoring') else 'not_sent')
    prior = conn.execute('SELECT id,target_id,operation,backup_filename,backup_status FROM action_steps WHERE action_id=? AND position=?',
                         (action_id,position)).fetchone()
    historical = bool(prior and prior['backup_status']=='missing_historical' and prior['backup_filename']==backup)
    reference,_ = backup_reference(backup,legacy=legacy or historical)
    if prior and (prior['operation']!=operation or prior['target_id']!=target):
        raise ValueError('A committed recovery step cannot change its operation or target')
    identity = prior['id'] if prior else conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM action_steps').fetchone()[0]
    evidence = collection_recovery.publish(conn,'action-step',identity,value)
    created,created_scope = value.get('created_id'),None
    if created is not None:
        owner = conn.execute('SELECT account_id FROM action_targets WHERE id=?',(target,)).fetchone()
        if owner and owner[0] is not None:
            created_scope = playlist_scopes.ensure(conn,created,owner[0])
    observed = 'unrecorded' if 'observed' not in value else 'unknown' if value['observed'] is None else 'present'
    conn.execute('INSERT INTO action_steps(id,action_id,target_id,position,operation,phase,outcome,'
                 'creation_marker,created_provider_id_snapshot,created_playlist_scope_id,original_survived,observed_status,'
                 'backup_filename,backup_digest,backup_bytes,backup_status,updated_at,recovery_filename,recovery_digest,recovery_bytes) '
                 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET phase=excluded.phase,'
                 'outcome=excluded.outcome,backup_filename=excluded.backup_filename,backup_digest=excluded.backup_digest,'
                 'backup_bytes=excluded.backup_bytes,backup_status=excluded.backup_status,updated_at=excluded.updated_at,'
                 'creation_marker=excluded.creation_marker,created_provider_id_snapshot=excluded.created_provider_id_snapshot,'
                 'created_playlist_scope_id=excluded.created_playlist_scope_id,original_survived=excluded.original_survived,'
                 'observed_status=excluded.observed_status,'
                 'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
                 (identity,action_id,target,position,operation,phase,outcome,value.get('marker'),created,created_scope,
                  value.get('original_survived'),observed,*reference,now,*evidence))


def sync_progress(conn,action_id,kind,params,plan,undo,now,*,legacy=False,status=None):
    """Decode the finite legacy formats once at the repository boundary."""
    params = params if isinstance(params,dict) else {}
    plan = plan if isinstance(plan,dict) else {}
    undo = undo if isinstance(undo,dict) else {}
    backups = list(undo.get('backups',[]))
    backups.extend(entry['backup'] for entry in undo.get('restored',[]) if entry.get('backup'))
    if undo.get('backup'):
        backups.append(undo['backup'])
    backups.extend(entry['backup'] for entry in undo.get('deletions',[]) if entry.get('backup'))
    backups.extend(entry.get('backup') or entry['deletion_backup'] for entry in
        (undo.get('recovery') or {}).get('steps',[]) if entry.get('backup') or entry.get('deletion_backup'))
    for filename in dict.fromkeys(backups):
        if not conn.execute('SELECT 1 FROM action_backups WHERE action_id=? AND (filename=? OR retained_filename=?)',
                            (action_id,filename,filename)).fetchone():
            from . import action_backup_files
            reference,_ = backup_reference(filename,legacy=legacy)
            position = conn.execute('SELECT COALESCE(MAX(position),-1)+1 FROM action_backups WHERE action_id=?',(action_id,)).fetchone()[0]
            retained = action_backup_files.retain(conn,filename)[0] if reference[3]=='verified' else None
            conn.execute('INSERT INTO action_backups(action_id,position,filename,retained_filename,digest,bytes,availability) '
                         'VALUES (?,?,?,?,?,?,?)',(action_id,position,reference[0],retained,*reference[1:]))
    for role,field in (('source','source'),('destination','target')):
        if type(params.get(field)) is int:
            _target(conn,action_id,role,0,membership=params[field],remote=params.get('source_ytm') if role=='source' else None,legacy=legacy)
    if type(plan.get('playlist_id')) is int:
        target = _target(conn,action_id,'placement',0,membership=plan['playlist_id'])
        if not conn.execute('SELECT 1 FROM action_placement_intents WHERE action_id=?',(action_id,)).fetchone():
            videos,handles = plan.get('video_ids',[]),plan.get('set_video_ids',{})
            if (not isinstance(videos,list) or any(not isinstance(v,str) or not v for v in videos)
                    or not isinstance(handles,dict) or any(not isinstance(v,str) or not v or not isinstance(h,str) or not h
                                                         for v,h in handles.items())):
                raise ValueError('Unsupported ordered action placement')
            conn.execute('INSERT INTO action_placement_intents VALUES (?,?,?,?)',
                         (action_id,target,plan.get('after_video_id'),int('set_video_ids' in plan)))
            conn.executemany('INSERT INTO action_placement_items VALUES (?,?,?)',
                             [(action_id,index,video) for index,video in enumerate(videos)])
            conn.executemany('INSERT INTO action_placement_handles VALUES (?,?,?)',
                             [(action_id,video,handle) for video,handle in handles.items()])
        phase = 'complete' if status=='executed' else 'cancelled' if status=='cancelled' else 'pending'
        _step(conn,action_id,750000,'place_tracks',{'phase':phase},now,target=target,legacy=legacy)
    if kind=='plan' and not conn.execute('SELECT 1 FROM action_plan_resolutions WHERE action_id=?',(action_id,)).fetchone():
        for list_kind in ('additions','unresolved'):
            for position,item in enumerate(plan.get(list_kind,[])):
                if not isinstance(item,dict) or set(item)!={'identity_key','source_video_id','target_video_id','method'}:
                    raise ValueError('Unsupported merge plan resolution')
                recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(item['identity_key'],)).fetchone()
                conn.execute('INSERT INTO action_plan_resolutions VALUES (?,?,?,?,?,?,?,?,?)',
                             (action_id,list_kind,position,recording[0] if recording else None,
                              None if recording else item['identity_key'],item['identity_key'],
                              item['source_video_id'],item['target_video_id'],item['method']))
    for position,entry in enumerate(undo.get('restored',[])):
        target = _target(conn,action_id,'keeper',position,account=entry.get('identity'),remote=entry.get('ytm'))
        if entry.get('backup'):
            _step(conn,action_id,500000+position,'merge_plan',entry,now,target=target,
                  backup=entry['backup'],legacy=legacy)
    if undo.get('new_ytm') is not None:
        _target(conn,action_id,'destination',0,account=undo.get('target_identity'),remote=undo['new_ytm'])
    for position,entry in enumerate(undo.get('deletions',[])):
        target = _target(conn,action_id,'deletion',position,account=entry.get('identity'),remote=entry.get('ytm'))
        _step(conn,action_id,position,'delete_playlist',entry,now,target=target,
              backup=entry.get('backup'),legacy=legacy)
    journal = undo.get('recovery')
    if journal is not None:
        if not isinstance(journal,dict) or journal.get('version') not in (1,2) or not isinstance(journal.get('steps'),list):
            raise ValueError('Unsupported action undo journal')
        entries = sorted(journal['steps'],key=lambda step:step['kind']!='backup') if journal['version']==1 else journal['steps']
        for position,entry in enumerate(entries):
            operation = OPERATIONS.get(entry.get('kind'))
            if operation is None:
                raise ValueError('Unsupported action undo operation')
            account,remote = None,None
            backup = entry.get('backup') if operation=='restore_backup' else entry.get('deletion_backup') if operation=='delete_copy' else None
            if operation=='restore_keeper':
                account,remote = entry['entry'].get('identity'),entry['entry'].get('ytm')
            elif operation=='delete_copy':
                account,remote = entry.get('identity'),entry.get('ytm')
            elif backup:
                _,captured = backup_reference(backup,legacy=legacy)
                if captured:
                    account,remote = captured.get('identity_id'),captured.get('ytm_playlist_id')
            target = (_target(conn,action_id,'recovery',position,account=account,remote=remote)
                      if remote is not None else None)
            _step(conn,action_id,1000000+position,operation,entry,now,target=target,backup=backup,legacy=legacy)


def insert(conn,kind,params_raw,plan_raw,status,undo_raw,created_at,*,identity=None,executed_at=None,legacy=False,idempotency_key=None,reviewed_draft=None):
    draft = kind=='merge_draft'
    params,plan,undo = decode(params_raw),decode(plan_raw,draft=draft),decode(undo_raw,draft=draft)
    intent = {'kind':kind,'params':params,'plan':plan}
    if reviewed_draft is not None:
        intent['reviewed_draft'] = list(reviewed_draft)
    intent_digest = hashlib.sha256(canonical(intent)).hexdigest()
    conn.execute('UPDATE actions SET revision=revision WHERE 0')
    if idempotency_key is not None:
        previous = conn.execute('SELECT id,intent_digest FROM actions WHERE idempotency_key=?',(idempotency_key,)).fetchone()
        if previous:
            if previous['intent_digest']!=intent_digest:
                raise ValueError('Idempotency key belongs to another action intent')
            return previous[0]
    identity = identity if identity is not None else conn.execute(
        "SELECT MAX(COALESCE((SELECT MAX(id) FROM actions),0),COALESCE((SELECT seq FROM sqlite_sequence WHERE name='actions'),0))+1").fetchone()[0]
    revision = uuid4().hex
    captured = {'params':params_raw,'plan':plan_raw,'undo':undo_raw}
    reviewed = None
    if reviewed_draft is not None:
        reviewed = conn.execute('SELECT * FROM action_drafts WHERE id=? AND revision=?',reviewed_draft).fetchone()
        if reviewed is None:
            raise ValueError('The merge draft changed. Reload it before applying the merge.')
        if conn.execute('SELECT 1 FROM actions WHERE reviewed_draft_id=? AND reviewed_draft_revision=?',reviewed_draft).fetchone():
            raise ValueError('This draft revision already has an action. Check the Action Log before continuing.')
        captured['reviewed_draft'] = {'id':reviewed['id'],'revision':reviewed['revision'],
            'signature':reviewed['signature'],'params':collection_recovery.read(
                reviewed['recovery_filename'],reviewed['recovery_digest'],reviewed['recovery_bytes'],
                kind='action-draft',owner=reviewed['id'])}
    reference = collection_recovery.publish(conn,'action',identity,captured)
    undo_of = params.get('undid') if kind=='undo' and isinstance(params,dict) else None
    if undo_of is not None and not conn.execute('SELECT 1 FROM actions WHERE id=?',(undo_of,)).fetchone():
        raise ValueError('Undo history refers to a missing action')
    mode = params.get('mode') if kind=='plan' and isinstance(params,dict) else None
    conn.execute('INSERT INTO actions(id,kind,status,revision,idempotency_key,intent_digest,undo_of_action_id,execution_mode,'
                 'created_at,executed_at,recovery_filename,recovery_digest,recovery_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (identity,kind,status,revision,idempotency_key or 'action:'+uuid4().hex,intent_digest,undo_of,mode,
                  created_at,executed_at,*reference))
    if reviewed is not None:
        conn.execute('UPDATE actions SET reviewed_draft_id=?,reviewed_draft_revision=?,reviewed_draft_signature=? WHERE id=?',
                     (reviewed['id'],reviewed['revision'],reviewed['signature'],identity))
    if draft:
        evidence = collection_recovery.publish(conn,'action-draft',identity,params_raw)
        conn.execute('INSERT INTO action_drafts(id,signature,revision,updated_at,recovery_filename,recovery_digest,recovery_bytes) '
                     'VALUES (?,?,?,?,?,?,?)',(identity,plan,revision,created_at,*evidence))
    else:
        sync_progress(conn,identity,kind,params,plan,undo,executed_at,legacy=legacy,status=status)
    return identity


@collection_recovery.pinned_read
def document(conn,row):
    value = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                     kind='action',owner=row['id'])
    if row['kind']=='merge_draft':
        draft = conn.execute('SELECT * FROM action_drafts WHERE id=?',(row['id'],)).fetchone()
        value['plan'] = draft['signature']
        value['params'] = collection_recovery.read(draft['recovery_filename'],draft['recovery_digest'],draft['recovery_bytes'],
                                                  kind='action-draft',owner=row['id'])
        return value
    params,plan = decode(value['params']),decode(value['plan'])
    if row['kind']=='plan' and isinstance(params,dict) and isinstance(plan,dict):
        params['mode'] = row['execution_mode']
        for role,field in (('source','source'),('destination','target')):
            target = conn.execute('SELECT * FROM action_targets WHERE action_id=? AND role=? AND position=0',
                                  (row['id'],role)).fetchone()
            if target:
                params[field] = target['membership_ref_id'] if target['membership_ref_id'] is not None else target['unresolved_membership_ref_id']
                if role=='source':
                    params['source_ytm'] = target['provider_playlist_id_snapshot']
        for list_kind in ('additions','unresolved'):
            if list_kind in plan:
                plan[list_kind] = [dict(identity_key=item['captured_identity_key'],source_video_id=item['source_video_id_snapshot'],
                                        target_video_id=item['target_video_id_snapshot'],method=item['method'])
                    for item in conn.execute('SELECT * FROM action_plan_resolutions WHERE action_id=? AND list_kind=? ORDER BY position',
                                             (row['id'],list_kind))]
        value['params'],value['plan'] = json.dumps(params),json.dumps(plan)
    placement = conn.execute('SELECT p.*,t.membership_ref_id,t.unresolved_membership_ref_id FROM action_placement_intents p '
                              'JOIN action_targets t ON t.id=p.target_id WHERE p.action_id=?',(row['id'],)).fetchone()
    if placement is not None and isinstance(plan,dict):
        plan['playlist_id'] = placement['membership_ref_id'] if placement['membership_ref_id'] is not None else placement['unresolved_membership_ref_id']
        plan['after_video_id'] = placement['after_video_id_snapshot']
        plan['video_ids'] = [item[0] for item in conn.execute('SELECT video_id_snapshot FROM action_placement_items '
                                                            'WHERE action_id=? ORDER BY position',(row['id'],))]
        if placement['handles_present']:
            plan['set_video_ids'] = dict(conn.execute('SELECT video_id_snapshot,occurrence_handle_snapshot FROM action_placement_handles WHERE action_id=?',
                                                      (row['id'],)))
        value['plan'] = json.dumps(plan)
    undo = decode(value['undo'])
    if isinstance(undo,dict):
        from . import action_backup_files
        changed = False
        if undo.get('backup'):
            undo['backup'] = action_backup_files.relocate(conn,row['id'],undo['backup'])
            changed = True
        if 'backups' in undo:
            undo['backups'] = [action_backup_files.relocate(conn,row['id'],filename) for filename in undo['backups']]
            changed = True
        if isinstance(undo.get('recovery'),dict) and undo['recovery'].get('version')==1:
            undo['recovery']['steps'].sort(key=lambda step:step['kind']!='backup')
            undo['recovery']['version'] = 2
            changed = True
        for step in conn.execute('SELECT * FROM action_steps WHERE action_id=? ORDER BY position',(row['id'],)):
            if step['operation']=='place_tracks':
                continue
            entry = collection_recovery.read(step['recovery_filename'],step['recovery_digest'],step['recovery_bytes'],
                                             kind='action-step',owner=step['id'])
            phase = 'uncertain' if imported_pending(row, step) else step['phase']
            if 'phase' in entry or entry.get('phase')!=phase:
                entry['phase'] = phase
            if step['backup_filename'] is not None:
                field = 'deletion_backup' if step['operation']=='delete_copy' else 'backup'
                entry[field] = action_backup_files.relocate(conn,row['id'],step['backup_filename'])
            for column,field in (('creation_marker','marker'),('created_provider_id_snapshot','created_id'),
                                 ('original_survived','original_survived')):
                if step[column] is not None:
                    entry[field] = bool(step[column]) if field=='original_survived' else step[column]
            if step['observed_status']=='unknown':
                entry['observed'] = None
            if step['operation']=='delete_playlist':
                target = conn.execute('SELECT * FROM action_targets WHERE id=?',(step['target_id'],)).fetchone()
                entry['identity'] = target['account_id'] if target['account_id'] is not None else target['unresolved_account_id']
                entry['ytm'] = target['provider_playlist_id_snapshot']
                undo['deletions'][step['position']] = entry
            elif step['operation']=='merge_plan':
                undo['restored'][step['position']-500000] = entry
            else:
                undo['recovery']['steps'][step['position']-1000000] = entry
            changed = True
        if changed:
            value['undo'] = json.dumps(undo)
    return value


@collection_recovery.pinned_read
def records(conn,where='',values=(),*,order='id DESC'):
    return [(row,document(conn,row)) for row in conn.execute(
        'SELECT * FROM actions '+where+' ORDER BY '+order,values).fetchall()]


def update(conn,action_id,status,now,*,undo_raw=None,params_raw=None,expected_revision=None):
    conn.execute('UPDATE actions SET revision=revision WHERE 0')
    row = conn.execute('SELECT * FROM actions WHERE id=?',(action_id,)).fetchone()
    if row is None:
        raise ValueError('Action no longer exists')
    if expected_revision is not None and row['revision']!=expected_revision:
        raise ValueError('Action was updated by another worker')
    value = document(conn,row)
    if undo_raw is not None:
        value['undo'] = undo_raw
    if params_raw is not None:
        value['params'] = params_raw
    params,plan,undo = decode(value['params']),decode(value['plan']),decode(value['undo'])
    sync_progress(conn,action_id,row['kind'],params,plan,undo,now,status=status)
    reference = collection_recovery.publish(conn,'action',action_id,value)
    conn.execute('UPDATE actions SET status=?,executed_at=?,revision=?,recovery_filename=?,recovery_digest=?,recovery_bytes=? '
                 'WHERE id=?',(status,now,uuid4().hex,*reference,action_id))


def update_draft(conn,action_id,params_raw,now=None):
    decode(params_raw)
    conn.execute('UPDATE action_drafts SET revision=revision WHERE 0')
    row = conn.execute('SELECT a.status FROM actions a JOIN action_drafts d ON d.id=a.id WHERE a.id=?',(action_id,)).fetchone()
    if row is None or row['status']!='draft':
        raise ValueError('Merge draft is no longer editable')
    reference = collection_recovery.publish(conn,'action-draft',action_id,params_raw)
    revision = uuid4().hex
    conn.execute('UPDATE action_drafts SET revision=?,updated_at=?,recovery_filename=?,recovery_digest=?,recovery_bytes=? '
                 'WHERE id=?',(revision,now,*reference,action_id))
    conn.execute('UPDATE actions SET revision=? WHERE id=?',(revision,action_id))


def claim_target(conn,target_id,now,*,lease_seconds=60,reconcile=False):
    """A lease fences completion; expiration never makes a remote write replayable."""
    conn.execute('UPDATE action_target_claims SET lease_until=lease_until WHERE 0')
    target = conn.execute('SELECT * FROM action_targets WHERE id=?',(target_id,)).fetchone()
    if target is None or target['account_id'] is None or target['playlist_scope_id'] is None:
        raise ValueError('Reconnect and resolve this historical action target before recovery')
    prior = conn.execute('SELECT * FROM action_target_claims WHERE account_id=? AND playlist_scope_id=?',
                         (target['account_id'],target['playlist_scope_id'])).fetchone()
    if prior and prior['lease_until']>now:
        raise ValueError('Another action worker owns this playlist target')
    uncertain = conn.execute("SELECT s.action_id FROM action_steps s JOIN action_targets t ON t.id=s.target_id "
        "JOIN actions a ON a.id=s.action_id WHERE t.account_id=? AND t.playlist_scope_id=? "
        "AND (s.outcome='unknown' OR (s.operation='delete_playlist' AND s.phase='pending' "
        "AND s.outcome='not_sent' AND a.idempotency_key='legacy-action:'||a.id)) "
        "AND a.status NOT IN ('undone','cancelled') LIMIT 1",
        (target['account_id'],target['playlist_scope_id'])).fetchone()
    if uncertain and (not reconcile or uncertain['action_id']!=target['action_id']):
        raise ValueError('The prior remote outcome is unknown; reconcile it before another write')
    token = uuid4().hex
    conn.execute('INSERT INTO action_target_claims VALUES (?,?,?,?,?) ON CONFLICT(account_id,playlist_scope_id) '
                 'DO UPDATE SET action_target_id=excluded.action_target_id,claim_token=excluded.claim_token,lease_until=excluded.lease_until',
                 (target['account_id'],target['playlist_scope_id'],target_id,token,now+lease_seconds))
    conn.execute('UPDATE action_steps SET claim_token=? WHERE target_id=?',(token,target_id))
    return token


def require_claim(conn,target_id,token):
    if not conn.execute('SELECT 1 FROM action_target_claims WHERE action_target_id=? AND claim_token=?',
                        (target_id,token)).fetchone():
        raise ValueError('Action target completion belongs to a stale worker')


def release_claim(conn,target_id,token):
    require_claim(conn,target_id,token)
    conn.execute('DELETE FROM action_target_claims WHERE action_target_id=? AND claim_token=?',(target_id,token))
