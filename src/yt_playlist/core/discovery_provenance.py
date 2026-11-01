"""Typed discovery inputs and retained exposure facts. Callers own transactions."""
import json

from . import artist_keys, catalog_keys, collection_recovery, genre_identity, legacy_evidence


SUBJECTS = {'track':'recording_id','artist':'artist_id','album':'album_id'}
CANDIDATES = {'track':('discovery_memberships','recording_id'),
              'artist':('discovered_artists','artist_id'),
              'album':('album_discovery_memberships','album_id')}


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
        from . import entity_names
        if captured_account is not None and conn.execute(f'SELECT 1 FROM {entity_names.accounts(conn)} WHERE id=?',(captured_account,)).fetchone():
            fields.update(kind=kind,account_id=captured_account,
                import_key=import_key,legacy_source_key=None,
                resolution_status='resolved' if import_key is not None else 'legacy_account',resolution_reason=None)
    elif separator and kind in ('radio','genre','artist','artist_start'):
        target = None
        if kind=='radio':
            row = conn.execute('SELECT video_id FROM youtube_items WHERE video_id=?',(value,)).fetchone()
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
    if not available(conn):
        conn.execute('UPDATE main.discovered_artists SET because=?,fits=? WHERE artist_id=?',
            (json.dumps(because or [], allow_nan=False), json.dumps(fits or [], allow_nan=False), artist_id))
        return
    reference = collection_recovery.publish(conn,'discovery-candidate',artist_id,
        {'because':because or [],'fits':fits or []})
    conn.execute('UPDATE discovered_artists SET recovery_filename=?,recovery_digest=?,recovery_bytes=? WHERE artist_id=?',
        (*reference,artist_id))


def explanation(conn, row):
    if 'preparation_artist_id' in row.keys():
        identity = row['preparation_artist_id'] if row['preparation_artist_id'] is not None else row['artist']
        return collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
            kind='discovery-candidate', owner=identity)
    if not available(conn):
        return {field:json.loads(row[field] or '[]') for field in ('because','fits')}
    if row['recovery_filename'] is None:
        return {'because':[],'fits':[]}
    return collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
        kind='discovery-candidate',owner=row['artist_id'])


def invalidate_explanation(conn, artist_id):
    if not available(conn):
        conn.execute("UPDATE main.discovered_artists SET because='[]',fits='[]' WHERE artist_id=?", (artist_id,))
        return
    conn.execute('UPDATE discovered_artists SET recovery_filename=NULL,recovery_digest=NULL,recovery_bytes=NULL '
        'WHERE artist_id=?',(artist_id,))


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


def refresh_counts(conn, kind, identifier):
    if not available(conn):
        # Legacy counters include offers for which no distinct visit was captured.
        # Existing writers increment them together with their visible receipt;
        # reconstructing them from receipts would discard that older evidence.
        return
    table,column = CANDIDATES[kind]
    count,first,last = conn.execute('SELECT count(*),min(shown_at),max(shown_at) '
        'FROM discovery_impressions WHERE '+column+'=?',(identifier,)).fetchone()
    conn.execute('UPDATE '+table+' SET offered_count=?,first_shown=?,last_shown=? WHERE '+column+'=?',
        (count,first,last,identifier))
    if kind!='track':
        legacy = conn.execute('SELECT offered_count,first_shown,last_shown FROM discovery_legacy_offers WHERE '+
            column+'=?',(identifier,)).fetchone()
        conn.execute('UPDATE '+table+' SET legacy_offered_count=?,legacy_first_shown=?,legacy_last_shown=? WHERE '+column+'=?',
            (*(tuple(legacy) if legacy else (0,None,None)),identifier))


def impression(conn, kind, identifier, view_id, now, *, require_candidate=True):
    table,column = CANDIDATES[kind]
    if require_candidate and not conn.execute('SELECT 1 FROM '+table+' WHERE '+column+'=?',(identifier,)).fetchone():
        return False
    if not available(conn):
        if kind == 'artist':
            key = str(identifier)
        elif kind == 'track':
            key = conn.execute('SELECT identity_key FROM recordings WHERE id=?', (identifier,)).fetchone()[0]
        else:
            row = conn.execute("SELECT provider_id FROM album_primary_provider_ids WHERE provider='youtube' AND album_id=?", (identifier,)).fetchone()
            if row is None:
                return False
            key = row[0]
        inserted = conn.execute('INSERT OR IGNORE INTO discovery_visible_impressions VALUES (?,?,?,?)',
            (view_id, kind, key, now)).rowcount
        if inserted:
            conn.execute('UPDATE '+table+' SET offered_count=offered_count+1,'
                'first_shown=CASE WHEN first_shown IS NULL THEN ? ELSE min(first_shown,?) END,'
                'last_shown=CASE WHEN last_shown IS NULL THEN ? ELSE max(last_shown,?) END WHERE '+column+'=?',
                (now, now, now, now, identifier))
        return bool(inserted)
    inserted = conn.execute('INSERT OR IGNORE INTO discovery_impressions(view_id,'+column+',shown_at) VALUES (?,?,?)',
        (view_id,identifier,now)).rowcount
    refresh_counts(conn,kind,identifier)
    return bool(inserted)


