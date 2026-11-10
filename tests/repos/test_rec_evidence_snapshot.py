"""Training membership and eligibility must observe the same library generation."""
import pytest

from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, native_sql_calls, without_query_timing,
)
from yt_playlist.util.matching import identity_key


@pytest.fixture(scope='module')
def adoption_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Training snapshots own actual SQL, without a web lifecycle."""


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_concurrent_adoption_does_not_change_a_pinned_training_generation(tmp_path, adoption_schema):
    with _file_consumer_store(tmp_path, adoption_schema) as store:
        iid = store.accounts.upsert_account('me', 'credential', None, True)
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
