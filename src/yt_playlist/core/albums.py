"""Canonical releases and their track occurrences. Callers own transactions and locks."""
import json
import time

from . import artists


def lookup(conn, browse_id):
    row = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (browse_id,)).fetchone()
    return row[0] if row else None


def _credit_subject(conn, album_id, previous):
    """Identifying this release must not identify other credits sharing an unresolved name."""
    if previous is None:
        return None
    row = conn.execute('SELECT provisional FROM artists WHERE id=?',(previous,)).fetchone()
    if row is None or not row[0]:
        return previous
    other_albums = [r[0] for r in conn.execute('SELECT album_id FROM album_artists '
                    'WHERE artist_id=? AND album_id<>?',(previous,album_id))]
    other_entries = [r[0] for r in conn.execute('SELECT ta.entry_id FROM track_artists ta '
        'WHERE ta.artist_id=? AND NOT EXISTS(SELECT 1 FROM album_tracks at '
        'WHERE at.album_id=? AND at.entry_id=ta.entry_id)',(previous,album_id))]
    if other_albums or other_entries:
        conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)',
            ('ambiguous_album_artist',str(previous),json.dumps({'artist_id':previous,'album_id':album_id,
                'other_albums':other_albums,'other_entries':other_entries}),
             'exact release identifies only its own unresolved credit; shared-name state remains ambiguous'))
    return None if artists.ambiguous_context(conn,previous) else previous


def observe(conn, browse_id, *, title=None, artist=None, year=None, type=None,
            thumbnail=None, genre=None, credits=None, artist_id=None, rank=0):
    if not browse_id:
        raise ValueError('An album requires a YouTube browse ID.')
    aid = lookup(conn,browse_id)
    values = tuple(None if v=='' else v for v in (title,artist,str(year) if year is not None else None,type,thumbnail,genre))
    previous_rank = None
    if aid is None:
        aid = conn.execute('INSERT INTO albums(title,artist,year,type,thumbnail,genre,metadata_rank) VALUES (?,?,?,?,?,?,?)', (*values,rank)).lastrowid
        conn.execute("INSERT INTO album_provider_ids VALUES ('youtube',?,?)", (browse_id,aid))
    else:
        previous_rank = conn.execute('SELECT metadata_rank FROM albums WHERE id=?', (aid,)).fetchone()[0]
        fields = ('title','artist','year','type','thumbnail','genre')
        assignments = ','.join(f'{f}=COALESCE(?,{f})' if rank>=previous_rank else f'{f}=COALESCE({f},?)' for f in fields)
        conn.execute(f'UPDATE albums SET {assignments},metadata_rank=MAX(metadata_rank,?) WHERE id=?', (*values,rank,aid))
    has_credits = conn.execute('SELECT * FROM album_artists WHERE album_id=?', (aid,)).fetchall()
    if (credits or artist_id is not None or artist) and (not has_credits or ((credits or artist_id is not None) and (previous_rank is None or rank>=previous_rank))):
        structured = [c for c in (credits or []) if isinstance(c,dict) and c.get('name')]
        if structured:
            subjects = []
            for c in structured:
                provider_id = c.get('id') or c.get('browseId')
                previous = artists.credit_subject(conn,has_credits,c['name'])
                if provider_id:
                    previous = _credit_subject(conn,aid,previous)
                subjects.append((artists.ensure(conn,c['name'],provider='youtube',provider_id=provider_id,
                                               previous=previous),c['name']))
        else:
            subjects = [(artists.state_id(conn,artist_id if artist_id is not None else artist),artist or '')]
        conn.execute('DELETE FROM album_artists WHERE album_id=?', (aid,))
        conn.executemany('INSERT INTO album_artists VALUES (?,?,?,?)', [(aid,i,subject,credit) for i,(subject,credit) in enumerate(subjects)])
    return aid


