"""Release publications and frozen source choices; callers own transactions."""
import time


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='album_track_occurrences'").fetchone() is not None


def associate(conn, album_id, entry_id, provider, *, first_seen=None, last_seen=None):
    conn.execute('INSERT INTO album_item_observations(album_id,entry_id,provider,first_seen,last_seen) VALUES (?,?,?,?,?) '
        'ON CONFLICT(album_id,entry_id,provider) WHERE entry_id IS NOT NULL DO UPDATE SET '
        'first_seen=CASE WHEN first_seen IS NULL THEN excluded.first_seen WHEN excluded.first_seen IS NULL THEN first_seen ELSE min(first_seen,excluded.first_seen) END,'
        'last_seen=CASE WHEN last_seen IS NULL THEN excluded.last_seen WHEN excluded.last_seen IS NULL THEN last_seen ELSE max(last_seen,excluded.last_seen) END',
        (album_id,entry_id,provider,first_seen,last_seen))


def begin(conn, album_id, provider_id, *, capture_kind='provider', order_status='known', expected_count=None, fetched_at=None):
    revision = conn.execute("SELECT coalesce(max(revision),0)+1 FROM album_tracklists WHERE provider='youtube' AND provider_id=?",(provider_id,)).fetchone()[0]
    return conn.execute('INSERT INTO album_tracklists(album_id,provider,provider_id,revision,capture_kind,status,order_status,expected_count,fetched_at) '
        "VALUES (?,'youtube',?,?,?,'building',?,?,?)",(album_id,provider_id,revision,capture_kind,order_status,expected_count,fetched_at)).lastrowid


def occurrence(conn, tracklist_id, *, entry_id=None, recording_id=None, raw_identity_key=None, raw_source_ref=None,
               position=None, disc_number=None, track_number=None, captured_title=None, captured_credit=None, captured_source=None, legacy_row_id=None, unavailable=False):
    entry = conn.execute('SELECT e.*,coalesce(y.title,u.title) title,coalesce(y.artist,u.artist) artist '
        'FROM catalog_entries e LEFT JOIN youtube_items y ON y.video_id=e.video_id '
        'LEFT JOIN unresolved_items u ON u.entry_id=e.id WHERE e.id=?',(entry_id,)).fetchone() if entry_id is not None else None
    if entry_id is not None and entry is None:
        raise ValueError('Unknown album occurrence source')
    if entry is not None:
        if recording_id is not None and recording_id!=entry['recording_id']:
            raise ValueError('Album occurrence source disagrees with its recording')
        recording_id = entry['recording_id']
        captured_title = entry['title'] if captured_title is None else captured_title
        captured_credit = entry['artist'] if captured_credit is None else captured_credit
        raw_source_ref = raw_source_ref or entry['video_id'] or entry['source_ref']
    result = conn.execute('INSERT INTO album_track_occurrences(tracklist_id,position,disc_number,track_number,recording_id,'
        'captured_title,captured_credit,raw_identity_key,raw_source_ref,resolution_status,captured_source,legacy_row_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (tracklist_id,position,disc_number,track_number,recording_id,captured_title,captured_credit,raw_identity_key,raw_source_ref,
         'resolved' if recording_id is not None else 'unavailable' if unavailable else 'unresolved',captured_source,legacy_row_id)).lastrowid
    if entry is not None:
        provider = 'youtube' if entry['video_id'] else 'local' if entry['source_ref'].startswith('local:') else 'unresolved'
        conn.execute('INSERT INTO album_occurrence_sources VALUES (?,?,?)',(result,entry_id,provider))
    return result


def finish(conn, tracklist_id, *, complete):
    conn.execute('UPDATE album_tracklists SET status=? WHERE id=?',('complete' if complete else 'partial',tracklist_id))
    if complete:
        conn.execute('UPDATE albums SET current_tracklist_id=? WHERE id=(SELECT album_id FROM album_tracklists WHERE id=?)',
                     (tracklist_id,tracklist_id))


