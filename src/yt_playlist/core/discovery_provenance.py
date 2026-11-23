"""Typed discovery inputs and retained exposure facts. Callers own transactions."""
import json
import math

from . import artist_keys, catalog_keys, collection_recovery, discovery_candidates, genre_identity
from .artist_reconciliation import candidate, move_image_retry, move_scan_time


SUBJECTS = {'track':'recording_id','artist':'artist_id','album':'album_id'}
OFFER_OWNERS = {'track':'recordings','artist':'artists','album':'albums'}
CANDIDATE_FILTERS = {'track': 'discovery_entry_id IS NOT NULL', 'artist': 'discovery_candidate=1',
                     'album': 'discovery_found_at IS NOT NULL'}
EARLIER_OFFERS = (('earlier_offer_count', 'offered_count', 'INTEGER NOT NULL DEFAULT 0'),
                  ('earlier_first_shown', 'first_shown', 'REAL'),
                  ('earlier_last_shown', 'last_shown', 'REAL'))


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_origins'").fetchone() is not None


def origin_fields(conn, token, *, account_id=None, import_key=None):
    """Resolve only demonstrated references; an unknown input cannot admit a subject."""
    if not isinstance(token,str) or not token:
        raise ValueError('Discovery needs its source input')
    fields = dict(kind='legacy_unknown',account_id=account_id,album_id=None,seed_video_id=None,
                  genre_key=None,artist_id=None,import_key=None,legacy_source_key=token,
                  resolution_status='unresolved',resolution_reason='unrecognized source input')
    kind,separator,value = token.partition(':')
    if token in ('discovery','spotify_model'):
        fields.update(kind=token,legacy_source_key=None,resolution_status='resolved',resolution_reason=None)
    elif kind=='spotify_export' and separator:
        try:
            captured_account = int(value)
        except ValueError:
            captured_account = None
        if account_id is not None and captured_account!=account_id:
            raise ValueError('Discovery import account disagrees with its input')
        if captured_account is not None and conn.execute('SELECT 1 FROM accounts WHERE id=?',(captured_account,)).fetchone():
            fields.update(kind=kind,account_id=captured_account,
                import_key=import_key,legacy_source_key=None,
                resolution_status='resolved' if import_key is not None else 'legacy_account',resolution_reason=None)
    elif separator and kind in ('radio','genre','artist','artist_start'):
        target = None
        if kind=='radio':
            row = conn.execute('SELECT video_id FROM track_sources WHERE video_id=?',(value,)).fetchone()
            target = row[0] if row else None
            column = 'seed_video_id'
        elif kind=='genre':
            target,_ = genre_identity.resolve(conn,value)
            column = 'genre_key'
        else:
            ids = {artist_keys.canonical_id(conn,r[0]) for r in conn.execute(
                "SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?",(value,))}
            target = next(iter(ids)) if len(ids)==1 else None
            column = 'artist_id'
        if target is not None:
            fields.update(kind=kind,legacy_source_key=None,resolution_status='resolved',resolution_reason=None)
            fields[column] = target
        else:
            fields['resolution_reason'] = 'missing or ambiguous '+kind+' owner'
    elif not separator:
        row = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?",(token,)).fetchone()
        if row:
            fields.update(kind='album',album_id=row[0],legacy_source_key=None,
                          resolution_status='resolved',resolution_reason=None)
        else:
            fields['resolution_reason'] = 'missing album owner'
    # Account attribution is meaningful only for an import or a retained unknown.
    if fields['kind'] not in ('spotify_export','spotify_model','legacy_unknown'):
        fields['account_id'] = None
    return fields


def origin(conn, token, *, account_id=None, import_key=None, create=True):
    fields = origin_fields(conn, token, account_id=account_id, import_key=import_key)
    return store_origin(conn, fields, create=create)


def store_origin(conn, fields, *, create=True):
    """Intern an explicit origin supplied by its caller."""
    identity = {k:v for k,v in fields.items() if k not in ('resolution_status','resolution_reason')}
    where = ' AND '.join(k+' IS ?' for k in identity)
    existing = conn.execute('SELECT id FROM discovery_origins WHERE '+where,tuple(identity.values())).fetchone()
    if existing or not create:
        return existing[0] if existing else None
    return conn.execute('INSERT INTO discovery_origins('+','.join(fields)+') VALUES ('+
        ','.join('?' for _ in fields)+')',tuple(fields.values())).lastrowid


