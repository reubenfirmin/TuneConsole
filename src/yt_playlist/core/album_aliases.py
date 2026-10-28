"""Reconcile exact provider representations, never releases inferred from a title."""
import hashlib
import json
import time

from yt_playlist.core import albums
from yt_playlist.library.album_fetch import shareable_album_tracks
from yt_playlist.util.duration import parse_duration


def _text(value):
    return ' '.join(str(value or '').split()).casefold()


def fingerprint(album):
    """Missing comparison fields are unknown, rather than evidence of equality."""
    try:
        tracks = shareable_album_tracks(album)
    except (ValueError, AttributeError):
        return None
    title, year = _text(album.get('title')), str(album.get('year') or '')
    artists = album.get('artists') or []
    if not isinstance(artists, list) or any(not isinstance(a, dict) for a in artists):
        return None
    credits = [_text(a.get('name')) for a in artists]
    if not title or len(year) != 4 or not year.isdigit() or not credits or not all(credits):
        return None
    ordered = []
    for track in tracks:
        duration = parse_duration(track.get('duration'))
        explicit = track.get('isExplicit')
        if not _text(track.get('title')) or duration is None or duration <= 0 or type(explicit) is not bool:
            return None
        ordered.append([track['videoId'], _text(track['title']), duration, explicit,
                        track.get('discNumber'), track.get('trackNumber')])
    encoded = json.dumps(['youtube-album-v1', title, credits, year, _text(album.get('type')), ordered],
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest(), [t['videoId'] for t in tracks]


def primary_id(conn, browse_id):
    row = conn.execute("SELECT preferred.provider_id FROM album_provider_ids p "
        "JOIN album_primary_provider_ids preferred ON preferred.album_id=p.album_id "
        "AND preferred.provider=p.provider WHERE p.provider='youtube' AND p.provider_id=?",
        (browse_id,)).fetchone()
    return row[0] if row else browse_id


def observe(conn, browse_id, album, *, now=None):
    """Caller owns the writer transaction. Return False for a changed known alias."""
    existing = conn.execute("SELECT p.is_primary,p.equivalence_digest,p.album_id,f.digest "
        'FROM album_provider_ids p LEFT JOIN album_provider_fingerprints f USING(provider,provider_id) '
        "WHERE p.provider='youtube' AND p.provider_id=?", (browse_id,)).fetchone()
    shared = existing and conn.execute('SELECT 1 FROM album_provider_ids '
        "WHERE provider='youtube' AND album_id=? AND provider_id<>? LIMIT 1",
        (existing['album_id'], browse_id)).fetchone()
    comparison = fingerprint(album)
    if comparison is None:
        return not shared
    digest, videos = comparison
    now = time.time() if now is None else now
    if shared and existing['digest'] != digest:
        return False  # A changed edition must not overwrite the shared last-good tracklist.
    aid = albums.observe(conn, browse_id, title=album['title'],
        artist=', '.join(a['name'] for a in album['artists']), year=album['year'], rank=1)
    conn.execute("INSERT INTO album_provider_fingerprints VALUES ('youtube',?,?,?) "
        'ON CONFLICT(provider,provider_id) DO UPDATE SET digest=excluded.digest,observed_at=excluded.observed_at',
        (browse_id, digest, now))
    candidates = conn.execute('SELECT DISTINCT p.album_id FROM album_provider_fingerprints f '
        'JOIN album_provider_ids p USING(provider,provider_id) '
        "WHERE f.provider='youtube' AND f.digest=? AND p.album_id<>? ORDER BY p.album_id", (digest, aid)).fetchall()
    for candidate in candidates:
        target, retired = sorted((aid, candidate[0]))
        conflict = conn.execute('SELECT 1 FROM album_provider_ids a JOIN album_provider_ids b '
            "ON a.provider=b.provider AND a.provider<>'youtube' AND a.provider_id<>b.provider_id "
            'WHERE a.album_id=? AND b.album_id=? LIMIT 1', (target, retired)).fetchone()
        if not conflict and _compatible(conn, target, videos) and _compatible(conn, retired, videos):
            _merge(conn, target, retired, digest, now)
            aid = target
    return True


def _compatible(conn, aid, videos):
    rows = conn.execute('SELECT t.*,e.video_id FROM album_tracks t '
                        'LEFT JOIN catalog_entries e ON e.id=t.entry_id '
                        'WHERE t.album_id=? ORDER BY t.position IS NULL,t.position,t.id', (aid,)).fetchall()
    allowed = set(videos)
    if any(row['video_id'] not in allowed for row in rows):
        return False  # Includes unresolved recording/key links and historical bonus tracks.
    if any(not row['is_full'] and row['position'] is not None for row in rows):
        return False
    full = [row for row in rows if row['is_full']]
    if full and (any(row['position'] is None for row in full) or
                 [row['video_id'] for row in full] != videos):
        return False
    state = conn.execute('SELECT complete,expected_track_count FROM album_fetch_state WHERE album_id=?',
                         (aid,)).fetchone()
    if full and (not state or not state['complete'] or state['expected_track_count'] != len(full)):
        return False
    if not full and state and state['complete'] and state['expected_track_count']:
        return False
    return True


def _merge(conn, target, retired, digest, now):
    # Preserve the pre-reconciliation ownership/ordering evidence. Exact source IDs
    # on youtube_items and frozen share snapshots remain untouched.
    tables = ('albums', 'album_provider_ids', 'album_artists', 'album_library_memberships',
              'album_discovery_memberships', 'album_tracks', 'album_fetch_state')
    original = {table: [dict(row) for row in conn.execute(
        f'SELECT * FROM {table} WHERE {"id" if table == "albums" else "album_id"} IN (?,?)',
        (target, retired))] for table in tables}
    conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
        ('album_alias_reconciliation', str(retired), json.dumps(original),
         f'complete ordered provider responses matched {digest}; canonical album {target}'))
    for row in conn.execute('SELECT * FROM album_library_memberships WHERE album_id=?', (retired,)).fetchall():
        prior = conn.execute('SELECT id,saved_at FROM album_library_memberships '
                             'WHERE album_id=? AND identity_id IS ?', (target, row['identity_id'])).fetchone()
        if prior:
            times = [t for t in (prior['saved_at'], row['saved_at']) if t is not None]
            conn.execute('UPDATE album_library_memberships SET saved_at=? WHERE id=?',
                         (min(times) if times else None, prior['id']))
            conn.execute('DELETE FROM album_library_memberships WHERE id=?', (row['id'],))
        else:
            conn.execute('UPDATE album_library_memberships SET album_id=? WHERE id=?', (target, row['id']))
    # In-flight remote library snapshots cannot replace ownership captured before
    # this reconciliation. Preserve the regular account snapshot revision guard.
    conn.execute('INSERT INTO album_library_revisions SELECT id,1 FROM identities WHERE 1 '
                 'ON CONFLICT(identity_id) DO UPDATE SET revision=revision+1')
    old = conn.execute('SELECT * FROM album_discovery_memberships WHERE album_id=?', (retired,)).fetchone()
    current = conn.execute('SELECT * FROM album_discovery_memberships WHERE album_id=?', (target,)).fetchone()
    if old and current:
        for field in ('found_at', 'first_shown', 'legacy_first_shown', 'last_shown', 'legacy_last_shown'):
            values = [v for v in (old[field], current[field]) if v is not None]
            value = (max(values) if 'last' in field else min(values)) if values else None
            conn.execute(f'UPDATE album_discovery_memberships SET {field}=? WHERE album_id=?', (value, target))
        conn.execute('UPDATE album_discovery_memberships SET offered_count=offered_count+?, '
                     'legacy_offered_count=legacy_offered_count+? WHERE album_id=?',
                     (old['offered_count'], old['legacy_offered_count'], target))
        conn.execute('DELETE FROM album_discovery_memberships WHERE album_id=?', (retired,))
    elif old:
        conn.execute('UPDATE album_discovery_memberships SET album_id=? WHERE album_id=?', (target, retired))
    has_full = conn.execute('SELECT 1 FROM album_tracks WHERE album_id=? AND is_full=1', (target,)).fetchone()
    if has_full:
        conn.execute('DELETE FROM album_tracks WHERE album_id=? AND is_full=1', (retired,))
    else:
        conn.execute('UPDATE album_tracks SET album_id=? WHERE album_id=? AND is_full=1', (target, retired))
    for row in conn.execute('SELECT id,entry_id FROM album_tracks WHERE album_id=?', (retired,)).fetchall():
        if conn.execute('SELECT 1 FROM album_tracks WHERE album_id=? AND entry_id=?', (target, row['entry_id'])).fetchone():
            conn.execute('DELETE FROM album_tracks WHERE id=?', (row['id'],))
        else:
            conn.execute('UPDATE album_tracks SET album_id=? WHERE id=?', (target, row['id']))
    conn.execute('DELETE FROM album_tracks WHERE album_id=? AND is_full=0 AND entry_id IN '
                 '(SELECT entry_id FROM album_tracks WHERE album_id=? AND is_full=1)', (target, target))
    if not has_full:
        conn.execute('DELETE FROM album_fetch_state WHERE album_id=?', (target,))
        conn.execute('UPDATE album_fetch_state SET album_id=? WHERE album_id=?', (target, retired))
    conn.execute('DELETE FROM album_fetch_state WHERE album_id=?', (retired,))
    # Retain each original proof when an already-aliased release joins another.
    conn.execute('UPDATE album_provider_ids SET is_primary=0,equivalence_digest=?,verified_at=? '
                 'WHERE album_id=? AND is_primary=1 AND provider IN '
                 '(SELECT provider FROM album_provider_ids WHERE album_id=?)', (digest, now, retired, target))
    conn.execute('UPDATE album_provider_ids SET album_id=? WHERE album_id=?', (target, retired))
    conn.execute('UPDATE album_alias_reconciliations SET album_id=? WHERE album_id=?', (target, retired))
    conn.execute('INSERT INTO album_alias_reconciliations VALUES (?,?,?,?)', (retired, target, digest, now))
