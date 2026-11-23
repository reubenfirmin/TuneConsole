"""SQL admission for a captured video's exact recording identity."""
from . import association_work, track_sources


def recording(conn, sample, now):
    """Keep an unknown video provisional without matching or renaming text identities."""
    row = conn.execute('SELECT recording_id FROM track_sources WHERE video_id=?',
                       (sample['video_id'],)).fetchone()
    if row:
        return row[0]
    rid = conn.execute('INSERT INTO recordings(identity_key,title,artist,created_at) VALUES (?,?,?,?)',
        ('@sample-video:' + sample['video_id'], sample.get('title'), sample.get('artist'), now)).lastrowid
    track_sources.create(conn, rid, sample['video_id'], None, sample.get('title'), sample.get('artist'))
    association_work.append(conn, sample['video_id'], rid, None, 'sample', '{}', now)
    return rid