def retain(conn, recording_id, origin_id, video_id, last_found_at, *, first_found_at=...):
    first = last_found_at if first_found_at is ... else first_found_at
    existing = conn.execute('SELECT id,first_found_at,last_found_at FROM discovery_sources '
        'WHERE recording_id=? AND origin_id=? AND input_video_id IS ?',
        (recording_id,origin_id,video_id)).fetchone()
    if existing:
        starts = [v for v in (existing['first_found_at'],first) if v is not None]
        ends = [v for v in (existing['last_found_at'],last_found_at) if v is not None]
        conn.execute('UPDATE discovery_sources SET first_found_at=?,last_found_at=? WHERE id=?',
            (min(starts) if starts else None,max(ends) if ends else None,existing['id']))
        return existing['id']
    return conn.execute('INSERT INTO discovery_sources(recording_id,origin_id,input_video_id,first_found_at,last_found_at) '
        'VALUES (?,?,?,?,?)',(recording_id,origin_id,video_id,first,last_found_at)).lastrowid


def retain_artist(conn, artist_id, origin_id, score):
    conn.execute('INSERT INTO discovery_artist_sources VALUES (?,?,?) '
        'ON CONFLICT(artist_id,origin_id) DO UPDATE SET score=max(score,excluded.score)',
        (artist_id,origin_id,score or 0))


def explain(conn, artist_id, because, fits):
    reference = collection_recovery.publish(conn,'discovery-candidate',artist_id,
        {'because':because or [],'fits':fits or []})
    write_explanation_reference(conn, artist_id, reference)


def write_explanation_reference(conn, artist_id, reference, *, folded=True):
    if folded:
        conn.execute('UPDATE artists SET discovery_recovery_filename=?,discovery_recovery_digest=?,'
                     'discovery_recovery_bytes=? WHERE id=? AND discovery_candidate=1', (*reference, artist_id))
    else:
        conn.execute('UPDATE discovered_artists SET recovery_filename=?,recovery_digest=?,recovery_bytes=? '
                     'WHERE artist_id=?', (*reference, artist_id))


def invalidate_explanation(conn, artist_id, *, folded=True):
    write_explanation_reference(conn, artist_id, (None, None, None), folded=folded)


def subject(conn, kind, value):
    if kind=='artist':
        return artist_keys.lookup(conn,value,exact=True)
    if kind=='album':
        row = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?",(value,)).fetchone()
        return row[0] if row else None
    if kind=='track':
        ids = {catalog_keys.resolve_recording(conn,r[0]) for r in conn.execute(
            'SELECT recording_id FROM recording_aliases WHERE alias=? UNION SELECT id FROM recordings WHERE identity_key=?',
            (value,value))}
        if len(ids)>1:
            return None
        key = catalog_keys.resolve_key(conn,value)
        row = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(key,)).fetchone() if key else None
        return row[0] if row else None
    raise ValueError('Unsupported discovery subject')


def offer_views(conn, kind, identifier):
    row = conn.execute(f'SELECT discovery_views FROM {OFFER_OWNERS[kind]} WHERE id=?', (identifier,)).fetchone()
    return json.loads(row[0]) if row else {}


def refresh_counts(conn, kind, identifier, views=None):
    if kind == 'album':
        return
    if views is None:
        views = offer_views(conn, kind, identifier)
    times = views.values()
    conn.execute(f'UPDATE {OFFER_OWNERS[kind]} SET discovery_offered_count=?,discovery_first_shown=?,'
                 f'discovery_last_shown=? WHERE id=? AND {CANDIDATE_FILTERS[kind]}',
                 (len(views), min(times, default=None), max(times, default=None), identifier))


def _save_views(conn, kind, identifier, views):
    conn.execute(f'UPDATE {OFFER_OWNERS[kind]} SET discovery_views=? WHERE id=?',
                 (json.dumps(views, separators=(',', ':')), identifier))
    refresh_counts(conn, kind, identifier, views)


def impression(conn, kind, identifier, view_id, now):
    row = conn.execute(f'SELECT discovery_views FROM {OFFER_OWNERS[kind]} '
                       f'WHERE id=? AND {CANDIDATE_FILTERS[kind]}', (identifier,)).fetchone()
    if row is None or view_id is None or not isinstance(now, (int, float)) or not math.isfinite(now):
        return False
    views = json.loads(row[0])
    view_id = str(view_id)
    if view_id in views:
        return False
    views[view_id] = now
    _save_views(conn, kind, identifier, views)
    return True


