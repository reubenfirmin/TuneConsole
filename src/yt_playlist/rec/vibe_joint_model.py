"""Joint Gaussian partial pooling on the continuous measurement scale (#819).

Every row is one original recording, never an upload, play or projected score.
The sparse mixed-model equations condition every random effect jointly without
allocating recording-by-recording kernels. Symbolic fill is admitted before
numeric factorization; an oversized corpus is deferred, never silently sampled.
"""
import base64
from collections import Counter
import math

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.numerics import single_threaded
from . import vibe_joint_common as common
from .vibe_joint_common import (BASELINE_VARIANCE, COMPONENTS, INITIAL, MAX_ROWS, MEMORY_BYTES,
                               VERSION, _encoded, _factor, _ordering, canonical, terms, unique)


def configuration():
    return common.configuration(initial=INITIAL)


def _design(rows, *, term_builder=None):
    # Keep the legacy research adapter's explicit term/component extension
    # points; the production calibrated model calls common algebra directly.
    return common._design(rows, term_builder=term_builder or terms, components=COMPONENTS)


def fit(rows, vibe, *, fixed=None, memory_bytes=MEMORY_BYTES):
    """Exact conditional Gaussian posterior; ML pooling, or fixed test settings.

    Variance-component uncertainty is not integrated out. The posterior and
    calibration report must disclose that plug-in approximation. A single
    measurement per song cannot identify classifier noise separately from true
    between-song residuals: that combined variance is kept in new-track risk.
    Independent replicate variance can be supplied in ``noise``; coverage/support
    is not passed off as a calibrated observation precision.
    """
    rows = unique(rows)
    if any('metadata_prior' in row for row in rows):
        if not all('metadata_prior' in row for row in rows):
            raise ValueError('Every calibrated original requires its masked metadata prior')
        from . import vibe_joint_calibrated

        return vibe_joint_calibrated.fit(rows, vibe, fixed=fixed, memory_bytes=memory_bytes)
    count = len(rows)
    base_bytes = len(canonical(rows)) * 4 + count * 2048 + 8 * 1024 * 1024
    if count > MAX_ROWS or base_bytes > memory_bytes:
        raise WorkBudgetExceeded('Joint vibe originals exceed the fit memory budget')
    if not count:
        return None
    if any(not 0 <= row['score'] <= 1 or not 0 < row['support'] <= 1
           or not math.isfinite(row.get('noise', 0.)) or row.get('noise', 0.) < 0 for row in rows):
        raise ValueError('Invalid original vibe measurement')
    import numpy as np
    from scipy.optimize import minimize
    from scipy.sparse import diags

    with single_threaded():
        design, features, kinds = _design(rows)
        order, fill, accounted = _ordering(design, base_bytes, memory_bytes)
        design = design[:, order]
        kinds = [kinds[index] for index in order]
        available = [kind in kinds or kind == 'residual' for kind in COMPONENTS]
        kind_ids = np.asarray([-1 if kind == 'baseline' else COMPONENTS.index(kind) for kind in kinds])
        response = np.asarray([row['score'] - .5 for row in rows])
        noise = np.asarray([row.get('noise', 0.) for row in rows])
        active = [index for index, present in enumerate(available) if present]

        def posterior(variances):
            check_stopping()
            prior = np.where(kind_ids < 0, BASELINE_VARIANCE, variances[np.maximum(kind_ids, 0)])
            diagonal = variances[-1] + noise
            weighted = design.multiply((1 / diagonal)[:, None])
            precision = (design.T @ weighted + diags(1 / prior)).tocsc()
            precision.sort_indices()
            factor = _factor(precision, fill)
            rhs = design.T @ (response / diagonal)
            mean = factor.solve(rhs)
            # Matrix determinant lemma and Woodbury identity give the same
            # marginal likelihood as the dense recording covariance.
            loss = .5 * (response @ (response / diagonal) - rhs @ mean
                + np.log(diagonal).sum() + np.log(prior).sum()
                + np.log(factor.U.diagonal()).sum() + count * np.log(2 * np.pi))
            check_stopping()
            return precision, mean, float(loss)

        def objective(log_variances):
            variances = np.zeros(len(COMPONENTS))
            variances[active] = np.exp(log_variances)
            return posterior(variances)[2]

        if fixed is None and count >= 4:
            result = minimize(objective, np.log([INITIAL[COMPONENTS[i]] for i in active]),
                              method='L-BFGS-B', jac='3-point',
                              bounds=[(math.log(1e-5), math.log(.25))] * len(active),
                              options={'maxiter': 100, 'ftol': 1e-9, 'finite_diff_rel_step': 1e-5})
            if not result.success:
                raise WorkBudgetExceeded('Joint vibe variance fit did not converge: '
                                         + str(result.message) + f' ({result.nit} iterations)')
            variances = np.zeros(len(COMPONENTS))
            variances[active] = np.exp(result.x)
            diagnostics = {'method': 'marginal-maximum-likelihood', 'iterations': int(result.nit),
                           'negative_log_likelihood': float(result.fun)}
        else:
            settings = fixed if fixed is not None else INITIAL
            if any(not math.isfinite(settings[name]) or settings[name] <= 0 for name in COMPONENTS):
                raise ValueError('Fixed variance settings must be positive and finite')
            variances = np.asarray([settings[name] if present else 0.
                                    for name, present in zip(COMPONENTS, available)])
            diagnostics = {'method': 'fixed-control' if fixed is not None else 'sparse-prior', 'iterations': 0}
        precision, mean, _loss = posterior(variances)
    return {'format': 3, 'vibe': vibe, 'configuration': configuration(), 'inputs': rows,
            'variances': dict(zip(COMPONENTS, map(float, variances))),
            'order': order, 'effect_mean': mean.tolist(),
            'precision': {'indptr': _encoded(precision.indptr, '<i4'),
                          'indices': _encoded(precision.indices, '<i4'),
                          'values': _encoded(precision.data, '<f8')},
            'diagnostics': {**diagnostics, 'negative_log_likelihood': _loss,
                            'recordings': count, 'accounted_memory_bytes': accounted,
                            'effects': len(features), 'factor_nonzeros': fill,
                            'precision_nonzeros': precision.nnz,
                            'artists': len({a for row in rows for a in row['artists']}),
                            'sources': sorted({row['source'] for row in rows}),
                            'noise_identified': all('noise' in row for row in rows),
                            'uncertainty': 'conditional-on-fitted-variance-components',
                            'residual': 'between-track-plus-unidentified-measurement-noise',
                            'bias': 'shared-classifier-bias-not-identified-by-measurement-holdout'}}


