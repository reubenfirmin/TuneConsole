"""Recent layer cost follows its window, independent of lifetime history."""
import numpy as np
import pytest

from yt_playlist.rec import embedding_vectors, layers, rec_params, taste_viz


@pytest.mark.parametrize('consumer,window_h', [
    (layers.now_mode_mix, 6), (layers.session_mode_mix, 24),
    (layers.evidence_coverage, 24), (taste_viz.mode_layers, 24),
])
@pytest.mark.parametrize('quiet', [False, True])
def test_recent_layers_read_and_decode_only_window_keys_once(store, monkeypatch, consumer, window_h, quiet):
    iid = store.identities.upsert_identity('Main', 'cred', None, True)
    now = 10_000_000
    old = [(f'old{i}|artist', f'old{i}', now - 30 * 86400 - i) for i in range(200)]
    recent = [] if quiet else [
        ('now1|artist', 'now1', now - 60),
        ('now2|artist', 'now2', now - 120),
        ('session|artist', 'session', now - 12 * 3600),
        ('now1|artist', 'now1', now - 3600),
    ]
    store.history.import_play_events(iid, old + recent)
    rec_params.set_param(store, 'now_window_h', 6)
    rec_params.set_param(store, 'now_min_events', 2)
    store.rec_model.replace_rec_content_vectors([
        (key, np.array([1., 0.], dtype=np.float32).tobytes())
        for key in dict.fromkeys(r[0] for r in old + recent)])
    store.modes.replace_modes([{'mode_id': 1, 'label': 'A', 'families': [['house', 1]],
        'centroid': np.array([1., 0.], dtype=np.float32), 'size': 203, 'rep_keys': []}],
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
    expected = {key for key, _video, ts in recent if ts >= now - window_h * 3600}
    assert history == [(now - window_h * 3600, sum(ts >= now - window_h * 3600 for _, _, ts in recent))]
    assert generations == [expected]
    assert decoded == [len(expected)]
    if consumer is taste_viz.mode_layers:
        assert result['now']['n'] == (0 if quiet else 2)
        assert result['session']['n'] == (0 if quiet else 3)
        assert result['evidence']['session']['total'] == (0 if quiet else 3)
