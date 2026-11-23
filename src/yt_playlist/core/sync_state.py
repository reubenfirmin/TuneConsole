"""Library sync progress owned by accounts, playlist scopes and settings."""
import json

from . import collection_recovery, playlist_scopes, worker_settings
from .collection_owners import SYNC_ATTEMPT as ATTEMPT

STATUSES = frozenset({'complete','partial','failed','skipped'})
RESOURCES = frozenset({'playlist_listing','playlists','ratings','history','saved_songs','saved_albums','saved_album_mirror','album_tracks'})
IMPORT_STAMPS = ('takeout_imported_at', 'spotify_imported_at', 'spotify_import_report', 'spotify_library_imported_at')
HISTORY_FIELDS = ('last_plays_sync_at', 'verified_imports_repaired', *IMPORT_STAMPS)
STAMPS = {'last_sync_at':'last_success_at','last_sync_attempt_at':'started_at','last_sync_outcome':'outcome',
          'last_plays_sync_at':'last_plays_sync_at', **dict(zip(IMPORT_STAMPS, IMPORT_STAMPS))}
GLOBAL = 'library_sync_resources'
COLUMN = "sync_progress TEXT CHECK(sync_progress IS NULL OR json_type(sync_progress) IS 'object')"
REFERENCE = ('recovery_filename', 'recovery_digest', 'recovery_bytes')


def stamp(conn,key,default=None):
    column = STAMPS[key]
    if column in HISTORY_FIELDS:
        return worker_settings.read(conn, column, default)
    value = (head(conn) or {}).get(column)
    return default if value is None else str(value)


def set_stamp(conn,key,value):
    column = STAMPS[key]
    if column in IMPORT_STAMPS:
        save_history_progress(conn, column, value)
        return
    value = value if column == 'outcome' or value is None else float(value)
    if column == 'last_plays_sync_at':
        save_history_progress(conn, column, value)
        return
    attempt = head(conn) or _empty()
    save_head(conn, {**attempt, column: value, 'revision': attempt['revision'] + 1})


def save_history_progress(conn, column, value):
    if column not in HISTORY_FIELDS:
        raise ValueError('Unknown listening history progress column')
    ensure(conn)
    worker_settings.save(conn, column, str(value) if value is not None else None)


@collection_recovery.pinned_read
def read(conn):
    attempt = head(conn)
    if attempt is None or attempt['recovery_filename'] is None:
        return {}
    result = collection_recovery.read(attempt['recovery_filename'],attempt['recovery_digest'],attempt['recovery_bytes'],
                                      kind='library-sync',owner=1)
    return restore_progress(result, attempt, cursors(conn))


def head(conn):
    value = worker_settings.read(conn, ATTEMPT)
    return json.loads(value) if value is not None else None


def save_head(conn, document):
    worker_settings.save(conn, ATTEMPT, json.dumps(document))


def _empty(now=None):
    return dict(revision=1, phase='running', outcome=None, started_at=now, finished_at=None, last_success_at=None,
                recovery_filename=None, recovery_digest=None, recovery_bytes=None)


def cursors(conn):
    for row in json.loads(worker_settings.read(conn, GLOBAL, '{}')).values():
        yield dict(row, provider_playlist_id=None)
    for raw, in conn.execute('SELECT sync_progress FROM accounts WHERE sync_progress IS NOT NULL ORDER BY id'):
        for row in json.loads(raw).values():
            yield dict(row, provider_playlist_id=None)
    for remote, raw in conn.execute('SELECT provider_playlist_id,sync_progress FROM playlist_scopes '
                                    'WHERE sync_progress IS NOT NULL ORDER BY id'):
        for row in json.loads(raw).values():
            yield dict(row, provider_playlist_id=remote)


def restore_progress(result, attempt, rows):
    """Overlay durable cursors without reviving retired report members."""
    for row in rows:
        account = (result.get('accounts',{}).get(str(row['account_id'])) if row['account_id'] is not None else result)
        if account is None:
            # Retired/omitted accounts retain their typed cursors, but do not
            # become configured accounts in a later attempt's report.
            continue
        if row['collection']=='account':
            item = account
        elif row['collection']=='playlist':
            # The published attempt defines membership. Durable scopes and
            # cursors also retain deleted playlists for curation/reappearance.
            item = account.get('resources',{}).get('playlists',{}).get('items',{}).get(row['provider_playlist_id'])
            if item is None:
                continue
        else:
            item = account.setdefault('resources',{}).setdefault(row['collection'],{})
        for column,target in (('phase','status'),('updated_at','last_attempt_at'),('last_success_at','last_success_at'),
                              ('failures','failures'),('retry_at','retry_at')):
            if row[column] is None:
                item.pop(target,None)
            else:
                item[target] = row[column]
        if row['cursor'] is not None:
            item['cursor'] = row['cursor']
    result['last_attempt_at'] = attempt['finished_at'] or 0
    # A running attempt keeps the last published result, including freshness.
    if attempt['phase']!='running':
        result['status'] = attempt['phase']
    return result


