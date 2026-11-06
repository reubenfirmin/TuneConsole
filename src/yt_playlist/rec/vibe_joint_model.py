"""Joint Gaussian partial pooling on the continuous measurement scale (#819).

Every row is one original recording, never an upload, play or projected score.
The sparse mixed-model equations condition every random effect jointly without
allocating recording-by-recording kernels. Symbolic fill is admitted before
numeric factorization; an oversized corpus is deferred, never silently sampled.
"""
import base64
from collections import Counter
from functools import lru_cache
import heapq
from importlib.metadata import version as package_version
import json
import math

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping

VERSION = 'joint-vibes-gaussian-source-mixture-3'
MAX_ROWS = 32768
MEMORY_BYTES = 128 * 1024 * 1024
COMPONENTS = ('artist', 'genre', 'interaction', 'album', 'source', 'residual')
# Initialization, not cohort caps. ML estimates the variance components. The
# residual includes unidentified measurement noise when replicates are absent.
INITIAL = dict(zip(COMPONENTS, (.02, .02, .01, .01, .005, .04)))
BASELINE_VARIANCE = .25


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@lru_cache(maxsize=1)
def dependency_versions():
    return package_version('numpy'), package_version('scipy')


def configuration():
    numpy_version, scipy_version = dependency_versions()
    return {'version': VERSION, 'numpy': numpy_version, 'scipy': scipy_version,
            'initial': dict(INITIAL), 'baseline': .5, 'baseline_variance': BASELINE_VARIANCE,
            'variance_bounds': [1e-5, .25], 'max_iterations': 100,
            'gradient': 'central-relative', 'gradient_step': 1e-5,
            'prediction_source': 'canonical-original-empirical-mixture',
            'missing_groups': 'new-group-prior',
            'max_rows': MAX_ROWS, 'memory_bytes': MEMORY_BYTES}


def unique(rows):
    """Fail closed on conflicting observations of one canonical recording."""
    found = {}
    for row in rows:
        check_stopping()
        recording = row['recording']
        if (type(recording) is not int or recording < 1 or type(row['measurement']) is not int
                or row['measurement'] < 1 or row['artists'] != sorted(set(row['artists']))
                or any(type(artist) is not int or artist < 1 for artist in row['artists'])
                or row['genre'] is not None and not isinstance(row['genre'], str)
                or not isinstance(row['source'], str) or not row['source']):
            raise ValueError('Invalid canonical original measurement')
        previous = found.setdefault(recording, row)
        if previous != row:
            raise ValueError('Conflicting original measurements for one recording')
    return [found[key] for key in sorted(found)]


def terms(row, kind):
    artists, genre = row['artists'], row['genre']
    if kind == 'artist':
        return [(str(artist), 1 / len(artists)) for artist in artists]
    if kind == 'interaction':
        return [(json.dumps([artist, genre]), 1 / len(artists)) for artist in artists] if genre else []
    value = row.get(kind)
    return [(str(value), 1.)] if value else []


def _design(rows):
    from scipy.sparse import csr_matrix

    vocabulary, offsets, indices, values, kinds = {}, [0], [], [], []
    multiple_sources = len({row['source'] for row in rows}) > 1
    for row in rows:
        check_stopping()
        for kind in ('baseline', *COMPONENTS[:-1]):
            # A single source cannot identify a second intercept.
            pairs = [('intercept', 1.)] if kind == 'baseline' else terms(row, kind)
            if kind == 'source' and not multiple_sources:
                pairs = []
            for name, weight in pairs:
                key = (kind, name)
                if key not in vocabulary:
                    vocabulary[key] = len(vocabulary)
                    kinds.append(kind)
                indices.append(vocabulary[key])
                values.append(weight)
        offsets.append(len(indices))
    matrix = csr_matrix((values, indices, offsets), shape=(len(rows), len(vocabulary)))
    return matrix, list(vocabulary), kinds


