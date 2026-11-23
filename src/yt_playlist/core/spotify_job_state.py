"""Account-owned import reports with revision admission for worker completion."""
import json
import uuid

from . import collection_recovery, spotify_records, worker_settings

KEY = 'spotify_library_import_job'
KEYS = frozenset({KEY})
FIELDS = ('phase','started_at','finished_at','identity_label','report_schema','dismissed','error',
          'playlists_found','track_entries','albums_found','lookup_count','estimate_label')
OPAQUE = frozenset({'report','preview_token'})
TOKEN = '_job_revision'
CURRENT = 'spotify_import_current'


def current(conn):
    identity = worker_settings.read(conn, CURRENT)
    return spotify_records.read(conn, spotify_records.IMPORT + identity) if identity else None


@collection_recovery.pinned_read
def load(conn,*,token=False):
    row = current(conn)
    if row is None:
        return None
    value = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                     kind='spotify-import',owner=row['id'])
    return restore(row, value, token=token)


def restore(row, value, *, token=False):
    result = {'status':row['status'],**value}
    result.update({field:bool(row[field]) if field=='dismissed' and row[field] is not None else row[field]
                   for index,field in enumerate(FIELDS) if row['field_mask'] & (1<<index)})
    if row['account_present']:
        result['account_id'] = row['account_id'] if row['account_id'] is not None else row['unresolved_account_id']
    if token:
        result[TOKEN] = row['revision']
    return result


def save(conn,value,*,legacy=False):
    """An absent token starts a job; a token admits only its current revision."""
    from . import entity_names
    value = entity_names.account_document(value)
    conn.execute('UPDATE settings SET value=value WHERE 0')
    prepared = prepare(value, capture(conn, value.get('account_id') if isinstance(value, dict) else None), legacy=legacy)
    reference = collection_recovery.publish(conn,'spotify-import',prepared['identity'],
                                            {key:value[key] for key in OPAQUE if key in value})
    return register(conn, prepared, reference)


def capture(conn, account):
    head = current(conn)
    known = conn.execute('SELECT id FROM accounts WHERE id=?', (account,)).fetchone() if account is not None else None
    return {'head': dict(head) if head else None, 'known': known[0] if known else None,
            'next_id': spotify_records.next_identity(conn, spotify_records.IMPORT)}


def prepare(value, facts, *, legacy=False):
    if (not isinstance(value,dict) or 'status' not in value
            or set(value)-{'status','account_id',TOKEN,*FIELDS,*OPAQUE}):
        raise ValueError('Unsupported Spotify import job')
    head = facts['head']
    expected = value.get(TOKEN)
    if expected is not None and (head is None or head['revision']!=expected):
        raise ValueError('Spotify import job was replaced or updated')
    account = value.get('account_id')
    if account is not None and type(account) is not int:
        raise ValueError('Spotify import job account requires an integer identity')
    if expected is not None:
        prior_account = head['account_id'] if head['account_id'] is not None else head['unresolved_account_id']
        if account != prior_account or ('account_id' in value) != bool(head['account_present']):
            raise ValueError('Spotify import job account cannot change')
    known = facts['known']
    if not known and not legacy and expected is None:
        raise ValueError('A new Spotify import job requires its account')
    if expected is None:
        # Keep finished reports until explicit replacement or deletion. Only
        # the head changes; a stale worker can never regain the current slot.
        identity = facts['next_id']
    else:
        identity = head['id']
    return {'identity': identity, 'known': known, 'value': value, 'revision': uuid.uuid4().hex}


def register(conn, prepared, reference):
    value, identity, known = prepared['value'], prepared['identity'], prepared['known']
    expected = value.get(TOKEN)
    revision = prepared['revision']
    mask = sum(1 << index for index, field in enumerate(FIELDS) if field in value)
    columns = ('revision', 'field_mask', 'status', *FIELDS, 'recovery_filename', 'recovery_digest', 'recovery_bytes')
    data = (revision, mask, value['status'], *(value.get(field) for field in FIELDS), *reference)
    key = spotify_records.IMPORT + str(identity)
    if expected is not None:
        row = current(conn)
        if row is None or row['id'] != identity or row['revision'] != expected:
            raise ValueError('Spotify import job was replaced or updated')
    else:
        if identity < spotify_records.next_identity(conn, spotify_records.IMPORT):
            raise ValueError('Spotify import job identity was already used')
        row = dict(id=identity, account_id=known, unresolved_account_id=None if known else value.get('account_id'),
                   account_present=int('account_id' in value))
        spotify_records.note_identity(conn, spotify_records.IMPORT, identity)
        worker_settings.save(conn, CURRENT, str(identity))
    spotify_records.save(conn, key, {**row, **dict(zip(columns, data))})
    return revision


def get(conn,key,default=None):
    from . import entity_names
    value = load(conn)
    return json.dumps(entity_names.external_account_document(value)) if value is not None else default


def put(conn,values,*,legacy=False):
    raw = values.get(KEY)
    if raw:
        save(conn,json.loads(raw),legacy=legacy)
    else:
        clear(conn)


def clear(conn):
    worker_settings.save(conn, CURRENT, None)
    conn.execute('DELETE FROM settings WHERE key GLOB ?',
                 (spotify_records.IMPORT + '[0-9]*',))