def link(conn, entry_id, browse_id=None, *, title=None, artist=None, source='youtube'):
    # A track's album label is retained in its metadata. It does not identify a
    # release, and must not manufacture another album when a refresh omits its ID.
    if not browse_id:
        return None
    aid = observe(conn,browse_id,title=title,artist=artist)
    if not conn.execute('SELECT 1 FROM album_tracks WHERE album_id=? AND entry_id=?', (aid,entry_id)).fetchone():
        conn.execute('INSERT INTO album_tracks(album_id,entry_id,is_full,source) VALUES (?,?,0,?)', (aid,entry_id,source))
    # A single identified performer on this exact release can identify its formerly
    # anonymous credit. Reconcile that provisional subject and retain its handle.
    credit = conn.execute('SELECT aa.artist_id,aa.credit,a.provisional FROM album_artists aa JOIN artists a ON a.id=aa.artist_id WHERE aa.album_id=?', (aid,)).fetchall()
    if len(credit)==1 and credit[0]['provisional']:
        subjects = {r[0] for r in conn.execute(
            'SELECT DISTINCT ta.artist_id FROM album_tracks at JOIN track_artists ta ON ta.entry_id=at.entry_id '
            'JOIN artist_provider_ids p ON p.artist_id=ta.artist_id WHERE at.album_id=?', (aid,))}
        if len(subjects)==1:
            subject = next(iter(subjects))
            if conn.execute('SELECT 1 FROM artist_aliases WHERE artist_id=? AND alias=?', (subject,credit[0]['credit'])).fetchone():
                previous = _credit_subject(conn,aid,credit[0]['artist_id'])
                if previous is not None:
                    subject = artists.reconcile_provisional(conn,previous,subject,source='exact_release')
                conn.execute('UPDATE album_artists SET artist_id=? WHERE album_id=?', (subject,aid))
    return aid


def save(conn, data, *, identity_id=None):
    aid = observe(conn,data['browse'],title=data.get('title'),artist=data.get('artist'),
                  year=data.get('year'),type=data.get('type'),thumbnail=data.get('thumbnail'),
                  credits=data.get('credits'),rank=2)
    conn.execute("INSERT OR IGNORE INTO album_library_memberships(album_id,identity_id,source) VALUES (?,?,'youtube')", (aid,identity_id))
    return aid


def cache_full(conn, browse_id, title, artist, thumbnail, keys, *, entries=None, credits=None, year=None, album_type=None, now=None):
    aid = observe(conn,browse_id,title=title,artist=artist,thumbnail=thumbnail,credits=credits,year=year,type=album_type,rank=3)
    if entries is not None:
        rows = [(e['entry_id'],None,None,i,e.get('disc_number'),e.get('track_number')) for i,e in enumerate(entries)]
    else:
        rows = []
        for key in dict.fromkeys(keys):
            found = conn.execute('SELECT DISTINCT e.id FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id JOIN album_tracks t ON t.entry_id=e.id WHERE t.album_id=? AND r.identity_key=?', (aid,key)).fetchall()
            recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            rows.append((found[0][0],None,None,None,None,None) if len(found)==1 else (None,recording[0] if recording else None,key,None,None,None))
    conn.execute('DELETE FROM album_tracks WHERE album_id=? AND is_full=1', (aid,))
    for entry,rid,key,position,disc,track in rows:
        conn.execute('INSERT INTO album_tracks(album_id,entry_id,recording_id,legacy_track_key,position,disc_number,track_number,is_full,source) VALUES (?,?,?,?,?,?,?,1,\'full_fetch\')', (aid,entry,rid,key,position,disc,track))
    conn.execute('DELETE FROM album_tracks WHERE album_id=? AND is_full=0 AND entry_id IN (SELECT entry_id FROM album_tracks WHERE album_id=? AND is_full=1)', (aid,aid))
    conn.execute('INSERT INTO album_fetch_state VALUES (?,?,1,?) ON CONFLICT(album_id) DO UPDATE SET fetched_at=excluded.fetched_at,complete=1,expected_track_count=excluded.expected_track_count', (aid,time.time() if now is None else now,len(rows)))
    return aid
