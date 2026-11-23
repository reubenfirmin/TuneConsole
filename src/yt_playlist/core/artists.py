"""Artist identities and ordered source credits. Names are evidence, never provider identities."""
import sqlite3
import time

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core import artist_credits
from yt_playlist.core.artist_keys import canonical_id, lookup
from yt_playlist.core.discovery_sources import reconcile_artist as reconcile_discovery_artist
from yt_playlist.core.artist_reconciliation import move_lastfm_cache
from yt_playlist.core.recommendation_invalidation import artist_reconciled
from yt_playlist.util.matching import normalize


def token(artist_id):
    return f'@artist:{artist_id}'


def ambiguous_context(conn, artist_id):
    """A recorded split of unresolved credits cannot later become a global identity guess."""
    return conn.execute('SELECT 1 FROM artists WHERE id=? AND contextual_credit=1', (artist_id,)).fetchone() is not None


def mark_ambiguity(conn, artist_id):
    conn.execute('UPDATE artists SET contextual_credit=1 WHERE id=? AND contextual_credit=0', (artist_id,))


def redirect(conn, previous, target, source, now):
    changed = conn.execute('UPDATE artists SET redirect_artist_id=?,redirect_source=?,redirect_created_at=? '
                           'WHERE id=? AND redirect_artist_id IS NULL', (target, source, now, previous)).rowcount
    if changed != 1:
        raise sqlite3.IntegrityError('Artist redirect source changed during reconciliation')
    conn.execute('UPDATE artists SET redirect_artist_id=? WHERE redirect_artist_id=?', (target, previous))


def _reconcile_feature_state(conn, previous, target, source):
    """Compatibility workflow for offline catalog preparation."""
    conn.execute('UPDATE rec_feedback SET artist_id=? WHERE artist_id=?', (target, previous))
    move_lastfm_cache(conn, previous, target)
    edges = conn.execute('SELECT * FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?',
                         (previous, previous)).fetchall()
    conn.execute('DELETE FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?',
                 (previous, previous))
    conn.executemany('INSERT INTO artist_similarity_targets VALUES (?,?,?) '
        'ON CONFLICT(source_artist_id,target_artist_id) DO UPDATE SET weight=MAX(weight,excluded.weight)',
        [(target if edge['source_artist_id'] == previous else edge['source_artist_id'],
          target if edge['target_artist_id'] == previous else edge['target_artist_id'], edge['weight']) for edge in edges])
    artist_reconciled(conn)


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
    redirect(conn, previous, target, source, time.time() if now is None else now)
    artist_credits.reconcile_current(conn, previous, target, source, time.time() if now is None else now)
    conn.execute('UPDATE album_artists SET artist_id=? WHERE artist_id=?',(target,previous))
    conn.execute('INSERT OR IGNORE INTO artist_aliases SELECT ?,alias,source FROM artist_aliases WHERE artist_id=?',
                 (target,previous))
    reconcile_discovery_artist(conn,previous,target,source=source)
    _reconcile_feature_state(conn, previous, target, source)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='spotify_history_listens'").fetchone():
        conn.execute('UPDATE main.spotify_history_listens SET artist_id=? WHERE artist_id=?', (target,previous))

    return target


def ensure(conn, name, *, provider=None, provider_id=None, previous=None):
    previous = canonical_id(conn,previous) if previous is not None else None
    if previous is None and not (provider and provider_id):
        return _ensure_provisional(conn, name, provider or 'unidentified_credit')
    if previous is not None and provider and provider_id and ambiguous_context(conn,previous):
        previous = None
    if previous is not None and not (provider and provider_id):
        # A weaker refresh cannot replace the identity of an existing exact credit.
        row = (previous,)
    else:
        row = conn.execute('SELECT artist_id FROM artist_provider_ids WHERE provider=? AND provider_id=?',
                           (provider,provider_id)).fetchone()
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


def _ensure_provisional(conn, name, source):
    row = conn.execute('SELECT id FROM artists WHERE provisional=1 AND name=? AND redirect_artist_id IS NULL '
        'ORDER BY id LIMIT 1',
        (name,)).fetchone()
    artist_id = (row[0] if row else conn.execute('INSERT INTO artists(name,provisional) VALUES (?,1)',
                                               (name,)).lastrowid)
    if name:
        conn.execute('INSERT OR IGNORE INTO artist_aliases VALUES (?,?,?)', (artist_id, name, source))
    return artist_id


def observe(conn, entry_id, credit, credits=None, *, browse_id=None, source='youtube', now=None,
            delivery_key=None, metadata_delivery_id=None):
    existing = conn.execute('SELECT * FROM entry_artists WHERE entry_id=? ORDER BY position', (entry_id,)).fetchall()
    structured = [a for a in (credits or []) if isinstance(a,dict) and a.get('name')]
    if not structured and existing and (not browse_id or len(existing)!=1
                                       or credit_subject(conn,existing,credit) is None):
        return
    if not structured:
        if not credit:
            return
        structured = [{'name':credit,'id':browse_id}]
    # Validate the delivered structure before ensure() can change aliases or
    # reconcile a provisional identity. The caller owns their transaction.
    artist_credits.prepare(conn, credit or '', structured, source)
    artist_ids = []
    for a in structured:
        previous = credit_subject(conn,existing,a['name'])
        artist_id = ensure(conn,a['name'],provider=source,provider_id=a.get('id') or a.get('browseId'),
                           previous=previous)
        artist_ids.append(artist_id)
    value = artist_credits.prepare(conn, credit or '', structured, source, artist_ids=artist_ids)
    version = artist_credits.intern(conn, value)
    observation, select = artist_credits.capture(conn, entry_id, version, source,
        time.time() if now is None else now, delivery_key=delivery_key, metadata_delivery_id=metadata_delivery_id)
    if select:
        artist_credits.select(conn, entry_id, observation)


def state_id(conn, value):
    artist_id = lookup(conn,value,exact=True)
    if artist_id is not None:
        return artist_id
    if isinstance(value,int) or str(value).startswith('@artist:'):
        raise ValueError(f'Unknown artist reference: {value}')
    return _ensure_provisional(conn, str(value), 'unidentified_credit')


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
    redirects = dict(conn.execute('SELECT id,redirect_artist_id FROM artists WHERE redirect_artist_id IS NOT NULL'))
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