def retain_legacy(conn, kind, identifier, count, first, last):
    column = SUBJECTS[kind]
    existing = conn.execute('SELECT id,offered_count,first_shown,last_shown FROM discovery_legacy_offers WHERE '+column+'=?',
        (identifier,)).fetchone()
    if existing:
        starts = [v for v in (existing['first_shown'],first) if v is not None]
        ends = [v for v in (existing['last_shown'],last) if v is not None]
        conn.execute('UPDATE discovery_legacy_offers SET offered_count=offered_count+?,first_shown=?,last_shown=? WHERE id=?',
            (count,min(starts) if starts else None,max(ends) if ends else None,existing['id']))
    else:
        conn.execute('INSERT INTO discovery_legacy_offers('+column+',offered_count,first_shown,last_shown) VALUES (?,?,?,?)',
            (identifier,count,first,last))


def merge_subject(conn, kind, previous, target, *, reason):
    """Fold interpretations; keep every captured predecessor under its evidence owner."""
    column = SUBJECTS[kind]
    facts = [dict(r) for r in conn.execute('SELECT * FROM discovery_impressions WHERE '+column+' IN (?,?)',
        (previous,target))]
    legacy = [dict(r) for r in conn.execute('SELECT * FROM discovery_legacy_offers WHERE '+column+' IN (?,?)',
        (previous,target))]
    for start in range(0,len(facts),512):
        legacy_evidence.retain_many(conn,'discovery',[(
            'discovery_impression_merge',json.dumps([kind,previous,target,r['id']]),json.dumps(r),reason)
            for r in facts[start:start+512]])
    if legacy:
        legacy_evidence.retain(conn,'discovery_legacy_offer_merge',json.dumps([kind,previous,target]),
            json.dumps(legacy),reason)
    for row in facts:
        if row[column]==previous:
            conn.execute('INSERT INTO discovery_impressions(view_id,'+column+',shown_at) VALUES (?,?,?) '
                'ON CONFLICT(view_id,'+column+') WHERE '+column+' IS NOT NULL '
                'DO UPDATE SET shown_at=min(shown_at,excluded.shown_at)',(row['view_id'],target,row['shown_at']))
    conn.execute('DELETE FROM discovery_impressions WHERE '+column+'=?',(previous,))
    for row in legacy:
        if row[column]==previous:
            retain_legacy(conn,kind,target,row['offered_count'],row['first_shown'],row['last_shown'])
    conn.execute('DELETE FROM discovery_legacy_offers WHERE '+column+'=?',(previous,))
    refresh_counts(conn,kind,target)


def merge_origins(conn, column, previous, target, *, reason):
    if column not in ('artist_id','album_id','genre_key'):
        raise ValueError('Unsupported discovery origin identity')
    for old in conn.execute('SELECT * FROM discovery_origins WHERE '+column+'=?',(previous,)).fetchall():
        legacy_evidence.retain(conn,'discovery_origin_merge',json.dumps([old['id'],target]),json.dumps(dict(old)),reason)
        current = conn.execute('SELECT id FROM discovery_origins WHERE kind=? AND '+column+'=?',
            (old['kind'],target)).fetchone()
        if current is None:
            conn.execute('UPDATE discovery_origins SET '+column+'=? WHERE id=?',(target,old['id']))
            for aid, in conn.execute('SELECT artist_id FROM discovery_artist_sources WHERE origin_id=?',(old['id'],)):
                invalidate_explanation(conn,aid)
            continue
        oid = current[0]
        sources = conn.execute('SELECT * FROM discovery_sources WHERE origin_id=?',(old['id'],)).fetchall()
        for row in sources:
            legacy_evidence.retain(conn,'discovery_source_origin_merge',json.dumps([row['id'],oid]),json.dumps(dict(row)),reason)
            retain(conn,row['recording_id'],oid,row['input_video_id'],row['last_found_at'],first_found_at=row['first_found_at'])
        for row in conn.execute('SELECT * FROM discovery_artist_sources WHERE origin_id=?',(old['id'],)).fetchall():
            legacy_evidence.retain(conn,'discovery_artist_origin_merge',json.dumps([row['artist_id'],old['id'],oid]),
                json.dumps(dict(row)),reason)
            retain_artist(conn,row['artist_id'],oid,row['score'])
            invalidate_explanation(conn,row['artist_id'])
        conn.execute('DELETE FROM discovery_sources WHERE origin_id=?',(old['id'],))
        conn.execute('DELETE FROM discovery_artist_sources WHERE origin_id=?',(old['id'],))
        conn.execute('DELETE FROM discovery_origins WHERE id=?',(old['id'],))