def state(store):
    with store.read_view() as reader:
        return read(reader.conn)


def begin(conn,now):
    attempt = head(conn)
    if attempt is None:
        attempt = _empty(now)
    else:
        attempt.update(revision=attempt['revision']+1, phase='running', started_at=now)
    save_head(conn, attempt)
    return attempt['revision']


def ensure(conn, now=None):
    if head(conn) is None:
        save_head(conn, _empty(now))


def _resources(result):
    for account_id,account in result.get('accounts',{}).items():
        if not isinstance(account_id,str) or not account_id.isdecimal() or not isinstance(account,dict):
            raise ValueError('Sync progress requires recorded account IDs')
        yield int(account_id),'account',None,account
        for name,item in account.get('resources',{}).items():
            if name not in RESOURCES:
                raise ValueError('Unsupported sync resource; original retained')
            yield int(account_id),name,None,item
            for provider_id,child in item.get('items',{}).items():
                if name!='playlists':
                    raise ValueError('Unsupported sync child progress; original retained')
                yield int(account_id),'playlist',provider_id,child
    for name,item in result.get('resources',{}).items():
        if name not in {'saved_album_mirror','album_tracks'}:
            raise ValueError('Unsupported global sync progress; original retained')
        yield None,name,None,item


def write(conn,result,now,*,last_success=None):
    _write_cursors(conn, result)
    reference = collection_recovery.publish(conn,'library-sync',1,result)
    _write_attempt(conn, result, now, reference, last_success)


def write_prepared(conn,result,now,*,reference,last_success=None):
    """Persist supplied progress and an already published recovery reference."""
    _write_cursors(conn, result)
    _write_attempt(conn, result, now, reference, last_success)


def write_checkpoint(conn, result, reference):
    """Keep the attempt's lease and success clock while committing resumable work."""
    _write_cursors(conn, result)
    attempt = head(conn)
    if attempt is None:
        raise ValueError('A sync checkpoint requires its running attempt')
    save_head(conn, {**attempt, **dict(zip(REFERENCE, reference))})


def cursor_owner(account, scope, collection):
    if scope is not None:
        return ('playlist_scopes', scope), str(account)
    if account is not None:
        return ('accounts', account), collection
    return (None, None), collection


def load_resources(conn, owner):
    table, identity = owner
    if table is None:
        return json.loads(worker_settings.read(conn, GLOBAL, '{}'))
    row = conn.execute(f'SELECT sync_progress FROM {table} WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise ValueError('Sync progress requires its account or playlist scope')
    return json.loads(row[0]) if row[0] is not None else {}


def save_resources(conn, owner, rows):
    table, identity = owner
    value = json.dumps(rows)
    if table is None:
        worker_settings.save(conn, GLOBAL, value)
    else:
        conn.execute(f'UPDATE {table} SET sync_progress=? WHERE id=?', (value, identity))


def _write_cursors(conn, result):
    if not isinstance(result,dict) or result.get('status') not in STATUSES:
        raise ValueError('Unsupported sync result; original retained')
    updated = {}
    for account_id,collection,provider_id,item in _resources(result):
        if not isinstance(item,dict) or item.get('status') not in STATUSES or (item.get('cursor') is not None and not isinstance(item['cursor'],str)):
            raise ValueError('Unsupported sync cursor; original retained')
        scope = playlist_scopes.ensure(conn,provider_id,account_id) if provider_id is not None else None
        owner, key = cursor_owner(account_id, scope, collection)
        if owner not in updated:
            updated[owner] = load_resources(conn, owner)
        row = updated[owner].get(key, {})
        updated[owner][key] = {**row, 'account_id': account_id, 'collection': collection, 'playlist_scope_id': scope,
            'provider': 'youtube', 'revision': row.get('revision', 0) + 1, 'cursor': item.get('cursor'), 'phase': item['status'],
            'updated_at': item.get('last_attempt_at'), 'last_success_at': item.get('last_success_at'),
            'failures': item.get('failures'), 'retry_at': item.get('retry_at')}
    for owner, rows in updated.items():
        save_resources(conn, owner, rows)


def _write_attempt(conn, result, now, reference, last_success):
    attempt = head(conn) or dict(_empty(now), revision=0)
    attempt.update(revision=attempt['revision']+1, phase=result['status'], outcome=result['status'],
                   finished_at=result.get('last_attempt_at', now), **dict(zip(REFERENCE, reference)))
    if last_success is not None:
        attempt['last_success_at'] = last_success
    save_head(conn, attempt)