def merge_earlier_offers(conn, kind, previous, target):
    table = OFFER_OWNERS[kind]
    moved = conn.execute(f'''UPDATE {table} AS current SET
        earlier_offer_count=current.earlier_offer_count+old.earlier_offer_count,
        earlier_first_shown=min(coalesce(current.earlier_first_shown,old.earlier_first_shown),
                                coalesce(old.earlier_first_shown,current.earlier_first_shown)),
        earlier_last_shown=max(coalesce(current.earlier_last_shown,old.earlier_last_shown),
                               coalesce(old.earlier_last_shown,current.earlier_last_shown))
        FROM {table} old WHERE current.id=? AND old.id=? AND current.id<>old.id
          AND (old.earlier_offer_count<>0 OR old.earlier_first_shown IS NOT NULL OR old.earlier_last_shown IS NOT NULL)
    ''', (target, previous)).rowcount
    if moved:
        conn.execute(f'UPDATE {table} SET earlier_offer_count=0,earlier_first_shown=NULL,earlier_last_shown=NULL '
                     'WHERE id=?', (previous,))


def merge_subject(conn, kind, previous, target):
    """Fold visible impressions and legacy offer counts into the surviving subject."""
    if previous == target:
        return
    old = offer_views(conn, kind, previous)
    if old:
        views = offer_views(conn, kind, target)
        for view, shown in old.items():
            views[view] = min(shown, views.get(view, shown))
        _save_views(conn, kind, target, views)
        _save_views(conn, kind, previous, {})
    else:
        refresh_counts(conn, kind, target)
    merge_earlier_offers(conn, kind, previous, target)


def merge_fetches(conn, previous, target):
    conn.execute('''UPDATE discovery_origins SET fetch_sources=(SELECT json_group_array(json(value)) FROM (
        SELECT value FROM json_each(fetch_sources) UNION
        SELECT value FROM json_each((SELECT fetch_sources FROM discovery_origins WHERE id=?))))
        WHERE id=? AND EXISTS(SELECT 1 FROM discovery_origins WHERE id=? AND fetch_sources<>'[]')''',
        (previous, target, previous))


def merge_origins(conn, column, previous, target, *, reason, folded=True):
    if column not in ('artist_id','album_id','genre_key'):
        raise ValueError('Unsupported discovery origin identity')
    for old in conn.execute('SELECT * FROM discovery_origins WHERE '+column+'=?',(previous,)).fetchall():
        current = conn.execute('SELECT id FROM discovery_origins WHERE kind=? AND '+column+'=?',
            (old['kind'],target)).fetchone()
        if current is None:
            conn.execute('UPDATE discovery_origins SET '+column+'=? WHERE id=?',(target,old['id']))
            for aid, in conn.execute('SELECT artist_id FROM discovery_artist_sources WHERE origin_id=?',(old['id'],)):
                invalidate_explanation(conn,aid,folded=folded)
            continue
        oid = current[0]
        merge_fetches(conn, old['id'], oid)
        sources = conn.execute('SELECT * FROM discovery_sources WHERE origin_id=?',(old['id'],)).fetchall()
        for row in sources:
            retain(conn,row['recording_id'],oid,row['input_video_id'],row['last_found_at'],first_found_at=row['first_found_at'])
        for row in conn.execute('SELECT * FROM discovery_artist_sources WHERE origin_id=?',(old['id'],)).fetchall():
            retain_artist(conn,row['artist_id'],oid,row['score'])
            invalidate_explanation(conn,row['artist_id'],folded=folded)
        conn.execute('DELETE FROM discovery_sources WHERE origin_id=?',(old['id'],))
        conn.execute('DELETE FROM discovery_artist_sources WHERE origin_id=?',(old['id'],))
        conn.execute('DELETE FROM discovery_origins WHERE id=?',(old['id'],))


def reconcile_artist_sources(conn, previous, target, *, reason='canonical artist reconciliation', folded=True):
    for row in conn.execute('SELECT * FROM discovery_artist_sources WHERE artist_id IN (?,?)',
                            (previous,target)).fetchall():
        if row['artist_id']==previous:
            retain_artist(conn,target,row['origin_id'],row['score'])
    conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?',(previous,))
    merge_origins(conn,'artist_id',previous,target,reason=reason,folded=folded)