def cache(conn, album_id, provider_id, keys, *, entries=None, now=None, complete=True, expected_count=None):
    rows = []
    if entries is not None:
        rows = [dict(value,position=i) for i,value in enumerate(entries)]
    else:
        # Legacy callers did not capture order. Keep repetitions, without
        # interpreting iteration order as a musical position.
        for key in keys:
            found = conn.execute('SELECT DISTINCT e.id FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id '
                'JOIN album_tracks t ON t.entry_id=e.id WHERE t.album_id=? AND r.identity_key=?',(album_id,key)).fetchall()
            recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(key,)).fetchone()
            rows.append({'entry_id':found[0][0]} if len(found)==1 else
                        {'recording_id':recording[0] if recording else None,'raw_identity_key':key})
    tracklist = begin(conn,album_id,provider_id,capture_kind='provider' if entries is not None else 'legacy_full',
        order_status='known' if entries is not None else 'unknown',expected_count=len(rows) if expected_count is None and complete else expected_count,
        fetched_at=time.time() if now is None else now)
    for row in rows:
        occurrence(conn,tracklist,**row)
    finish(conn,tracklist,complete=bool(rows) and complete)
    collect(conn, provider='youtube', provider_id=provider_id)
    return tracklist


def collect(conn, *, provider, provider_id, keep=2):
    """Bound this publication's unreferenced revisions; captures and frozen shares survive."""
    if keep<0:
        raise ValueError('Retained revision count cannot be negative')
    unused = conn.execute("SELECT t.id,t.provider,t.provider_id,t.status FROM album_tracklists t "
        "WHERE t.provider=? AND t.provider_id=? AND t.capture_kind='provider' "
        "AND t.status IN ('complete','partial') AND NOT EXISTS(SELECT 1 FROM albums a WHERE a.current_tracklist_id=t.id) "
        'AND NOT EXISTS(SELECT 1 FROM album_share_snapshots s WHERE s.tracklist_id=t.id) '
        'ORDER BY t.status,t.id DESC', (provider, provider_id)).fetchall()
    retained = {}
    for row in unused:
        group = (row['provider'],row['provider_id'],row['status'])
        retained[group] = retained.get(group,0)+1
        if retained[group]<=keep:
            continue
        conn.execute('DELETE FROM album_occurrence_sources WHERE occurrence_id IN (SELECT id FROM album_track_occurrences WHERE tracklist_id=?)',(row['id'],))
        conn.execute('DELETE FROM album_track_occurrences WHERE tracklist_id=?',(row['id'],))
        conn.execute('DELETE FROM album_tracklists WHERE id=?',(row['id'],))


def freeze(conn, album_id, provider_id, title, videos, *, now=None, legacy=False):
    """Freeze exact source order without inventing provider completeness."""
    if any(not isinstance(video,str) or not video.strip() for video in videos):
        raise ValueError('An album share requires exact source references')
    published = conn.execute('SELECT t.id FROM albums a JOIN album_tracklists t ON t.id=a.current_tracklist_id '
        "WHERE a.id=? AND t.order_status='known' AND t.expected_count=?",(album_id,len(videos))).fetchone()
    items = []
    if published:
        for position,video in enumerate(videos):
            row = conn.execute('SELECT o.id,s.entry_id FROM album_track_occurrences o JOIN album_occurrence_sources s ON s.occurrence_id=o.id '
                'JOIN catalog_entries e ON e.id=s.entry_id WHERE o.tracklist_id=? AND o.position=? AND e.video_id=?',
                (published[0],position,video)).fetchone()
            if row is None:
                items = []
                break
            items.append((position,row[0],row[1],video))
    if published and len(items)==len(videos):
        tracklist = published[0]
    else:
        tracklist = begin(conn,album_id,provider_id,capture_kind='legacy_share' if legacy else 'share',
                          expected_count=len(videos),fetched_at=now)
        for position,video in enumerate(videos):
            entry = conn.execute('SELECT id FROM catalog_entries WHERE video_id=?',(video,)).fetchone()
            oid = occurrence(conn,tracklist,entry_id=entry[0] if entry else None,raw_source_ref=video,position=position)
            items.append((position,oid,entry[0] if entry else None,video))
        conn.execute("UPDATE album_tracklists SET status='snapshot' WHERE id=?",(tracklist,))
    snapshot = conn.execute('INSERT INTO album_share_snapshots(album_id,tracklist_id,captured_provider_id,title,created_at) VALUES (?,?,?,?,?)',
                            (album_id,tracklist,provider_id,title,now)).lastrowid
    conn.executemany('INSERT INTO album_share_items VALUES (?,?,?,?,?,?)',
                     [(snapshot,position,tracklist,oid,entry,raw) for position,oid,entry,raw in items])
    return snapshot