class Posterior:
    """Preparation-only reader. Product score queries never construct this."""
    def __init__(self, value):
        import numpy as np
        from scipy.sparse import csc_matrix

        self.legacy = _LegacyPosterior(value) if value['format'] == 1 else None
        if self.legacy is not None:
            return
        if value['format'] not in (2, 3, 4):
            raise ValueError('Unsupported joint vibe posterior')
        self.value, self.rows = value, unique(value['inputs'])
        self.variances = value['variances']
        if (set(self.variances) != set(COMPONENTS)
                or any(not math.isfinite(item) or item < 0 for item in self.variances.values())
                or self.variances['residual'] <= 0 or len(self.rows) > MAX_ROWS):
            raise ValueError('Invalid joint vibe variance components')
        if value['format'] == 4:
            from . import vibe_joint_calibrated

            vibe_joint_calibrated.validate_rows(self.rows)
            self.population = vibe_joint_calibrated.population(self.rows)
            design, features, _kinds = vibe_joint_calibrated.design(self.rows)
            diagnostics = value['diagnostics']
            residuals = diagnostics['residual_by_family']
            families = {'residual_audio' if row['source'].startswith('audio_measurement/')
                        else 'residual_provider' for row in self.rows}
            if (set(residuals) != {'residual_audio', 'residual_provider'}
                    or any(not math.isfinite(item) or not 0 <= item <= .25 for item in residuals.values())
                    or any(residuals[family] <= 0 for family in families)
                    or not math.isfinite(diagnostics['support_slope']) or abs(diagnostics['support_slope']) > 10
                    or not math.isfinite(diagnostics['provider_log_support_center'])):
                raise ValueError('Invalid calibrated residual model')
        else:
            design, features, _kinds = _design(self.rows)
        base_bytes = len(canonical(self.rows)) * 4 + len(self.rows) * 2048 + 8 * 1024 * 1024
        order, fill, _accounted = _ordering(design, base_bytes, MEMORY_BYTES)
        if value['order'] != order:
            raise ValueError('Invalid joint vibe effect ordering')
        self.features = {features[original]: index for index, original in enumerate(order)}
        size = len(features)
        encoded = value['precision']
        offsets = np.frombuffer(base64.b64decode(encoded['indptr'], validate=True), dtype='<i4')
        indices = np.frombuffer(base64.b64decode(encoded['indices'], validate=True), dtype='<i4')
        numbers = np.frombuffer(base64.b64decode(encoded['values'], validate=True), dtype='<f8')
        self.mean = np.asarray(value['effect_mean'])
        if (offsets.size != size + 1 or offsets[0] != 0 or offsets[-1] != indices.size
                or np.any(np.diff(offsets) < 0) or numbers.size != indices.size or indices.size > fill
                or np.any(indices < 0) or np.any(indices >= size) or not np.isfinite(numbers).all()
                or self.mean.shape != (size,) or not np.isfinite(self.mean).all()):
            raise ValueError('Invalid joint vibe posterior dimensions or values')
        precision = csc_matrix((numbers, indices, offsets), shape=(size, size))
        if not precision.has_canonical_format or (precision != precision.T).nnz:
            raise ValueError('Invalid joint vibe symmetric precision')
        with single_threaded():
            self.factor = _factor(precision, fill)
        self.artists = {artist for row in self.rows for artist in row['artists']}
        self.genres = {row['genre'] for row in self.rows if row['genre']}
        self.recordings = {row['recording'] for row in self.rows}
        self.coverage = sum(row['support'] for row in self.rows) / len(self.rows)
        self.source_average = np.zeros(size)
        self.source_variation = 0.
        self.source_prior = 0.
        if value['format'] == 3:
            # The target has no own measurement source. Marginalize over the
            # observed source population instead of inventing a new classifier
            # with a zero effect and the full source prior variance. Each
            # canonical recording contributes once to this distribution.
            sources = Counter(row['source'] for row in self.rows)
            weights = {self.features['source', source]: count / len(self.rows)
                       for source, count in sources.items() if ('source', source) in self.features}
            for index, weight in weights.items():
                self.source_average[index] = weight
            self.source_prior = self.variances['source'] * sum(weight ** 2 for weight in weights.values())
            source_mean = float(self.source_average @ self.mean)
            with single_threaded():
                variance = 0.
                vector = np.zeros(size)
                for index, weight in weights.items():
                    check_stopping()
                    vector[index] = 1.
                    variance += weight * (self.factor.solve(vector)[index]
                                           + (self.mean[index] - source_mean) ** 2)
                    vector[index] = 0.
                self.source_variation = max(0., float(variance - self.source_average
                    @ self.factor.solve(self.source_average)))

    def predict(self, artists, genre, *, album=None, recording=None, metadata_prior=None):
        import numpy as np

        if self.legacy is not None:
            return self.legacy.predict(artists, genre, album=album, recording=recording)
        check_stopping()
        if recording is not None and recording in self.recordings:
            raise ValueError('Target measurement was not hidden from fitting')
        if self.value['format'] == 4:
            from . import vibe_joint_calibrated

            return vibe_joint_calibrated.predict(self, artists, genre, album=album, metadata_prior=metadata_prior)
        known_artist = bool(self.artists.intersection(artists))
        known_genre = bool(genre) and genre in self.genres
        mixture = self.value['format'] == 3
        absent = not artists and not genre
        if absent or not mixture and not known_artist and not known_genre:
            return None
        query = {'artists': artists, 'genre': genre, 'album': album, 'source': None}
        vector = self.source_average.copy() if mixture else np.zeros(len(self.features))
        vector[self.features['baseline', 'intercept']] = 1.
        prior, new_groups = BASELINE_VARIANCE + self.source_prior, 0.
        for kind in COMPONENTS[:-1]:
            if mixture and kind == 'source':
                continue
            pairs = terms(query, kind)
            if not pairs and (mixture or kind in ('album', 'source')):
                prior += self.variances[kind]
                new_groups += self.variances[kind]
            for name, weight in pairs:
                variance = self.variances[kind] * weight * weight
                prior += variance
                index = self.features.get((kind, name))
                if index is None:
                    new_groups += variance
                else:
                    vector[index] = weight
        with single_threaded():
            mean_variance = max(0., float(new_groups + vector @ self.factor.solve(vector)))
        score = max(0., min(1., float(.5 + vector @ self.mean)))
        support = max(0., min(1., self.coverage * (1 - mean_variance / prior)))
        kind = ('artist_genre' if known_artist and known_genre else 'artist' if known_artist
                else 'genre' if known_genre else 'artist' if artists else 'genre')
        return {'score': score, 'support': support, 'source': kind,
                'recordings': len(self.rows), 'mean_variance': mean_variance,
                'new_track_variance': mean_variance + self.variances['residual'] + self.source_variation}


