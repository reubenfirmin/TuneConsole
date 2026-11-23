"""Interpret Spotify workflow receipt values and their SQL facts."""
import json
from uuid import uuid4

from . import collection_recovery, playlist_scopes, spotify_records

PLAYLIST = 'spotify_library_imported_playlists'
ALBUM = 'spotify_library_imported_albums'
TOKEN = '_receipt_revision'


def owns(key):
    return any(key==prefix or key.startswith(prefix+':') for prefix in (PLAYLIST,ALBUM))


def scope(conn,account,remote,*,legacy=False):
    known = conn.execute('SELECT id FROM accounts WHERE id=?',(account,)).fetchone() if account is not None else None
    if not known and not legacy:
        raise ValueError('New workflow receipts require their retained account')
    return (known[0],playlist_scopes.ensure(conn,remote,known[0]) if remote else None) if known else (None,None)


def write_facts(conn, kind, account, target):
    prior = spotify_records.read(conn, spotify_records.transfer_key(kind, account, target))
    known = conn.execute('SELECT id FROM accounts WHERE id=?', (account,)).fetchone() if account is not None else None
    return {'prior': dict(prior) if prior else None, 'known': known[0] if known else None,
            'next_id': spotify_records.next_identity(conn, spotify_records.TRANSFER)}


def receipt_save(conn,kind,account,fingerprint,value,*,legacy=False,expected_revision=None):
    conn.execute('UPDATE settings SET value=value WHERE 0')
    prepared = prepare_receipt(kind, account, fingerprint, value, write_facts(conn, kind, account, fingerprint),
                               legacy=legacy, expected_revision=expected_revision)
    reference = collection_recovery.publish(conn,'spotify-transfer',prepared['identity'],prepared['value'])
    return register_receipt(conn, prepared, reference)


def _check_revision(prior, expected_revision):
    if (expected_revision=='' and prior is not None) or (expected_revision not in (None,'') and (prior is None or prior['revision']!=expected_revision)):
        raise ValueError('Spotify transfer completion belongs to a stale revision')


def _check_target(prior, state):
    if prior and ((prior['marker'] is not None and prior['marker']!=state.get('marker')) or
                  (prior['provider_playlist_id_snapshot'] is not None and prior['provider_playlist_id_snapshot']!=state.get('ytm_playlist_id'))):
        raise ValueError('A Spotify receipt cannot change its creation marker or destination')


def prepare_receipt(kind, account, fingerprint, value, facts, *, legacy=False, expected_revision=None):
    prior = facts['prior']
    _check_revision(prior, expected_revision)
    if facts['known'] is None and not legacy:
        raise ValueError('New workflow receipts require their retained account')
    if not isinstance(value,(dict,type(None))):
        raise ValueError('Unsupported Spotify receipt format')
    state = dict(value or {})
    state.pop(TOKEN,None)
    _check_target(prior, state)
    positions = state.get('source_video_ids',[])
    if not isinstance(positions,list) or any(v is not None and (not isinstance(v,str) or not v) for v in positions):
        raise ValueError('Unsupported Spotify ordered source slots')
    return {'kind': kind, 'account': account, 'fingerprint': fingerprint, 'value': None if value is None else state,
            'legacy': legacy, 'expected_revision': expected_revision, 'revision': uuid4().hex,
            'identity': prior['id'] if prior else facts['next_id'], 'prior_id': prior['id'] if prior else None}


