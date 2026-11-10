"""Cold candidates distinguish scored tracks from eligible metadata fallbacks."""
from contextlib import nullcontext

import pytest

from tests.repository_mock_support import no_database, transient_repository_store
from yt_playlist.rec import surfaces


@pytest.fixture
def cold_store(no_database):
    store = transient_repository_store(
        discovery={'get_discovered_tracks': []},
        artists={'track_keys': {}, 'name_keys': {}},
        rec_model={'get_rec_vectors': [], 'get_rec_artist_vectors': [],
                   'suppressed_keys': set(), 'muted_artists': set(),
                   'get_weights': {}, 'get_lean': 1.},
        rec_query={'track_content': {}, 'track_audio_features': {}},
        genres={'feature_axes': {'Techno': ('techno', None)}},
        values={'read_view': lambda: nullcontext(store)},
    )
    return store


def test_metadata_free_discovery_is_neither_eligible_nor_rankable(cold_store):
    store = cold_store
    store.discovery.get_discovered_tracks.return_value = [
        {'identity_key': 'a|x', 'video_id': 'v', 'title': 'a', 'artist': 'x'}]
    assert surfaces.cold_candidates(store, 0) == []


@pytest.mark.parametrize("genre,year,audio", [("Techno", None, {}), (None, "2000", {}),
                                              (None, None, {"bpm": 120}),
                                              (None, None, {"music_key": "C"})])
def test_metadata_without_model_is_eligible_but_not_rankable(cold_store, genre, year, audio):
    store = cold_store
    store.discovery.get_discovered_tracks.return_value = [
        {'identity_key': 'a|x', 'video_id': 'v', 'title': 'a', 'artist': 'x',
         'genre': genre, 'year': year, 'audio': audio}]
    candidates = surfaces.cold_candidates(store, 0)
    assert [item.key for item in candidates] == ["a|x"]
    assert candidates[0].score is None