def reconcile_artist_sources(conn, previous, target, *, reason='canonical artist reconciliation'):
    for row in conn.execute('SELECT * FROM discovery_artist_sources WHERE artist_id IN (?,?)',
                            (previous,target)).fetchall():
        legacy_evidence.retain(conn,'discovery_artist_source_merge',json.dumps([previous,target,row['artist_id'],row['origin_id']]),
            json.dumps(dict(row)),reason)
        if row['artist_id']==previous:
            retain_artist(conn,target,row['origin_id'],row['score'])
    conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?',(previous,))
    merge_origins(conn,'artist_id',previous,target,reason=reason)


def reconcile_artist(conn, previous, target, *, source):
    reconcile_artist_sources(conn,previous,target,reason=source)
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_artist_image_state'").fetchone():
        conn.execute('INSERT INTO discovery_artist_image_state(artist_id,retry_at) SELECT ?,retry_at '
            'FROM discovery_artist_image_state WHERE artist_id=? ON CONFLICT(artist_id) DO UPDATE SET '
            'retry_at=max(retry_at,excluded.retry_at)',(target,previous))
        conn.execute('DELETE FROM discovery_artist_image_state WHERE artist_id=?',(previous,))
    for table,clock in (('artist_scans','scanned_at'),('discovered_artists','found_at')):
        rows = [dict(r) for r in conn.execute('SELECT * FROM '+table+' WHERE artist_id IN (?,?)',(previous,target))]
        if not rows:
            continue
        for row in rows:
            captured = dict(row)
            if table=='discovered_artists':
                captured['explanation'] = explanation(conn,row)
            legacy_evidence.retain(conn,'artist_reconcile:'+table,json.dumps([previous,target,row['artist_id']]),
                json.dumps(captured),source)
        winner = max(rows,key=lambda r:(r[clock] if r[clock] is not None else float('-inf'),r['artist_id']==target))
        if table=='discovered_artists':
            starts = [r['found_at'] for r in rows if r['found_at'] is not None]
            winner['found_at'] = min(starts) if starts else None
            winner.update(recovery_filename=None,recovery_digest=None,recovery_bytes=None)
        winner['artist_id'] = target
        conn.execute('DELETE FROM '+table+' WHERE artist_id IN (?,?)',(previous,target))
        conn.execute('INSERT INTO '+table+'('+','.join(winner)+') VALUES ('+','.join('?' for _ in winner)+')',tuple(winner.values()))
    merge_subject(conn,'artist',previous,target,reason=source)
    invalidate_explanation(conn,target)


def remove_spotify_artists(conn, *, account_id=None, import_key=None):
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
    conn.execute('DELETE FROM discovery_artist_sources WHERE origin_id IN (SELECT o.id FROM discovery_origins o WHERE '+where+')',parameters)
    removed = 0
    for aid in affected:
        remaining = conn.execute('SELECT max(score) FROM discovery_artist_sources WHERE artist_id=?',(aid,)).fetchone()[0]
        if remaining is None:
            removed += conn.execute('DELETE FROM discovered_artists WHERE artist_id=?',(aid,)).rowcount
        else:
            conn.execute('UPDATE discovered_artists SET score=? WHERE artist_id=?',(remaining,aid))
            invalidate_explanation(conn,aid)
    return removed


def finish_reassociation(conn, video_id, previous, target, member, remaining, now, *, association_id):
    sources = conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? '+
        ('' if remaining is None else 'AND input_video_id=?'),
        (previous,) if remaining is None else (previous,video_id)).fetchall()
    if member and (remaining is None or sources):
        entry = conn.execute('SELECT id FROM catalog_entries WHERE recording_id=? AND video_id=?',(target,video_id)).fetchone()[0]
        conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) '
            'ON CONFLICT(recording_id) DO UPDATE SET found_at=min(coalesce(found_at,excluded.found_at),coalesce(excluded.found_at,found_at))',
            (target,entry,member['found_at']))
    for row in sources:
        collision = conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? AND origin_id=? AND input_video_id IS ?',
            (target,row['origin_id'],row['input_video_id'])).fetchone()
        for captured in (row,collision):
            if captured is not None:
                legacy_evidence.retain(conn,'discovery_reassociation',json.dumps([association_id,captured['id']]),
                    json.dumps(dict(captured)),'exact upload reassociation; retained source contribution')
        retain(conn,target,row['origin_id'],row['input_video_id'],row['last_found_at'],first_found_at=row['first_found_at'])
        conn.execute('DELETE FROM discovery_sources WHERE id=?',(row['id'],))
    if remaining is None:
        merge_subject(conn,'track',previous,target,reason='recording reassociation '+str(association_id))
    refresh_counts(conn,'track',target)
