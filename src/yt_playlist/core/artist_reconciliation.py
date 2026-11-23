"""Choose surviving artist state without changing its captured predecessors."""
def latest(rows, timestamp, target):
    if not rows:
        return None
    def order(row):
        return (row[timestamp] if row[timestamp] is not None else float('-inf'),
                row['artist_id'] == target)
    winner = dict(max(rows, key=order))
    winner['artist_id'] = target
    return winner


def candidate(rows, target):
    winner = latest(rows, 'found_at', target)
    if winner is None:
        return None
    winner['found_at'] = min((row['found_at'] for row in rows if row['found_at'] is not None), default=None)
    winner.update(recovery_filename=None, recovery_digest=None, recovery_bytes=None)
    return winner


def move_scan_time(conn, previous, target):
    conn.execute(
        'UPDATE artists AS current SET discography_scanned_at=prior.discography_scanned_at '
        'FROM artists AS prior WHERE current.id=? AND prior.id=? AND prior.discography_scanned_at IS NOT NULL '
        'AND (current.discography_scanned_at IS NULL OR current.discography_scanned_at<prior.discography_scanned_at)',
        (target, previous))
    conn.execute('UPDATE artists SET discography_scanned_at=NULL WHERE id=? AND discography_scanned_at IS NOT NULL',
                 (previous,))


def move_image_retry(conn, previous, target):
    conn.execute(
        'UPDATE artists AS current SET discovery_image_retry_at=prior.discovery_image_retry_at '
        'FROM artists AS prior WHERE current.id=? AND prior.id=? AND prior.discovery_image_retry_at IS NOT NULL '
        'AND (current.discovery_image_retry_at IS NULL OR current.discovery_image_retry_at<prior.discovery_image_retry_at)',
        (target, previous))
    conn.execute('UPDATE artists SET discovery_image_retry_at=NULL '
                 'WHERE id=? AND discovery_image_retry_at IS NOT NULL', (previous,))


def move_lastfm_cache(conn, previous, target):
    conn.execute(
        'UPDATE artists AS current SET lastfm_similar_json=prior.lastfm_similar_json, '
        'lastfm_fetched_at=prior.lastfm_fetched_at FROM artists AS prior '
        'WHERE current.id=? AND prior.id=? AND prior.lastfm_similar_json IS NOT NULL '
        'AND (current.lastfm_similar_json IS NULL OR (prior.lastfm_fetched_at IS NOT NULL '
        'AND (current.lastfm_fetched_at IS NULL OR current.lastfm_fetched_at<prior.lastfm_fetched_at)))',
        (target, previous))
    conn.execute('UPDATE artists SET lastfm_similar_json=NULL,lastfm_fetched_at=NULL '
                 'WHERE id=? AND lastfm_similar_json IS NOT NULL', (previous,))


def origins_after_artist_move(origins, previous, target):
    result = []
    for item in origins:
        artists = {}
        for source in item['artists']:
            row = dict(source)
            if row['artist_id'] == previous:
                row['artist_id'] = target
            existing = artists.get(row['artist_id'])
            if existing is None or row['score'] > existing['score']:
                artists[row['artist_id']] = row
        result.append({**item, 'artists': list(artists.values())})
    return result
