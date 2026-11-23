"""NOW and TODAY read bounded evidence regardless of lifetime history."""
import numpy as np
import pytest

from tests.conftest import seed_catalog_track
from tests.group_support import publish_keyed
from yt_playlist.library import listening_composition as layers
from yt_playlist.rec import embedding_vectors, rec_params, taste_viz


@pytest.mark.parametrize('consumer', [
    layers.now_mode_mix, layers.session_mode_mix, layers.evidence_coverage, taste_viz.mode_layers,
])
@pytest.mark.parametrize('quiet', [False, True])
def test_recent_layers_read_and_decode_only_window_keys_once(store, monkeypatch, consumer, quiet):
    monkeypatch.setenv('TZ', 'UTC')
    iid = store.accounts.upsert_account('Main', None, True)
    midnight = 116 * 86400
    now = midnight + 20 * 60
    old = [(f'old{i}|artist', f'old{i}', now - 30 * 86400 - i) for i in range(3)]
    old.append(('earlier|artist', 'earlier', midnight - 3600))
    live = [] if quiet else [
        ('now1|artist', 'now1', midnight - 600),
        ('now2|artist', 'now2', midnight + 60),
        ('now2|artist', 'now2', now - 60),
    ]
    imported = [] if quiet else [
        ('yesterday|artist', 'yesterday', midnight - 300),
        ('today|artist', 'today', midnight + 300),
    ]
    store.history.import_listening_events(iid, old + imported, source_timezone='UTC')
    for key, video, at in live:
        store.history.record_play_event(iid, key, video, at, source_timezone='UTC')
    track_keys = dict.fromkeys((key, video) for key, video, _ in old + imported + live)
    for key, video in track_keys:
        seed_catalog_track(store, key, video, 'Artist', video_id=video)
    if consumer is taste_viz.mode_layers:
        publish_keyed(store, [('Jazz', [key for key, _video in track_keys], .5)])
    rec_params.set_param(store, 'now_min_events', 2)
    store.rec_model.replace_rec_content_vectors([
        (key, np.array([1., 0.], dtype=np.float32).tobytes())
        for key, _video in track_keys])
    store.modes.replace_modes([{'mode_id': 1, 'label': 'A', 'families': [['house', 1]],
        'centroid': np.array([1., 0.], dtype=np.float32), 'size': len(track_keys), 'rep_keys': []}],
        retired_ids=[], now=now)
    history, generations, decoded = [], [], []
    read_plays = store.history.play_events_since
    generation = store.rec_model.content_generation
    load = embedding_vectors.load_content_vectors

    def plays(since, *args, **kwargs):
        rows = read_plays(since, *args, **kwargs)
        history.append((since, len(rows)))
        return rows

    def content(*, keys=None):
        assert keys is not None
        generations.append(set(keys))
        return generation(keys=keys)

    def vectors(*args, **kwargs):
        decoded.append(len(kwargs['snapshot']['library']))
        return load(*args, **kwargs)

    monkeypatch.setattr(store.history, 'play_events_since', plays)
    monkeypatch.setattr(store.rec_model, 'content_generation', content)
    monkeypatch.setattr(embedding_vectors, 'load_content_vectors', vectors)
    result = consumer(store, now)
    expected = set() if quiet else {'now1|artist', 'now2|artist', 'today|artist'}
    expected_read = (midnight, 0) if quiet else (midnight - 600, 5)
    assert history == [expected_read]
    if consumer is taste_viz.mode_layers:
        assert generations == decoded == []
        assert result['now']['n'] == (0 if quiet else 2)
        assert result['session']['n'] == (0 if quiet else 2)
        assert result['evidence']['session']['total'] == (0 if quiet else 2)
    else:
        assert generations == [expected]
        assert decoded == [len(expected)]
