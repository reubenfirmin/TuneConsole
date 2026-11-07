"""Selection, scheduling and expiry rules for the discovery pool."""
STARTER_LIMIT = 12
STARTER_SCORE = 3.
STARTER_TRACKS_PER_ARTIST = 4
STARTER_TRACK_LIMIT = 24
INTEREST_WEIGHTS = (1., 2., 3.)
ARTWORK_COHORT = 40
ARTWORK_RETRY_SECONDS = 86400
SCAN_DAYS = 5
SCAN_BUDGET = 25
SPOTIFY_MIN_PLAYS = 3
DAY = 86400


def starter_tracks(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row['seed'], []).append(row)
    result, seen = [], set()
    for index in range(STARTER_TRACKS_PER_ARTIST):
        for group in groups.values():
            if index < len(group) and group[index]['recording_id'] not in seen:
                result.append(group[index])
                seen.add(group[index]['recording_id'])
    return result[:STARTER_TRACK_LIMIT]


def scans_due(artists, scanned, now, ttl_days, budget):
    cutoff = now - ttl_days * DAY
    due = [artist for artist in artists if artist['artist_id'] not in scanned
           or (scanned[artist['artist_id']] or 0.) < cutoff]
    due.sort(key=lambda artist: (scanned.get(artist['artist_id']) or float('-inf'), -artist['score']))
    return due[:budget]


def expiry(first_shown, gc_days):
    return first_shown + gc_days * DAY if first_shown is not None else None


def explanation_owner(row):
    if 'preparation_artist_id' in row:
        return row['preparation_artist_id'] if row['preparation_artist_id'] is not None else row['artist']
    return row['artist_id']
