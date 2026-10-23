"""Artist identities and ordered source credits. Names are evidence, never provider identities."""
import json
import time

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.discovery_sources import reconcile_artist_sources
from yt_playlist.util.matching import normalize


def token(artist_id):
    return f'@artist:{artist_id}'


def canonical_id(conn, artist_id):
    # Historical-schema fixtures can use these helpers before the additive upgrade.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='artist_redirects'").fetchone():
        row = conn.execute('SELECT artist_id FROM artist_redirects WHERE retired_id=?',(artist_id,)).fetchone()
        if row:
            return row[0]
    return artist_id


def lookup(conn, value, *, exact=False):
    if isinstance(value,int) or str(value).startswith('@artist:'):
        try:
            artist_id = int(str(value).removeprefix('@artist:'))
        except ValueError:
            return None
        return canonical_id(conn,artist_id) if conn.execute('SELECT 1 FROM artists WHERE id=?', (artist_id,)).fetchone() else None
    rows = conn.execute('SELECT DISTINCT artist_id FROM artist_aliases WHERE alias=?', (value,)).fetchall()
    if not rows and not exact:
        rows = [(r['artist_id'],) for r in conn.execute('SELECT artist_id,alias FROM artist_aliases')
                if normalize(r['alias']) == normalize(value)]
    ids = {canonical_id(conn,r[0]) for r in rows}
    return next(iter(ids)) if len(ids) == 1 else None


def ambiguous_context(conn, artist_id):
    """A recorded split of unresolved credits cannot later become a global identity guess."""
    return bool(conn.execute('SELECT 1 FROM identity_state_archive WHERE kind=? AND source_id=?',
                             ('ambiguous_album_artist',str(artist_id))).fetchone())


def reconcile_provisional(conn, previous, target, *, source, now=None):
    """Contextual identity evidence; the caller owns the entire transaction.

    Keep retired rows and raw observations. Only provisional identities can move;
    verified homonyms are never reconciled by credit text or numeric ID order.
    """
    previous,target = canonical_id(conn,previous),canonical_id(conn,target)
    row = conn.execute('SELECT provisional FROM artists WHERE id=?',(previous,)).fetchone()
    if previous==target or not row or not row[0]:
        return target
    if conn.execute('SELECT 1 FROM artist_provider_ids WHERE artist_id=?',(previous,)).fetchone():
        return target
    if ambiguous_context(conn,previous):
        return target
    if not conn.execute('SELECT 1 FROM artists a JOIN artist_provider_ids p ON p.artist_id=a.id '
                        'WHERE a.id=? AND a.provisional=0',(target,)).fetchone():
        return previous
    conn.execute('INSERT INTO artist_redirects VALUES (?,?,?,?)',
                 (previous,target,source,time.time() if now is None else now))
    conn.execute('UPDATE artist_redirects SET artist_id=? WHERE artist_id=?',(target,previous))
    for table in ('track_artists','album_artists','rec_feedback'):
        conn.execute(f'UPDATE {table} SET artist_id=? WHERE artist_id=?',(target,previous))
    conn.execute('INSERT OR IGNORE INTO artist_aliases SELECT ?,alias,source FROM artist_aliases WHERE artist_id=?',
                 (target,previous))
    reconcile_artist_sources(conn,previous,target)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='spotify_history_listens'").fetchone():
        conn.execute('UPDATE spotify_history_listens SET artist_id=? WHERE artist_id=?', (target,previous))

    for table,clock in (('artist_scans','scanned_at'),('rec_artist_similar','fetched_at'),
                        ('discovered_artists','found_at')):
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE artist_id IN (?,?)',
                                             (previous,target))]
        if not rows:
            continue
        for r in rows:
            conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)',
                ('artist_reconcile:'+table,json.dumps([previous,target,r['artist']]),json.dumps(r),source))
        # Deterministic precedence for derived payloads; lifecycle evidence is folded below.
        winner = max(rows,key=lambda r:(r[clock] if r[clock] is not None else float('-inf'),
                                       r['artist_id']==target,json.dumps(r,sort_keys=True)))
        if table=='discovered_artists':
            # Impression evidence outlives pool membership. Deduplicate only views
            # counted in both surviving candidate lifecycles, not a prior GC cycle.
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
        winner.update(artist_id=target,artist=token(target))
        conn.execute(f'DELETE FROM {table} WHERE artist_id IN (?,?)',(previous,target))
        columns = ','.join(winner)
        conn.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in winner)})',
                     tuple(winner.values()))

    impressions = conn.execute("SELECT view_id,shown_at FROM discovery_visible_impressions "
                               "WHERE kind='artist' AND item_id=?",(str(previous),)).fetchall()
    for r in impressions:
        conn.execute("INSERT INTO discovery_visible_impressions VALUES (?,'artist',?,?) "
            'ON CONFLICT(view_id,kind,item_id) DO UPDATE SET shown_at=MIN(shown_at,excluded.shown_at)',
            (r['view_id'],str(target),r['shown_at']))
    conn.execute("DELETE FROM discovery_visible_impressions WHERE kind='artist' AND item_id=?",(str(previous),))
    edges = conn.execute('SELECT * FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?',
                         (previous,previous)).fetchall()
    conn.execute('DELETE FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?',
                 (previous,previous))
    for edge in edges:
        conn.execute('INSERT INTO artist_similarity_targets VALUES (?,?,?) '
            'ON CONFLICT(source_artist_id,target_artist_id) DO UPDATE SET weight=MAX(weight,excluded.weight)',
            (target if edge['source_artist_id']==previous else edge['source_artist_id'],
             target if edge['target_artist_id']==previous else edge['target_artist_id'],edge['weight']))
    for table in ('rec_artist_vectors','rec_artist_content_vectors','rec_proposals'):
        conn.execute(f'DELETE FROM {table}')
    conn.execute("DELETE FROM settings WHERE key='rec_artist_content_model'")
    return target


