"""Project enriched recordings into the published collaborative track space."""
from collections import Counter
from hashlib import sha256

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import artist_model, content_character, content_model, embedding_vectors as vectors

VERSION = 'cold-track-projection-1'


class ContentProjection:
    """Fit a ridge map from recording content and related artists to library track vectors.

    The shared content encoder supplies genre, year and compatible audio features.
    Canonical artist neighborhoods give sparse recordings a second source of evidence.
    """

    def __init__(self, model, W, artist_features=None, *, genre_axes=None):
        self.genre_axes = genre_axes
        self.model = model
        self.W = W
        self.artist_features = artist_features or {}
        self.content_dim = model['ncat'] + len(model['cont'])
        self.adim = len(next(iter(self.artist_features.values()), ()))

    def _features(self, genre, year, audio, artists, *, genre_axes=None):
        axes = self.genre_axes if genre_axes is None else genre_axes
        if genre and axes is not None and genre not in axes:
            axes = None
        xc = content_model.encode_content(self.model, genre, year, audio, genre_axes=axes)
        xc = np.zeros(self.content_dim) if xc is None else xc
        rows = [self.artist_features[key] for key in sorted(set(artists)) if key in self.artist_features]
        av = np.mean(rows, axis=0) if rows else np.zeros(self.adim)
        return np.concatenate([xc, av])

    def predict(self, genre, year=None, audio=None, artist=None, *, genre_axes=None):
        artists = (artist,) if isinstance(artist, str) else artist or ()
        return self._features(genre, year, audio, artists, genre_axes=genre_axes) @ self.W

    @classmethod
    def fit(cls, store, lam=1.0, *, training_keys=None, include_artist=True, reference=None):
        """Fit using the given collaborative vectors, or capture the current generation."""
        with store.read_view() as reader:
            reference = vectors.load_vectors(reader) if reference is None else reference
            _, matrix, index = reference
            if matrix is None or len(index) < 20:
                return None
            if training_keys is not None:
                return cls._fit_library(reader, reference, lam, training_keys, include_artist)
            # Even overlays and unpublished arrays can reach the scorer. The exact
            # target vectors and lookup must match before reusing a fitted map.
            digest = sha256(np.ascontiguousarray(matrix)).digest()
            cache_key = (VERSION, lam, include_artist, matrix.shape, matrix.dtype.str, digest, tuple(index.items()))
            return reader.memoized_read(cache_key,
                lambda: cls._fit_library(reader, reference, lam, None, include_artist), owner=VERSION)

    @classmethod
    def _fit_library(cls, reader, reference, lam, training_keys, include_artist):
        fit_keys = reference[2].keys() if training_keys is None else reference[2].keys() & set(training_keys)
        if len(fit_keys) < 20:
            return None
        inputs = vectors.content_inputs(reader)
        refs = reader.artists.track_keys(library=True, keys=fit_keys) if include_artist else {}
        artist_features = artist_model.projection_features(
            reader, Counter(artist for credits in refs.values() for artist in credits)) if refs else {}
        axes = reader.genres.feature_axes(value[0] for value in inputs.content.values())
        return cls.fit_from_inputs(reference, inputs.content, inputs.audio, lam=lam, training_keys=training_keys,
                                   artist_features=artist_features, refs=refs, genre_axes=axes, policy=inputs.policy)

    @classmethod
    def fit_from_inputs(cls, reference, content, audio, lam=1.0, *, training_keys=None,
                        artist_features=None, refs=None, genre_axes=None, policy=None):
        """Fit detached evidence. Each evaluation fold learns its own feature statistics."""
        _, V, idx = reference
        if V is None:
            return None
        if training_keys is not None:
            allowed = set(training_keys)
            idx = {key: row for key, row in idx.items() if key in allowed}
            content = {key: value for key, value in content.items() if key in idx}
            audio = {key: value for key, value in audio.items() if key in idx}
        model = content_model.build_content_model(content, audio, genre_axes=genre_axes, policy=policy)
        refs = refs or {}
        proj = cls(model, None, artist_features, genre_axes=genre_axes)
        rows = []
        for i, key in enumerate(idx):
            if i % 256 == 0:
                check_stopping()
            genre, year = content.get(key, (None, None))
            x = proj._features(genre, year, audio.get(key), refs.get(key, ()))
            if np.any(x):
                rows.append((key, x))
        if len(rows) < 20:
            return None
        F = proj.content_dim + proj.adim
        X = np.array([x for _, x in rows])
        Y = np.array([V[idx[key]] for key, _ in rows])
        proj.W = np.linalg.solve(X.T @ X + lam * np.eye(F), X.T @ Y)
        return proj


def project_discoveries(store, projection, rows, credits):
    """Project enriched recording and credited-artist inputs for Fresh rankings."""
    axes = store.genres.feature_axes(row.get('genre') for row in rows)
    predicted = {}
    for row in vectors.discovery_inputs(store, rows):
        key = row['identity_key']
        audio = content_character.audio(row.get('audio'), row['character'])
        value = np.asarray(projection.predict(row.get('genre'), row.get('year'), audio,
                                               artist=credits.get(key, ()), genre_axes=axes), dtype=np.float32)
        if np.isfinite(value).all() and np.any(value):
            predicted[key] = value
    return predicted