class _LegacyPosterior:
    """Read already-published dense generations during continuing preparation."""
    def __init__(self, value):
        import numpy as np

        self.value, self.rows = value, value['inputs']
        variances = value['variances']
        if (set(variances) != set(COMPONENTS)
                or any(not math.isfinite(item) or item < 0 for item in variances.values())
                or variances['residual'] <= 0):
            raise ValueError('Invalid joint vibe variance components')
        size = len(self.rows)
        triangle = np.frombuffer(base64.b64decode(value['cholesky'], validate=True), dtype='<f8')
        if size > 1024 or triangle.size != size * (size + 1) // 2:
            raise ValueError('Invalid joint vibe posterior dimensions')
        self.lower = np.zeros((size, size))
        self.lower[np.tril_indices(size)] = triangle
        self.alpha = np.asarray(value['alpha'])
        if (self.alpha.shape != (size,) or not np.isfinite(self.alpha).all()
                or not np.isfinite(self.lower).all() or np.any(np.diag(self.lower) <= 0)):
            raise ValueError('Invalid joint vibe posterior values')
        self.terms = [{kind: dict(terms(row, kind)) for kind in COMPONENTS[:-1]} for row in self.rows]

    def predict(self, artists, genre, *, album=None, recording=None):
        import numpy as np
        from scipy.linalg import solve_triangular

        if recording is not None and any(row['recording'] == recording for row in self.rows):
            raise ValueError('Target measurement was not hidden from fitting')
        artist_set = set(artists)
        known_artist = any(artist_set.intersection(row['artists']) for row in self.rows)
        known_genre = bool(genre) and any(row['genre'] == genre for row in self.rows)
        if not known_artist and not known_genre:
            return None  # Missing/broad/unknown metadata retains the heuristic.
        query = {'artists': artists, 'genre': genre, 'album': album, 'source': None}
        features = {kind: dict(terms(query, kind)) for kind in COMPONENTS[:-1]}
        variances = self.value['variances']
        covariance = np.full(len(self.rows), BASELINE_VARIANCE)
        prior = BASELINE_VARIANCE
        for kind, feature in features.items():
            # Missing album/source means a new nuisance group: retain its risk,
            # without borrowing a known classifier/album offset for the target.
            prior += variances[kind] * (sum(weight * weight for weight in feature.values()) if feature else
                                        float(kind in ('album', 'source')))
            for index, row in enumerate(self.terms):
                covariance[index] += variances[kind] * sum(weight * row[kind].get(key, 0.)
                                                           for key, weight in feature.items())
        with single_threaded():
            projected = solve_triangular(self.lower, covariance, lower=True, check_finite=False)
        mean_variance = max(0., float(prior - projected @ projected))
        score = max(0., min(1., float(.5 + covariance @ self.alpha)))
        # Support remains coverage/strength for a derived fit, never suitability
        # or a calibrated probability. Explicit variances live in its receipt.
        coverage = sum(row['support'] for row in self.rows) / len(self.rows)
        support = max(0., min(1., coverage * (1 - mean_variance / prior)))
        kind = 'artist_genre' if known_artist and known_genre else 'artist' if known_artist else 'genre'
        return {'score': score, 'support': support, 'source': kind,
                'recordings': len(self.rows),
                'mean_variance': mean_variance,
                'new_track_variance': mean_variance + variances['residual']}
