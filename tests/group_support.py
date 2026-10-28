"""Small independently prepared musical corpora; no captured user library data."""
from yt_playlist.rec import group_discovery, group_similarity, group_versions, musical_profiles


def recordings(spec):
    result = []
    for genre, size, character in spec:
        for i in range(size):
            profile = musical_profiles.resolve([], genre=genre, overrides={
                'mood_relaxed': 1 - character, 'mood_aggressive': character,
                'instrumental': character, 'danceability': character, 'bpm': 80 + 80 * character})
            result.append(group_similarity.recording(f'{genre}:{character}:{i}',
                {'genre': genre, 'year': str(1980 + 10 * (i % 4))}, profile))
    return result


def publish(store, tracks, now=1000, previous=None, *, policy=group_discovery.Policy()):
    result = group_discovery.discover(tracks, policy=policy)
    prepared = group_versions.prepare(result, tracks, previous, store.modes.list_modes(False),
        now=now, serial=store.taste_groups.serial(), input_fingerprint=group_versions.fingerprint([
            [track.key, track.genre, track.values] for track in tracks]), policy=policy,
        legacy_versions=store.taste_groups.legacy_versions())
    store.conn.execute('BEGIN')
    with store.conn:
        store.taste_groups.publish(prepared)
    return store.taste_groups.head(members=True)
