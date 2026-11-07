"""Choose surviving artist state without changing its captured predecessors."""
import json


def latest(rows, timestamp, target, *, legacy=False):
    if not rows:
        return None
    def order(row):
        key = (row[timestamp] if row[timestamp] is not None else float('-inf'),
               row['artist_id'] == target)
        return (*key, json.dumps(row, sort_keys=True)) if legacy else key
    winner = dict(max(rows, key=order))
    winner['artist_id'] = target
    if legacy:
        winner['artist'] = f'@artist:{target}'
    return winner


def candidate(rows, target, *, typed, duplicates=0):
    winner = latest(rows, 'found_at', target, legacy=not typed)
    if winner is None:
        return None
    winner['found_at'] = min((row['found_at'] for row in rows if row['found_at'] is not None), default=None)
    if typed:
        winner.update(recovery_filename=None, recovery_digest=None, recovery_bytes=None)
    else:
        for field in ('first_shown', 'legacy_first_shown'):
            winner[field] = min((row[field] for row in rows if row[field] is not None), default=None)
        for field in ('last_shown', 'legacy_last_shown'):
            winner[field] = max((row[field] for row in rows if row[field] is not None), default=None)
        winner['offered_count'] = max(0, sum(row['offered_count'] or 0 for row in rows) - duplicates)
        winner['legacy_offered_count'] = sum(row['legacy_offered_count'] or 0 for row in rows)
    return winner


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
