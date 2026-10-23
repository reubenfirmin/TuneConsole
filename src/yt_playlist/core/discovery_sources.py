"""Discovery provenance and lifecycle evidence. Callers own transactions and the Store lock."""
import json


def retain_artist(conn, artist_id, source, score, identity_id=None):
    # Historical-schema fixtures also exercise the current identity helpers.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_artist_sources'").fetchone():
        return
    if not conn.execute('SELECT 1 FROM discovered_artists WHERE artist_id=?', (artist_id,)).fetchone():
        conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?', (artist_id,))
    conn.execute('INSERT INTO discovery_artist_sources VALUES (?,?,?,?) '
                 'ON CONFLICT(artist_id,source) DO UPDATE SET score=MAX(score,excluded.score)',
                 (artist_id,source,identity_id,score or 0))


def reconcile_artist_sources(conn, previous, target):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_artist_sources'").fetchone():
        return
    if not conn.execute('SELECT 1 FROM discovered_artists WHERE artist_id=?', (target,)).fetchone():
        conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?', (target,))
    for row in conn.execute('SELECT s.* FROM discovery_artist_sources s JOIN discovered_artists d '
                            'ON d.artist_id=s.artist_id WHERE s.artist_id=?', (previous,)).fetchall():
        conn.execute('INSERT INTO discovery_artist_sources VALUES (?,?,?,?) '
                     'ON CONFLICT(artist_id,source) DO UPDATE SET score=MAX(score,excluded.score)',
                     (target,row['source'],row['identity_id'],row['score']))
    conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?', (previous,))


def reconcile_artist(conn, previous, target, *, source):
    """Fold scan/candidate lifecycles, retaining pre-merge evidence and distinct visible offers."""
    reconcile_artist_sources(conn,previous,target)
    for table,clock in (('artist_scans','scanned_at'),('discovered_artists','found_at')):
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE artist_id IN (?,?)',
                                             (previous,target))]
        if not rows:
            continue
        for row in rows:
            conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)',
                ('artist_reconcile:'+table,json.dumps([previous,target,row['artist']]),json.dumps(row),source))
        winner = max(rows,key=lambda r:(r[clock] if r[clock] is not None else float('-inf'),
                                       r['artist_id']==target,json.dumps(r,sort_keys=True)))
        if table=='discovered_artists':
            # Impression evidence outlives membership. Only overlapping surviving lifecycles
            # double-count a view; retained impressions from a prior GC cycle do not.
            lifecycles = {r['artist_id']:r['first_shown'] for r in rows}
            duplicates = conn.execute("SELECT COUNT(*) FROM discovery_visible_impressions a "
                "JOIN discovery_visible_impressions b ON a.view_id=b.view_id AND a.kind=b.kind "
                "WHERE a.kind='artist' AND a.item_id=? AND b.item_id=? "
                "AND a.shown_at>=? AND b.shown_at>=?",
                (str(previous),str(target),lifecycles.get(previous),lifecycles.get(target))).fetchone()[0]
            for field in ('found_at','first_shown','legacy_first_shown'):
                winner[field] = min((r[field] for r in rows if r[field] is not None),default=None)
            for field in ('last_shown','legacy_last_shown'):
                winner[field] = max((r[field] for r in rows if r[field] is not None),default=None)
            winner['offered_count'] = max(0,sum(r['offered_count'] or 0 for r in rows)-duplicates)
            winner['legacy_offered_count'] = sum(r['legacy_offered_count'] or 0 for r in rows)
        winner.update(artist_id=target,artist=f'@artist:{target}')
        conn.execute(f'DELETE FROM {table} WHERE artist_id IN (?,?)',(previous,target))
        columns = ','.join(winner)
        conn.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in winner)})',
                     tuple(winner.values()))

    impressions = conn.execute("SELECT view_id,shown_at FROM discovery_visible_impressions "
                               "WHERE kind='artist' AND item_id=?",(str(previous),)).fetchall()
    for row in impressions:
        conn.execute("INSERT INTO discovery_visible_impressions VALUES (?,'artist',?,?) "
            'ON CONFLICT(view_id,kind,item_id) DO UPDATE SET shown_at=MIN(shown_at,excluded.shown_at)',
            (row['view_id'],str(target),row['shown_at']))
    conn.execute("DELETE FROM discovery_visible_impressions WHERE kind='artist' AND item_id=?",(str(previous),))