def _ordering(design, base_bytes, memory_bytes):
    """Bound symbolic elimination, including its Python graph, before LU.

    Every co-occurring effect creates one precision edge. Eliminating the
    lowest-degree effect first leaves shared genre/source/intercept effects
    until late. Numeric LU uses this exact symmetric ordering without pivoting.
    The fill bound therefore covers every variance-component iteration.
    """
    size = design.shape[1]
    graph = [set() for _ in range(size)]
    edges, fill, order = 0, size, []

    def admit(heap_size=0):
        # Include graph entries, numeric input/factor copies, serialization and
        # optimizer vectors. This is accounted working data, not process RSS.
        used = base_bytes + size * 2048 + design.nnz * 64 + (edges * 2 + fill) * 128 + heap_size * 96
        if used > memory_bytes:
            raise WorkBudgetExceeded('Joint vibe sparse fill exceeds the fit memory budget')
        return used

    peak = admit()
    for row in range(design.shape[0]):
        check_stopping()
        columns = design.indices[design.indptr[row]:design.indptr[row + 1]].tolist()
        for offset, first in enumerate(columns):
            for second in columns[offset + 1:]:
                if second not in graph[first]:
                    edges += 1
                    peak = max(peak, admit())
                    graph[first].add(second)
                    graph[second].add(first)
            peak = max(peak, admit())
    queue = [(len(neighbors), index) for index, neighbors in enumerate(graph)]
    heapq.heapify(queue)
    active = set(range(size))
    while queue:
        check_stopping()
        degree, index = heapq.heappop(queue)
        if index not in active or degree != len(graph[index]):
            continue
        active.remove(index)
        order.append(index)
        neighbors = sorted(graph[index])
        fill += 2 * len(neighbors)
        edges -= len(neighbors)
        for neighbor in neighbors:
            graph[neighbor].remove(index)
        graph[index].clear()
        for offset, first in enumerate(neighbors):
            for second in neighbors[offset + 1:]:
                if second not in graph[first]:
                    edges += 1
                    peak = max(peak, admit(len(queue)))
                    graph[first].add(second)
                    graph[second].add(first)
            heapq.heappush(queue, (len(graph[first]), first))
            peak = max(peak, admit(len(queue)))
    return order, fill, peak


def _factor(precision, fill):
    import numpy as np
    from scipy.sparse.linalg import splu

    factor = splu(precision, permc_spec='NATURAL', diag_pivot_thresh=0,
                  options={'SymmetricMode': True})
    if (not np.array_equal(factor.perm_r, np.arange(precision.shape[0]))
            or not np.array_equal(factor.perm_c, np.arange(precision.shape[0]))
            or factor.L.nnz + factor.U.nnz - precision.shape[0] > fill
            or np.any(factor.U.diagonal() <= 0)):
        raise WorkBudgetExceeded('Joint vibe factor differs from admitted symmetric fill')
    return factor


def _encoded(array, dtype):
    return base64.b64encode(array.astype(dtype).tobytes()).decode('ascii')


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
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
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
        from threadpoolctl import threadpool_limits

        self.legacy = _LegacyPosterior(value) if value['format'] == 1 else None
        if self.legacy is not None:
            return
        if value['format'] not in (2, 3):
            raise ValueError('Unsupported joint vibe posterior')
        self.value, self.rows = value, unique(value['inputs'])
        self.variances = value['variances']
        if (set(self.variances) != set(COMPONENTS)
                or any(not math.isfinite(item) or item < 0 for item in self.variances.values())
                or self.variances['residual'] <= 0 or len(self.rows) > MAX_ROWS):
            raise ValueError('Invalid joint vibe variance components')
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
        with threadpool_limits(limits=1):
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
            with threadpool_limits(limits=1):
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

    def predict(self, artists, genre, *, album=None, recording=None):
        import numpy as np
        from threadpoolctl import threadpool_limits

        if self.legacy is not None:
            return self.legacy.predict(artists, genre, album=album, recording=recording)
        check_stopping()
        if recording is not None and recording in self.recordings:
            raise ValueError('Target measurement was not hidden from fitting')
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
        with threadpool_limits(limits=1):
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
        from threadpoolctl import threadpool_limits

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
        with threadpool_limits(limits=1):
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