def ensure(conn, name, *, provider=None, provider_id=None, previous=None):
    previous = canonical_id(conn,previous) if previous is not None else None
    if previous is not None and provider and provider_id and ambiguous_context(conn,previous):
        previous = None
    if previous is not None and not (provider and provider_id):
        # A weaker refresh cannot replace the identity of an existing exact credit.
        row = (previous,)
    elif provider and provider_id:
        row = conn.execute('SELECT artist_id FROM artist_provider_ids WHERE provider=? AND provider_id=?',
                           (provider,provider_id)).fetchone()
    else:
        rows = conn.execute('SELECT id FROM artists WHERE provisional=1 AND name=? ORDER BY id', (name,)).fetchall()
        row = next((r for r in rows if canonical_id(conn,r[0])==r[0]),None)
    if row:
        artist_id = canonical_id(conn,row[0])
        if previous is not None and provider and provider_id:
            artist_id = reconcile_provisional(conn,previous,artist_id,source=provider)
    else:
        candidate = conn.execute('SELECT id FROM artists WHERE id=? AND provisional=1 '
            'AND NOT EXISTS (SELECT 1 FROM artist_provider_ids p WHERE p.artist_id=artists.id)',
            (previous,)).fetchone() if provider and provider_id else None
        if candidate:
            artist_id = candidate[0]
            conn.execute('UPDATE artists SET provisional=0 WHERE id=?',(artist_id,))
        else:
            artist_id = conn.execute('INSERT INTO artists(name,provisional) VALUES (?,?)',
                                     (name,int(not (provider and provider_id)))).lastrowid
        if provider and provider_id:
            conn.execute('INSERT INTO artist_provider_ids VALUES (?,?,?)', (provider,provider_id,artist_id))
    if name:
        conn.execute('INSERT OR IGNORE INTO artist_aliases VALUES (?,?,?)',
                     (artist_id,name,provider or 'unidentified_credit'))
    return artist_id


