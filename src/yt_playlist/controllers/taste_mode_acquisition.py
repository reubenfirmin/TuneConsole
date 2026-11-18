"""Mode metadata/vector acquisition, separate from clustering and reconciliation."""
from yt_playlist.rec import mode_discovery as discovery


class TasteModes:
    def __init__(self, *, model, modes, axes, parameter, space, vectors):
        self.model, self.modes, self.axes, self.parameter = model, modes, axes, parameter
        self.space, self.vectors = space, vectors

    def discover(self, *, k=None, min_members=None, n_rep=6, seed=discovery._SEED,
                 snapshot=None, genres=None, vector_space=None):
        k = self.parameter('modes_k') if k is None else k
        minimum = self.parameter('modes_min_members') if min_members is None else min_members
        snapshot = self.model.content_generation() if snapshot is None else snapshot
        space = self.space(snapshot)
        keys, matrix, _ = self.vectors(snapshot) if vector_space is None else vector_space
        if matrix is None or len(keys) < max(int(minimum), discovery._MIN_CORPUS):
            return []
        genres = self.modes.genres_for(keys) if genres is None else genres
        return discovery.discover_from_inputs(keys, matrix, space=space, genres=genres,
            genre_axes=self.axes(genres.values()), k=k, min_members=minimum, n_rep=n_rep, seed=seed)

    def live(self, *, snapshot=None):
        space = self.space(snapshot)
        return [m for m in self.modes.list_modes(active_only=True) if (m.get('space') or '') == space]

    def prepare(self, now, **kwargs):
        discovered = self.discover(**kwargs)
        if not discovered:
            return None
        return discovery.assign_mode_ids(self.modes.list_modes(active_only=True), discovered,
            threshold=self.parameter('modes_match_threshold'), next_id=self.modes.next_mode_id())
