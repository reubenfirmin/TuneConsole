"""Generated membership cannot erase independently eligible recommendations."""
import numpy as np
import pytest

from yt_playlist.rec import library_pool, road_trip_candidates, surfaces
from yt_playlist.util.matching import identity_key


@pytest.mark.parametrize('source', ['wheelhouse', 'catalog', 'comfort', 'library_pool', 'road_trip'])
def test_generated_membership_preserves_candidates_and_user_feedback(store, source):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    tids = [store.tracks.upsert_track(f'v{i}', f'Song {i}', f'Artist {i}', '', 200)
            for i in range(12)]
    keys = [identity_key(f'Song {i}', f'Artist {i}') for i in range(12)]
    pid = store.playlists.upsert_playlist(iid, 'owned', 'My music', 12, None, 1)
    store.playlists.set_playlist_tracks(pid, tids)
    for day in range(4):
        store.history.record_history_plays(iid, day * 86400, keys, source_timezone='UTC')
    store.rec_model.replace_rec_vectors([
        (key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys])
    now = 100 * 86400
    parameters = {'lane': 'explore', 'epoch': 0, 'include_genres': [], 'avoid_genres': []}
    if source == 'library_pool':
        library_pool.prepare(store, now, parameters)

    def picked():
        if source == 'library_pool':
            return {t['key'] for t in library_pool.pick(store, now, parameters)}
        if source == 'road_trip':
            return {t['video_id'] for t in road_trip_candidates.own_candidates(store, now)}
        scorer = {'wheelhouse': surfaces.for_you, 'catalog': surfaces.explore_for_you,
                  'comfort': surfaces.comfort_listening}[source]
        return {t.key for t in scorer(store, now)}

    before = picked()
    assert len(before) == 12
    # The same songs remain in the user's original playlist. Saving any number
    # of generated mixes must not consume them or change their taste fit.
    generated = store.playlists.upsert_playlist(iid, 'generated', 'Generated mix', 12, None, now)
    store.playlists.set_playlist_tracks(generated, tids)
    store.playlists.mark_generated_playlist('generated')
    assert picked() == before
    if source == 'wheelhouse':
        assert all(row['eligible'] for row in surfaces.taste_comparison(store, now, keys=keys))
    store.rec_model.record_dislike(keys[0], until=None, now=now)
    muted = picked()
    assert ('v0' if source == 'road_trip' else keys[0]) not in muted
    assert len(muted) == 11
