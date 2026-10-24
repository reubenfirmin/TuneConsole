"""Training membership and eligibility must observe the same library generation."""
from yt_playlist.core.store import Store
from yt_playlist.util.matching import identity_key


def test_concurrent_adoption_does_not_change_a_pinned_training_generation(tmp_path):
    store = Store(tmp_path / 'library.db')
    store.init_schema()
    try:
        iid = store.identities.upsert_identity('me', 'credential', None, True)
        pid = store.playlists.upsert_playlist(iid, 'PLGEN', 'Generated mix', 2, 'h', 1)
        tids = [store.tracks.upsert_track(f'v{i}', f'Song {i}', 'Artist', None, None, 1)
                for i in range(2)]
        keys = {identity_key(f'Song {i}', 'Artist') for i in range(2)}
        store.playlists.set_playlist_tracks(pid, tids)
        store.playlists.mark_generated_playlist('PLGEN')
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            assert reader.rec_eligibility.excluded_playlist_ids() == {pid}
            store.playlists.adopt_generated_playlist('PLGEN')
            assert store.rec_eligibility.excluded_playlist_ids() == set()
            assert reader.rec_eligibility.excluded_playlist_ids() == {pid}
            assert reader.rec_evidence.rec_baskets() == []
            assert reader.rec_query.library_keys() == keys
        assert {key for basket in store.rec_evidence.rec_baskets() for key in basket} == keys
    finally:
        store.conn.close()
