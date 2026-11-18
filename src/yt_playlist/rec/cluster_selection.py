"""Cluster ring selection from acquired geometry, metadata and taxonomy."""
import random
from yt_playlist.core.activity import check_cancellable_work


def ring(body, neighbors, metadata, discovered, genres, axes, existing, *, album_cap=2, ring_size=6):
    seeds = body.get('central_seeds') or []
    artists = [s for s in seeds if s.get('kind') == 'artist' and s.get('keys')]
    artist_keys = [set(s['keys']) for s in artists]
    song_keys = {key for s in seeds if s.get('kind') == 'song' for key in s.get('keys', [])}
    preferred = {key for s in artists for key in s['keys']} - song_keys
    counts = {}
    for row in existing.values():
        if album := (row.get('album') or '').strip().lower():
            counts[album] = counts.get(album, 0) + 1
    rng = random.Random(str(body.get('random_seed', 0)))
    variation = .02 if len(artists) > 1 else .12
    rank = {key: score + rng.uniform(-variation, variation) for key, score in sorted(neighbors)} if seeds else dict(neighbors)
    memberships = {key: [i for i, keys in enumerate(artist_keys) if key in keys] for key, _ in neighbors}
    artist_counts = [0] * len(artists)
    candidates = {key: score for key, score in neighbors
                  if (metadata.get(key) or discovered.get(key) or {}).get('video_id')}
    selected = []
    while candidates and len(selected) < int(body.get('k') or ring_size):
        check_cancellable_work()
        eligible = [key for key in candidates
            if not (album := ((metadata.get(key) or discovered[key]).get('album') or '').strip().lower())
            or counts.get(album, 0) < album_cap]
        if not eligible:
            break
        def priority(key):
            members = memberships[key] if key in preferred else []
            return bool(members), -min((artist_counts[i] for i in members), default=0), rank[key]
        key = max(eligible, key=priority)
        score = candidates.pop(key)
        new = key not in metadata and key in discovered
        row = metadata.get(key) or discovered[key]
        if album := (row.get('album') or '').strip().lower():
            counts[album] = counts.get(album, 0) + 1
        for index in memberships[key]:
            artist_counts[index] += 1
        genre = (row.get('genre') if new else genres.get(key, '')) or ''
        selected.append({'key': key, 'video_id': row['video_id'], 'title': row['title'],
            'artist': row['artist'], 'album': row['album'], 'thumbnail': row['thumbnail'],
            'genre': genre, 'family': axes.get(genre, ('', None))[0],
            'score': round(score, 4), 'out_of_corpus': new})
    return {'ring': selected}, [row['key'] for row in selected if row['key'] in discovered]