def remove_spotify_artists(conn):
    affected = [r[0] for r in conn.execute(
        "SELECT DISTINCT artist_id FROM discovery_artist_sources WHERE source LIKE 'spotify_export:%' OR source='spotify_model'")]
    derived = {r[0] for r in conn.execute("SELECT artist_id FROM discovery_artist_sources WHERE source='spotify_model'")}
    conn.execute("DELETE FROM discovery_artist_sources WHERE source LIKE 'spotify_export:%' OR source='spotify_model'")
    removed = 0
    for aid in affected:
        if not conn.execute('SELECT 1 FROM discovery_artist_sources WHERE artist_id=?', (aid,)).fetchone():
            removed += conn.execute('DELETE FROM discovered_artists WHERE artist_id=?', (aid,)).rowcount
        elif aid in derived:
            # Discard explanations/ranking computed with the removed history;
            # independent membership survives and the clean model refreshes it.
            conn.execute("UPDATE discovered_artists SET because='[]',fits='[]',score=("
                         'SELECT MAX(score) FROM discovery_artist_sources WHERE artist_id=?) WHERE artist_id=?', (aid,aid))
    return removed


def source_identity(token):
    for kind in ('radio', 'genre'):
        if token.startswith(kind + ':'):
            return kind, token[len(kind) + 1:]
    return 'album', token


def retain(conn, recording_id, token, video_id, now, *, first_found_at=None):
    kind, identifier = source_identity(token)
    conn.execute(
        "INSERT INTO discovery_sources(recording_id,source_token,input_video_id,found_at,"
        "source_type,source_id,last_found_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(recording_id,source_token) DO UPDATE SET "
        "found_at=MIN(COALESCE(found_at,excluded.found_at),COALESCE(excluded.found_at,found_at)), "
        "last_found_at=MAX(COALESCE(last_found_at,excluded.last_found_at),"
        "COALESCE(excluded.last_found_at,last_found_at))",
        (recording_id, token, video_id, now if first_found_at is None else first_found_at,
         kind, identifier, now))


def merge_membership(conn, recording_id, member):
    conn.execute(
        'INSERT INTO discovery_memberships VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(recording_id) DO UPDATE SET '
        'found_at=MIN(COALESCE(found_at,excluded.found_at),COALESCE(excluded.found_at,found_at)), '
        'offered_count=offered_count+excluded.offered_count, '
        'first_shown=MIN(COALESCE(first_shown,excluded.first_shown),COALESCE(excluded.first_shown,first_shown)), '
        'last_shown=MAX(COALESCE(last_shown,excluded.last_shown),COALESCE(excluded.last_shown,last_shown))',
        (recording_id,member['entry_id'],member['found_at'],member['first_shown'],
         member['last_shown'],member['offered_count']))


def prepare_reassociation(conn, video_id, previous):
    """Release a representative before moving its entry; preserve recording-level counters."""
    member = conn.execute('SELECT * FROM discovery_memberships WHERE recording_id=?', (previous,)).fetchone()
    remaining = conn.execute('SELECT MIN(id) FROM catalog_entries WHERE recording_id=? AND video_id IS NOT ?',
                             (previous,video_id)).fetchone()[0]
    if member:
        if remaining is None:
            conn.execute('DELETE FROM discovery_memberships WHERE recording_id=?', (previous,))
        elif conn.execute('SELECT video_id FROM catalog_entries WHERE id=?', (member['entry_id'],)).fetchone()[0] == video_id:
            conn.execute('UPDATE discovery_memberships SET entry_id=? WHERE recording_id=?', (remaining,previous))
    return member, remaining


def finish_reassociation(conn, video_id, previous, target, member, remaining, now, *, association_id):
    """Exact source evidence follows its upload; a split never copies aggregate offer counts."""
    sources = conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? '
                           + ('' if remaining is None else 'AND input_video_id=?'),
                           (previous,) if remaining is None else (previous,video_id)).fetchall()
    if member and remaining is None:
        merge_membership(conn,target,member)
    elif member and sources:
        entry = conn.execute('SELECT id FROM catalog_entries WHERE video_id=?',(video_id,)).fetchone()[0]
        found = [row['found_at'] for row in sources if row['found_at'] is not None]
        conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) '
                     'ON CONFLICT(recording_id) DO NOTHING', (target,entry,min(found) if found else now))
    for row in sources:
        # Collision aggregation retains the original inputs, including which upload
        # supplied each source. This evidence must survive subsequent pool expiry.
        for original in [row,conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? AND source_token=?',
                                         (target,row['source_token'])).fetchone()]:
            if original is not None:
                conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)',
                             ('discovery_reassociation',json.dumps([association_id,original['recording_id'],original['source_token']]),
                              json.dumps(dict(original)),'exact upload reassociation; retained pre-merge evidence'))
        retain(conn,target,row['source_token'],row['input_video_id'],row['last_found_at'],
               first_found_at=row['found_at'])
        conn.execute('DELETE FROM discovery_sources WHERE recording_id=? AND source_token=?',
                     (previous,row['source_token']))