def reconcile_artist(conn, previous, target, *, source):
    folded = discovery_candidates.artists_folded(conn)
    reconcile_artist_sources(conn,previous,target,reason=source,folded=folded)
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_artist_image_state'").fetchone():
        conn.execute('INSERT INTO discovery_artist_image_state(artist_id,retry_at) SELECT ?,retry_at '
            'FROM discovery_artist_image_state WHERE artist_id=? ON CONFLICT(artist_id) DO UPDATE SET '
            'retry_at=max(retry_at,excluded.retry_at)',(target,previous))
        conn.execute('DELETE FROM discovery_artist_image_state WHERE artist_id=?',(previous,))
    elif conn.execute("SELECT 1 FROM pragma_table_info('artists') WHERE name='discovery_image_retry_at'").fetchone():
        move_image_retry(conn, previous, target)
    move_scan_time(conn, previous, target)
    rows = [dict(row) for row in conn.execute('SELECT * FROM discovered_artists WHERE artist_id IN (?,?)',
                                             (previous, target))]
    winner = candidate(rows, target)
    if winner is not None:
        if folded:
            discovery_candidates.merge_artist(conn, previous, target, winner)
        else:
            conn.execute('DELETE FROM discovered_artists WHERE artist_id IN (?,?)', (previous, target))
            conn.execute('INSERT INTO discovered_artists('+','.join(winner)+') VALUES ('+
                         ','.join('?' for _ in winner)+')', tuple(winner.values()))
    merge_subject(conn, 'artist', previous, target)
    invalidate_explanation(conn,target,folded=folded)


def remove_spotify_artists(conn, *, account_id=None, import_key=None, folded=True):
    where = "o.kind IN ('spotify_export','spotify_model')"
    parameters = []
    if account_id is not None:
        where += ' AND o.account_id=?'
        parameters.append(account_id)
    if import_key is not None:
        where += ' AND o.import_key=?'
        parameters.append(import_key)
    affected = {r[0] for r in conn.execute('SELECT s.artist_id FROM discovery_artist_sources s '
        'JOIN discovery_origins o ON o.id=s.origin_id WHERE '+where,parameters)}
    derived = {r[0] for r in conn.execute('SELECT s.artist_id FROM discovery_artist_sources s '
        "JOIN discovery_origins o ON o.id=s.origin_id WHERE o.kind='spotify_model'")}
    conn.execute('DELETE FROM discovery_artist_sources WHERE origin_id IN (SELECT o.id FROM discovery_origins o WHERE '+where+')',parameters)
    removed = 0
    for aid in affected:
        remaining = conn.execute('SELECT max(score) FROM discovery_artist_sources WHERE artist_id=?',(aid,)).fetchone()[0]
        if remaining is None:
            if folded:
                removed += discovery_candidates.clear_artist(conn, aid)
            else:
                removed += conn.execute('DELETE FROM discovered_artists WHERE artist_id=?',(aid,)).rowcount
        else:
            if folded:
                conn.execute('UPDATE artists SET discovery_score=? WHERE id=? AND discovery_candidate=1', (remaining, aid))
            else:
                conn.execute('UPDATE discovered_artists SET score=? WHERE artist_id=?',(remaining,aid))
            if aid in derived:
                invalidate_explanation(conn,aid,folded=folded)
    return removed


def finish_reassociation(conn, video_id, previous, target, member, now, *, merge_recordings, association_id,
                         merge_offers=merge_subject, refresh_offers=refresh_counts):
    folded = discovery_candidates.tracks_folded(conn)
    sources = conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? '+
        ('' if merge_recordings else 'AND input_video_id=?'),
        (previous,) if merge_recordings else (previous,video_id)).fetchall()
    if member and (merge_recordings or sources):
        found = member['found_at'] if merge_recordings else min(
            (row['first_found_at'] for row in sources if row['first_found_at'] is not None), default=now)
        entry = conn.execute('SELECT id FROM track_sources WHERE recording_id=? AND video_id=?',(target,video_id)).fetchone()[0]
        if folded:
            discovery_candidates.admit_track(conn, target, entry, found, merge=True)
        else:
            conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) '
                'ON CONFLICT(recording_id) DO UPDATE SET found_at=min(coalesce(found_at,excluded.found_at),coalesce(excluded.found_at,found_at))',
                (target,entry,found))
    for row in sources:
        retain(conn,target,row['origin_id'],row['input_video_id'],row['last_found_at'],first_found_at=row['first_found_at'])
        conn.execute('DELETE FROM discovery_sources WHERE id=?',(row['id'],))
    if merge_recordings:
        merge_offers(conn, 'track', previous, target)
    else:
        refresh_offers(conn, 'track', target)
