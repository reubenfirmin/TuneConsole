"""Bind the listening-layer controller to one coherent recommendation reader."""
from yt_playlist.controllers.listening_layers import ListeningLayers
from yt_playlist.rec import embedding_vectors, layers, rec_params, taste_modes, transient


def _plays(store, since):
    quarantined = store.rec_eligibility.generated_only_keys()
    return [row for row in store.history.play_events_since(
        since, exclude_list_ids=transient.radio_list_ids(store), taste_only=True)
        if row['identity_key'] not in quarantined]


def recent_evidence(store, now, *, window_h=24):
    latest = {row['identity_key']: row['played_at'] for row in _plays(store, now - window_h * 3600)}
    snapshot = store.rec_model.content_generation(keys=list(latest))
    modes = taste_modes.live_modes(store, snapshot=snapshot)
    _keys, vectors, index = embedding_vectors.load_content_vectors(store, snapshot=snapshot)
    return layers.RecentEvidence(latest, modes, vectors, index if vectors is not None else {})


def listening(store):
    return ListeningLayers(parameter=lambda name: rec_params.get_param(store, name),
        evidence=lambda now, **kw: recent_evidence(store, now, **kw),
        plays=lambda since: _plays(store, since))


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
