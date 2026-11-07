"""SQL admission for a captured video's exact recording identity."""


def recording(conn, sample, now):
    """Keep an unknown video provisional without matching or renaming text identities."""
    row = conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?',
                       (sample['video_id'],)).fetchone()
    if row:
        return row[0]
    rid = conn.execute('INSERT INTO recordings(identity_key,title,artist,created_at) VALUES (?,?,?,?)',
        ('@sample-video:' + sample['video_id'], sample.get('title'), sample.get('artist'), now)).lastrowid
    conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) '
        'VALUES (?,?,?,?,?,?)', (sample['video_id'], rid, sample.get('title'), sample.get('artist'),
                               sample.get('title'), sample.get('artist')))
    conn.execute('INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?)', (rid, sample['video_id']))
    conn.execute('INSERT INTO item_recording_associations(video_id,recording_id,source,evidence,at) '
        "VALUES (?,?,'sample','{}',?)", (sample['video_id'], rid, now))
    return rid
