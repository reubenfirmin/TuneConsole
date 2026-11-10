"""Completed catalog/profile inputs for measured selection and clone consumers.

Provider ingestion and progressive profile capture have their own tests. These
fixtures retain all six modes, four lanes and twenty subjects per mode/lane;
the SQL subjects, profile files and typed candidate publications are real.
"""
from yt_playlist.rec import home_vibe, musical_character, musical_profiles

from tests.recommendation_support import seed_publication_subjects


def catalog(store, *, by_mode=False):
    from tests.test_home_other_genres import genre_bundles

    bundles = genre_bundles(store)
    audio = []
    for mid in range(6):
        for items in bundles[str(mid)].values():
            for index, item in enumerate(items):
                item['video_id'] = item['key']
                energy = mid / 5 if by_mode else .1 if index >= 12 else .9
                audio.append((energy, 70 if index >= 12 else 160, 1 - energy, item['key']))
    items = [item for lane in bundles['all'].values() for item in lane]
    seed_publication_subjects(store, items,
                              discoveries=[item['key'] for item in bundles['all']['fresh']])
    with store.conn:
        store.conn.executemany('UPDATE recordings SET energy=?,bpm=?,mood_relaxed=? WHERE identity_key=?', audio)
    store._fixture_bundle_payload = bundles
    return bundles


def profiles(store, bundles):
    # Compute each distinct completed musical input once. Keep full resolved
    # bodies and real digest-checked FAO chunks, rather than mocking profile reads.
    cache, rows = {}, []
    subjects = {row['identity_key']: row for row in store.conn.execute(
        'SELECT id,identity_key,energy,bpm,mood_relaxed FROM recordings')}
    for lane in bundles['all'].values():
        for item in lane:
            subject = subjects[item['key']]
            identity = item['genre'], subject['energy'], subject['bpm'], subject['mood_relaxed']
            if identity not in cache:
                profile = musical_profiles.resolve([], genre=item['genre'], selected={
                    name: subject[name] for name in ('energy', 'bpm', 'mood_relaxed')})
                profile['sources_truncated'] = False
                cache[identity] = musical_profiles.canonical(profile), musical_profiles.digest(profile)
            raw, digest = cache[identity]
            rows.append((item['key'], raw, musical_profiles.VERSION, digest))
    store.musical_profiles.publish_recordings(rows, 1000)
    # This is completed lower capture input; no metadata worker is owned here.
    with store.conn:
        store.conn.execute('DELETE FROM rec_content_pending')


def publish(store, bundles):
    from tests.recommendation_support import publish_bundles

    # Pool selection has separate owners. Cloning consumes its complete lower
    # result, with all 480 actual profiles and each preset's typed SQL/file head.
    compact = musical_character.load(store, [item['key'] for items in bundles['all'].values() for item in items])
    for items in bundles['all'].values():
        for item in items:
            item['musical'] = compact[item['key']]
    bundles['_vibes'] = {'version': home_vibe.VERSION,
                         'profile_version': musical_character.VERSION,
                         'pools': {preset: bundles['all'] for preset in home_vibe.PRESETS}, 'genres': {}}
    # Fixture construction owns this private writer. Keep its completed
    # publication in one real lock scope instead of admitting a fresh reporting
    # snapshot for each SQL manifest lookup. Concurrency belongs to publication
    # tests; clone consumers still open their ordinary real readers.
    with store._lock:
        publish_bundles(store, bundles, 1, fresh=bundles['all']['fresh'])
