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

    def __init__(self, store, now_fn, state, factory, *, initial_queue=False):
        self.store = store
        self._now_fn, self._factory = now_fn, factory
        self._model = None
        self._initial_queue = initial_queue
        self._load_lock = threading.RLock()
        context = state.get('candidate_context') or {}
        choice_context = dict(context.get('choice_context') or {})
        choice_context.pop('comparison', None)
        self._saved = {
            'tuning': radio_tuning.controls(state.get('tuning') or radio_tuning.DEFAULTS),
            'fingerprint': state.get('fingerprint'),
            'by_video': {}, 'by_key': {}, 'prepared': None,
            'encoding_model': context.get('encoding_model'), 'now_gain': context.get('now_gain', 0.),
            'choice_context': choice_context,
            'group_generation': choice_context.get('group_generation'),
            'group_allocation': context.get('group_allocation') or {},
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
        return getattr(model if model is not None else self.planning_model(), name)

    def retain(self, tracks):
        with self._load_lock:
            if self._model is not None:
                self._model.retain(tracks)
                return
            for track in tracks:
                self._saved['by_video'][track['video_id']] = track
                self._saved['by_key'][track['key']] = track

    def for_tuning(self, values):
        model = self.planning_model(initial_queue=True)
        plan = getattr(model, 'for_tuning', None)
        return plan(values) if plan else None

    def planning_model(self, *, initial_queue=False):
        with self._load_lock:
            if self._model is None:
                options = {'initial_queue': True} if self._initial_queue or initial_queue else {}
                model = self._factory(self.store, self._now_fn(), self._saved['tuning'], now_source=self, **options)
                try:
                    model.retain(deepcopy(list(self._saved['by_video'].values())))
                except BaseException:
                    prepared = getattr(model, 'prepared', None)
                    if prepared is not None:
                        prepared.close()
                    raise
                self._model = model
            return self._model

    def close(self):
        with self._load_lock:
            if self._model is not None:
                self._model.close()
