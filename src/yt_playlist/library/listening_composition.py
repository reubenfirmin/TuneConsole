"""Bind the listening-layer controller to one coherent recommendation reader."""
from yt_playlist.controllers.listening_layers import ListeningLayers
from yt_playlist.library.listening_window import listening_window
from yt_playlist.rec import embedding_vectors, layers, rec_params, taste_modes, transient


def _plays(store, since):
    quarantined = store.rec_eligibility.generated_only_keys()
    return [row for row in store.history.play_events_since(
        since, exclude_list_ids=transient.radio_list_ids(store), taste_only=True)
        if row['identity_key'] not in quarantined]


def layer_plays(store, now):
    return store.memoized_read(('listener-layer-plays', now),
        lambda: _window_plays(store, now, lambda start: _plays(store, start)))


def observed_layer_plays(store, now):
    return store.memoized_read(('observed-layer-plays', now),
        lambda: _window_plays(store, now, lambda start: store.history.play_events_since(start, include_live=True)))


def _window_plays(store, now, events):
    window = listening_window(store, now)
    start = min(start for start, _ in window.days.values())
    if window.active is not None:
        start = min(start, window.active.started_at)
    return window.read(events(start))


def today_coverage(store, now):
    window = listening_window(store, now)
    start = min(start for start, _ in window.days.values())
    observed = window.read(store.history.play_events_since(start))
    return {'tracks': len(observed.today), 'eligible_tracks': len(layer_plays(store, now).today),
            'latest_day': store.history.latest_dated_listening()}


def recent_evidence(store, now, **_legacy):
    captured = layer_plays(store, now)
    latest = {**captured.today, **captured.current}
    snapshot = store.rec_model.content_generation(keys=list(latest))
    modes = taste_modes.live_modes(store, snapshot=snapshot)
    _keys, vectors, index = embedding_vectors.load_content_vectors(store, snapshot=snapshot)
    return layers.RecentEvidence(latest, modes, vectors, index if vectors is not None else {}, captured)


def listening(store):
    return ListeningLayers(parameter=lambda name: rec_params.get_param(store, name),
        evidence=lambda now, **kw: recent_evidence(store, now, **kw),
        plays=lambda now: layer_plays(store, now))


def now_mode_mix(store, now, **kw):
    return listening(store).now_mode_mix(now, **kw)


def now_mode_posterior(store, now):
    return listening(store).now_mode_posterior(now)


def session_mode_mix(store, now, **kw):
    return listening(store).session_mode_mix(now, **kw)


def evidence_coverage(store, now, **kw):
    return listening(store).evidence_coverage(now, **kw)


def session_tilt(store, now, vectors, index):
    return listening(store).session_tilt(now, vectors, index)


def track_tilts(store, now, vectors, index):
    return listening(store).track_tilts(now, vectors, index)