def observe(conn, entry_id, credit, credits=None, *, browse_id=None, source='youtube', now=None):
    existing = conn.execute('SELECT * FROM track_artists WHERE entry_id=?', (entry_id,)).fetchall()
    structured = [a for a in (credits or []) if isinstance(a,dict) and a.get('name')]
    if not structured and existing and (not browse_id or len(existing)!=1
                                       or credit_subject(conn,existing,credit) is None):
        return
    if not structured:
        if not credit:
            return
        structured = [{'name':credit,'id':browse_id}]
    conn.execute('INSERT INTO artist_credit_observations(entry_id,credit,credits_json,source,observed_at) '
                 'VALUES (?,?,?,?,?)', (entry_id,credit or '',json.dumps(structured),source,
                                        time.time() if now is None else now))
    conn.execute('DELETE FROM track_artists WHERE entry_id=?', (entry_id,))
    for position,a in enumerate(structured):
        previous = credit_subject(conn,existing,a['name'])
        artist_id = ensure(conn,a['name'],provider=source,provider_id=a.get('id') or a.get('browseId'),
                           previous=previous)
        conn.execute('INSERT INTO track_artists VALUES (?,?,?,?,?)',
                     (entry_id,position,artist_id,a['name'],source))


def state_id(conn, value):
    artist_id = lookup(conn,value,exact=True)
    if artist_id is not None:
        return artist_id
    if isinstance(value,int) or str(value).startswith('@artist:'):
        raise ValueError(f'Unknown artist reference: {value}')
    return ensure(conn,str(value))


def credit_subject(conn, existing, name):
    """Only a retained exact alias on this existing credit can supply context."""
    candidates = {canonical_id(conn,r['artist_id']) for r in existing
                  if r['credit']==name or conn.execute(
                      'SELECT 1 FROM artist_aliases WHERE artist_id=? AND alias=?',
                      (r['artist_id'],name)).fetchone()}
    return next(iter(candidates)) if len(candidates)==1 else None


def public_key(conn, artist_id):
    artist_id = canonical_id(conn,artist_id)
    row = conn.execute('SELECT name FROM artists WHERE id=?', (artist_id,)).fetchone()
    if row is None:
        return token(artist_id)
    normalized = normalize(row[0])
    ids = {canonical_id(conn,r['artist_id']) for r in conn.execute('SELECT artist_id,alias FROM artist_aliases')
           if normalize(r['alias']) == normalized}
    return normalized if len(ids) == 1 else token(artist_id)


def public_keys(conn, artist_ids):
    """Resolve a batch with one alias scan, preserving homonyms and retired handles.

    Calling public_key for every library artist repeatedly normalizes the entire alias catalog.
    The batch shares that work while retaining the single-artist ambiguity rules.
    """
    requested = set(artist_ids)
    if not requested:
        return {}
    redirects = (dict(conn.execute('SELECT retired_id,artist_id FROM artist_redirects'))
                 if conn.execute("SELECT 1 FROM sqlite_master WHERE name='artist_redirects'").fetchone()
                 else {})
    canonical = {aid: redirects.get(aid, aid) for aid in requested}
    wanted = set(canonical.values())
    names = {row['id']: normalize(row['name']) for row in conn.execute('SELECT id,name FROM artists')
             if row['id'] in wanted}
    aliases = {name: set() for name in names.values()}
    for i, row in enumerate(conn.execute('SELECT artist_id,alias FROM artist_aliases')):
        if i % 64 == 0:
            check_cancellable_work()
        name = normalize(row['alias'])
        if name in aliases:
            aliases[name].add(redirects.get(row['artist_id'], row['artist_id']))
    return {aid: (names[target] if target in names and len(aliases[names[target]]) == 1
                  else token(target)) for aid, target in canonical.items()}
