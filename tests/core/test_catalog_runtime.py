import builtins
import json
from copy import deepcopy

import pytest

from yt_playlist.core import catalog_identity, catalog_state
from yt_playlist.core.migrations import v008_recordings


def test_runtime_operations_do_not_import_historical_catalog_helpers(store, monkeypatch):
    original_import = builtins.__import__

    def reject_migration(name, *args, **kwargs):
        if name.endswith('v008_recordings'):
            raise AssertionError('live catalog imported a numbered migration')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', reject_migration)
    missing = store.tracks.upsert_track(None, 'Song', 'Artist', 'Album', 200)
    assert store.tracks.upsert_track(None, 'Song', 'Artist', 'Album', 200) == missing
    first = store.tracks.upsert_track('a', 'Song', 'Artist', 'Album', 200)
    old = store.tracks.catalog_subject(first)['identity_key']
    store.rec_surface.set_cluster_canvas('saved',json.dumps({'nodes': [{'key': old}], 'label': old}))
    second = store.tracks.upsert_track('b', 'Song', 'Artist', 'Other', 200)
    store.tracks.set_track_audio(first, bpm=120)
    store.tracks.associate_youtube_item('b', store.tracks.catalog_subject(first)['recording_id'],
                                source='verified', evidence={'review': 'same recording'}, now=2)
    assert store.tracks.get_track_audio(second)[0] == 120
    saved = json.loads(store.rec_surface.get_cluster_canvas('saved'))
    assert saved['nodes'][0]['key'] == store.tracks.catalog_subject(first)['identity_key']
    assert saved['label'] == old
    assert saved['catalog_state_version'] == 1


@pytest.mark.parametrize('album,title,artist', [('', '', ''), ('Álbum', '曲', 'Artista'), (None, 'Song', 'A')])
def test_runtime_source_entry_ids_preserve_existing_identity(album, title, artist):
    assert catalog_identity.unresolved_ref(album, title, artist) == v008_recordings.unresolved_ref(album, title, artist)


def test_runtime_rekey_changes_only_declared_key_slots_and_preserves_input():
    value = {'nodes': [{'id': 7, 'key': 'old', 'video_id': 'old',
                        'seeds': [{'keys': ['old'], 'label': 'old'}]}],
             'central_seeds': ['old'], 'path_keys': ['old'], 'trunk': [7],
             'label': 'old', 'unknown': {'key': 'old'}}
    original = deepcopy(value)
    result = catalog_state.rekey_state(value, {'old': 'new'})
    assert value == original
    assert result['nodes'][0]['key'] == 'new'
    assert result['nodes'][0]['seeds'][0]['keys'] == ['new']
    assert result['central_seeds'] == ['new']
    assert result['path_keys'] == ['new']
    assert result['nodes'][0]['video_id'] == result['label'] == result['unknown']['key'] == 'old'
    assert result['catalog_state_version'] == 1
    assert result['catalog_identity_version'] == 2


def test_unknown_runtime_state_format_is_preserved_on_live_rekey(store):
    first = store.tracks.upsert_track('a', 'Song', 'Artist', None, 200)
    old = store.tracks.catalog_subject(first)['identity_key']
    future = json.dumps({'catalog_state_version': 999, 'nodes': [{'key': old}]})
    store.rec_surface.set_cluster_canvas('future',future)
    store.tracks.upsert_track('b', 'Song', 'Artist', None, 200)
    assert store.rec_surface.get_cluster_canvas('future') == future
    with pytest.raises(ValueError, match='Unsupported'):
        catalog_state.rekey_state(json.loads(future), {old: 'new'})


def test_historical_state_conversion_does_not_follow_runtime_format_changes(monkeypatch):
    monkeypatch.setattr(catalog_state, 'STATE_VERSION', 99)
    monkeypatch.setattr(catalog_state, 'IDENTITY_VERSION', 99)
    monkeypatch.setattr(catalog_identity, 'RECORDING_FIELDS', ('future_field',))
    historical = v008_recordings.convert_state({'keys': ['old'], 'label': 'old'}, {'old': 'new'})
    assert historical == {'keys': ['new'], 'label': 'old', 'catalog_identity_version': 2}
    assert 'future_field' not in v008_recordings.RECORDING_FIELDS
