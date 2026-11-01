"""Account-owned import reports with revision admission for worker completion."""
import json
import uuid

from . import collection_recovery

KEY = 'spotify_library_import_job'
KEYS = frozenset({KEY})
FIELDS = ('phase','started_at','finished_at','identity_label','report_schema','dismissed','error',
          'playlists_found','track_entries','albums_found','lookup_count','estimate_label')
OPAQUE = frozenset({'report','preview_token'})
TOKEN = '_job_revision'


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='spotify_import_jobs'").fetchone() is not None


@collection_recovery.pinned_read
def load(conn,*,token=False):
    row = conn.execute('SELECT j.* FROM spotify_import_job_head h JOIN spotify_import_jobs j ON j.id=h.job_id WHERE h.id=1').fetchone()
    if row is None:
        return None
    value = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                     kind='spotify-import',owner=row['id'])
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
    if (not isinstance(value,dict) or 'status' not in value
            or set(value)-{'status','account_id',TOKEN,*FIELDS,*OPAQUE}):
        raise ValueError('Unsupported Spotify import job')
    conn.execute('UPDATE spotify_import_job_head SET job_id=job_id WHERE 0')
    head = conn.execute('SELECT j.id,j.revision,j.account_id,j.unresolved_account_id,j.account_present '
                        'FROM spotify_import_job_head h JOIN spotify_import_jobs j ON j.id=h.job_id').fetchone()
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
    from . import entity_names
    known = conn.execute(f'SELECT id FROM {entity_names.accounts(conn)} WHERE id=?',(account,)).fetchone() if account is not None else None
    if not known and not legacy and expected is None:
        raise ValueError('A new Spotify import job requires its account')
    if expected is None:
        # Keep finished reports until explicit replacement or deletion. Only
        # the head changes; a stale worker can never regain the current slot.
        identity = conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM spotify_import_jobs').fetchone()[0]
    else:
        identity = head['id']
    reference = collection_recovery.publish(conn,'spotify-import',identity,
                                            {key:value[key] for key in OPAQUE if key in value})
    revision = uuid.uuid4().hex
    fields = [value.get(field) for field in FIELDS]
    mask = sum(1<<index for index,field in enumerate(FIELDS) if field in value)
    columns = ','.join(FIELDS)
    conn.execute(f'INSERT INTO spotify_import_jobs(id,revision,account_id,unresolved_account_id,account_present,field_mask,status,{columns},recovery_filename,recovery_digest,recovery_bytes) '
                 f'VALUES ({",".join("?" for _ in range(22))}) ON CONFLICT(id) DO UPDATE SET '
                 'revision=excluded.revision,field_mask=excluded.field_mask,status=excluded.status,'+
                 ','.join(f'{field}=excluded.{field}' for field in (*FIELDS,'recovery_filename','recovery_digest','recovery_bytes')),
                 (identity,revision,known[0] if known else None,None if known else account,int('account_id' in value),mask,
                  value['status'],*fields,*reference))
    conn.execute('INSERT INTO spotify_import_job_head VALUES (1,?) ON CONFLICT(id) DO UPDATE SET job_id=excluded.job_id',(identity,))
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
        conn.execute('DELETE FROM spotify_import_job_head')
        conn.execute('DELETE FROM spotify_import_jobs')