def share(conn, album_id, provider_id):
    frozen = conn.execute('SELECT id,title FROM album_share_snapshots WHERE album_id=? '
        'ORDER BY captured_provider_id=? DESC,id DESC LIMIT 1',(album_id,provider_id)).fetchone()
    if frozen:
        return {'title':frozen['title'],'video_ids':[r[0] for r in conn.execute(
            'SELECT coalesce(i.raw_source_ref,e.video_id) FROM album_share_items i LEFT JOIN catalog_entries e ON e.id=i.entry_id '
            'WHERE i.snapshot_id=? ORDER BY i.position',(frozen['id'],))]}
    rows = conn.execute('SELECT o.position,e.video_id FROM albums a JOIN album_track_occurrences o ON o.tracklist_id=a.current_tracklist_id '
        "LEFT JOIN album_occurrence_sources s ON s.occurrence_id=o.id AND s.provider='youtube' AND s.entry_id="
        "(SELECT min(entry_id) FROM album_occurrence_sources WHERE occurrence_id=o.id AND provider='youtube') "
        'LEFT JOIN catalog_entries e ON e.id=s.entry_id WHERE a.id=? ORDER BY o.position',(album_id,)).fetchall()
    if not rows or any(r['position']!=i or not r['video_id'] for i,r in enumerate(rows)):
        return None
    return {'title':conn.execute('SELECT title FROM albums WHERE id=?',(album_id,)).fetchone()[0],
            'video_ids':[r['video_id'] for r in rows]}


def merge(conn, target, retired):
    """Retain every revision/share across a verified provider-alias merge."""
    old = conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(retired,)).fetchone()[0]
    current = conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(target,)).fetchone()[0]
    conn.execute('UPDATE albums SET current_tracklist_id=NULL WHERE id=?',(retired,))
    conn.execute('UPDATE album_tracklists SET album_id=? WHERE album_id=?',(target,retired))
    conn.execute('UPDATE album_share_snapshots SET album_id=? WHERE album_id=?',(target,retired))
    for row in conn.execute('SELECT * FROM album_item_observations WHERE album_id=?',(retired,)).fetchall():
        prior = conn.execute('SELECT id FROM album_item_observations WHERE album_id=? AND entry_id IS ? AND provider=?',
                             (target,row['entry_id'],row['provider'])).fetchone() if row['entry_id'] is not None else None
        if prior:
            associate(conn,target,row['entry_id'],row['provider'],first_seen=row['first_seen'],last_seen=row['last_seen'])
            conn.execute('DELETE FROM album_item_observations WHERE id=?',(row['id'],))
        else:
            conn.execute('UPDATE album_item_observations SET album_id=? WHERE id=?',(target,row['id']))
    if current is None and old is not None:
        conn.execute('UPDATE albums SET current_tracklist_id=? WHERE id=?',(old,target))


def reassociate(conn, old_recording, new_recording):
    conn.execute('UPDATE album_track_occurrences SET recording_id=? WHERE recording_id=? '
        'AND NOT EXISTS(SELECT 1 FROM album_occurrence_sources s WHERE s.occurrence_id=album_track_occurrences.id)',(new_recording,old_recording))
    conn.execute('UPDATE album_item_observations SET recording_id=? WHERE entry_id IS NULL AND recording_id=?',(new_recording,old_recording))