def register_receipt(conn, prepared, reference):
    kind, account, fingerprint = prepared['kind'], prepared['account'], prepared['fingerprint']
    state, identity, revision = prepared['value'] or {}, prepared['identity'], prepared['revision']
    key = spotify_records.transfer_key(kind, account, fingerprint)
    prior = spotify_records.read(conn, key)
    _check_revision(prior, prepared['expected_revision'])
    _check_target(prior, state)
    if (prior['id'] if prior else None) != prepared['prior_id']:
        raise ValueError('Spotify receipt changed before it was saved')
    if prior is None and identity < spotify_records.next_identity(conn, spotify_records.TRANSFER):
        raise ValueError('Spotify receipt identity was already used')
    known,target_scope = scope(conn,account,state.get('ytm_playlist_id'),legacy=prepared['legacy'])
    phase = state.get('creation_phase','legacy')
    row = dict(id=identity, source_kind=kind, captured_account_id=account, account_id=known, fingerprint=fingerprint,
               revision=revision, creation_phase=phase, marker=state.get('marker'), playlist_scope_id=target_scope,
               provider_playlist_id_snapshot=state.get('ytm_playlist_id'), complete=state.get('complete'),
               order_verified=state.get('order_verified'), updated_at=state.get('updated_at'),
               last_run_added=state.get('last_run_added'), slots=state.get('source_video_ids', []))
    spotify_records.save(conn, key, {**row, **dict(zip(spotify_records.REFERENCE, reference))})
    spotify_records.note_identity(conn, spotify_records.TRANSFER, identity)
    return revision


@collection_recovery.pinned_read
def receipts(conn,kind,account,*,token=False):
    result = {}
    for row in documents(conn, kind, account):
        value = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='spotify-transfer',owner=row['id'])
        result[row['fingerprint']] = restore_receipt(row, value, row['slots'], token=token)
    return result


def documents(conn, kind, account):
    prefix = spotify_records.transfer_prefix(kind, account)
    return sorted((json.loads(raw) for _, raw in spotify_records.matching(conn, prefix)), key=lambda row: row['id'])


def owners(conn):
    keys = conn.execute('SELECT key FROM settings WHERE key GLOB ?',
                        (spotify_records.TRANSFER + '*:*:*',))
    owners = dict.fromkeys(tuple(key[len(spotify_records.TRANSFER):].split(':', 2)[:2]) for key, in keys)
    for kind, account in owners:
        yield kind, None if account == 'None' else int(account)


def delete(conn, kind, account):
    spotify_records.clear(conn, spotify_records.transfer_prefix(kind, account))


def restore_receipt(row, value, slots, *, token=False):
    if isinstance(value,dict):
        for column,field in (('creation_phase','creation_phase'),('marker','marker'),('provider_playlist_id_snapshot','provider_playlist_id'),
                              ('complete','complete'),('order_verified','order_verified'),('updated_at','updated_at'),('last_run_added','last_run_added')):
            if field in value:
                value[field] = bool(row[column]) if field in ('complete','order_verified') and row[column] is not None else row[column]
        if 'source_video_ids' in value:
            value['source_video_ids'] = slots
        if token:
            value[TOKEN] = row['revision']
    return value


def receipt_key(key):
    prefix = next(p for p in (PLAYLIST,ALBUM) if key==p or key.startswith(p+':'))
    account = None if key==prefix else int(key[len(prefix)+1:])
    return ('playlist' if prefix==PLAYLIST else 'album'),account


def put(conn,values,*,legacy=False):
    for key,raw in values.items():
        value = json.loads(raw) if raw is not None and raw!='' else None
        kind,account = receipt_key(key)
        if value is None:
            delete(conn, kind, account)
        elif isinstance(value,list) and all(isinstance(item,str) for item in value):
            for fingerprint in value:
                receipt_save(conn,kind,account,fingerprint,None,legacy=legacy or account is None)
        elif isinstance(value,dict) and kind=='playlist':
            for fingerprint,item in value.items():
                receipt_save(conn,kind,account,fingerprint,item,legacy=legacy)
        else:
            raise ValueError('Unsupported Spotify workflow receipt family')


def get(conn,key,default=None):
    kind,account = receipt_key(key)
    value = receipts(conn,kind,account)
    if not value:
        return default
    return json.dumps(sorted(value) if kind=='album' or account is None else value)


def settings_with_prefix(conn,prefix):
    values = {}
    for kind,account in owners(conn):
        key = (PLAYLIST if kind=='playlist' else ALBUM)+('' if account is None else ':'+str(account))
        if key.startswith(prefix):
            values[key] = get(conn,key)
    return values
