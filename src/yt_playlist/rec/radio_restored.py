"""Restore a published Radio menu before loading candidates for future planning."""
import threading
from copy import deepcopy

from yt_playlist.rec import radio_tuning


class RestoredCandidates:
    """Saved metadata serves projection and selection; planning loads the model.

    The controller's operation lock serializes playlist work. A separate load
    lock also prevents competing planners from constructing duplicate models;
    reading the saved menu never acquires that lock.
    """

    def __init__(self, store, now_fn, state, factory):
        self.store = store
        self._now_fn, self._factory = now_fn, factory
        self._model = None
        self._load_lock = threading.RLock()
        context = state.get('candidate_context') or {}
        self._saved = {
            'tuning': radio_tuning.controls(state.get('tuning') or radio_tuning.DEFAULTS),
            'fingerprint': state.get('fingerprint'), 'comparison': state.get('comparison'),
            'by_video': {}, 'by_key': {}, 'prepared': None,
            'encoding_model': context.get('encoding_model'), 'now_gain': context.get('now_gain', 0.),
            'choice_context': context.get('choice_context') or {},
            'now_posterior': context.get('now_posterior') or {},
            'now_sampled_at': context.get('now_sampled_at', 0),
        }

    @property
    def model_ready(self):
        return self._model is not None

    def __getattr__(self, name):
        model = self._model
        if model is None and name in self._saved:
            return self._saved[name]
        return getattr(model if model is not None else self._load(), name)

    def retain(self, tracks):
        with self._load_lock:
            if self._model is not None:
                self._model.retain(tracks)
                return
            for track in tracks:
                self._saved['by_video'][track['video_id']] = track
                self._saved['by_key'][track['key']] = track

    def _load(self):
        with self._load_lock:
            if self._model is None:
                model = self._factory(self.store, self._now_fn(), self._saved['tuning'],
                                      comparison=self._saved['comparison'], now_source=self)
                try:
                    model.retain(deepcopy(list(self._saved['by_video'].values())))
                except BaseException:
                    prepared = getattr(model, 'prepared', None)
                    if prepared is not None:
                        prepared.close()
                    raise
                self._model = model
            return self._model
