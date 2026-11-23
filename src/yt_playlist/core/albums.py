"""Canonical releases and their track occurrences. Callers own transactions and locks."""
from . import artists, album_tracklists


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
    other_entries = [r[0] for r in conn.execute('SELECT ta.entry_id FROM entry_artists ta '
        'WHERE ta.artist_id=? AND NOT EXISTS(SELECT 1 FROM album_tracks at '
        'WHERE at.album_id=? AND at.entry_id=ta.entry_id)',(previous,album_id))]
    if other_albums or other_entries:
        artists.mark_ambiguity(conn, previous)
    return None if artists.ambiguous_context(conn,previous) else previous


def observe(conn, browse_id, *, title=None, artist=None, year=None, type=None,
            thumbnail=None, genre=None, credits=None, artist_id=None, rank=0):
    from . import genre_classifications
    if not browse_id:
        raise ValueError('An album requires a YouTube browse ID.')
    aid = lookup(conn,browse_id)
    fields = ('title','artist','year','type','thumbnail')
    inputs = (title,artist,str(year) if year is not None else None,type,thumbnail)
    values = tuple(None if v=='' else v for v in inputs)
    previous_rank = None
    if aid is None:
        aid = conn.execute(f'INSERT INTO albums({",".join(fields)},metadata_rank) VALUES (' + ','.join('?' for _ in range(len(fields)+1)) + ')', (*values,rank)).lastrowid
        conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id) VALUES ('youtube',?,?)", (browse_id,aid))
    else:
        previous_rank = conn.execute('SELECT metadata_rank FROM albums WHERE id=?', (aid,)).fetchone()[0]
        assignments = ','.join(f'{f}=COALESCE(?,{f})' if rank>=previous_rank else f'{f}=COALESCE({f},?)' for f in fields)
        conn.execute(f'UPDATE albums SET {assignments},metadata_rank=MAX(metadata_rank,?) WHERE id=?', (*values,rank,aid))
    if genre not in (None, '') and (previous_rank is None or rank >= previous_rank
            or genre_classifications.read(conn, 'albums', aid) in (None, '')):
        genre_classifications.write(conn, 'albums', aid, genre)
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
    album_tracklists.associate(conn,aid,entry_id,source)
    # A single identified performer on this exact release can identify its formerly
    # anonymous credit. Reconcile that provisional subject and retain its handle.
    credit = conn.execute('SELECT aa.artist_id,aa.credit,a.provisional FROM album_artists aa JOIN artists a ON a.id=aa.artist_id WHERE aa.album_id=?', (aid,)).fetchall()
    if len(credit)==1 and credit[0]['provisional']:
        subjects = {r[0] for r in conn.execute(
            'SELECT DISTINCT ta.artist_id FROM album_tracks at JOIN entry_artists ta ON ta.entry_id=at.entry_id '
            'JOIN artist_provider_ids p ON p.artist_id=ta.artist_id WHERE at.album_id=?', (aid,))}
        if len(subjects)==1:
            subject = next(iter(subjects))
            if conn.execute('SELECT 1 FROM artist_aliases WHERE artist_id=? AND alias=?', (subject,credit[0]['credit'])).fetchone():
                previous = _credit_subject(conn,aid,credit[0]['artist_id'])
                if previous is not None:
                    subject = artists.reconcile_provisional(conn,previous,subject,source='exact_release')
                conn.execute('UPDATE album_artists SET artist_id=? WHERE album_id=?', (subject,aid))
    return aid
