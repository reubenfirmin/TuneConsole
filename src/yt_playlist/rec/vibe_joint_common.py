"""Shared sparse algebra and configuration for versioned vibe models.

This module owns no fitter or posterior dispatch. Legacy and calibrated models
depend on it, keeping their numerical implementation independent of each other.
"""
import base64
from functools import lru_cache
import heapq
import hashlib
from importlib.metadata import version as package_version
import json

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping

VERSION = 'joint-vibes-metadata-source-mixture-4'
MAX_ROWS = 32768
MEMORY_BYTES = 128 * 1024 * 1024
COMPONENTS = ('artist', 'genre', 'interaction', 'album', 'source', 'residual')
# Initialization, not cohort caps. ML estimates the variance components. The
# residual includes unidentified measurement noise when replicates are absent.
INITIAL = dict(zip(COMPONENTS, (.02, .02, .01, .01, .005, .04)))
BASELINE_VARIANCE = .25
MAX_MIXTURE = 128
RECIPE = {'prior': 'learned masked production heuristic covariate',
          'album': 'nested within canonical artist',
          'residual': 'audio/provider separate',
          'support': 'provider centered log support; fitted logit variance slope [-10,10]',
          'interval': 'equal-tail empirical source/support Gaussian mixture',
          'mixture_components': MAX_MIXTURE}
# Only symbolic geometry is reusable. Scores, variances, posterior factors and
# means never enter this cache. Eight channels often share the same geometry.
_ORDERING_CACHE = None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@lru_cache(maxsize=1)
def dependency_versions():
    return package_version('numpy'), package_version('scipy')


def configuration(*, initial=INITIAL):
    from . import home_vibe, musical_profiles

    numpy_version, scipy_version = dependency_versions()
    return {'version': VERSION, 'numpy': numpy_version, 'scipy': scipy_version,
            'initial': dict(initial), 'baseline': .5, 'baseline_variance': BASELINE_VARIANCE,
            'variance_bounds': [1e-5, .25], 'max_iterations': 100,
            'gradient': 'central-relative', 'gradient_step': 1e-5,
            'prediction_source': 'canonical-original-empirical-mixture',
            'masked_metadata': dict(RECIPE),
            'metadata_profile_version': musical_profiles.VERSION,
            'metadata_heuristic_sha256': hashlib.sha256(canonical(home_vibe.PRESETS)).hexdigest(),
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


def _design(rows, *, term_builder=None, components=COMPONENTS):
    from scipy.sparse import csr_matrix

    vocabulary, offsets, indices, values, kinds = {}, [0], [], [], []
    multiple_sources = len({row['source'] for row in rows}) > 1
    for row in rows:
        check_stopping()
        for kind in ('baseline', *components[:-1]):
            # A single source cannot identify a second intercept.
            pairs = [('intercept', 1.)] if kind == 'baseline' else (term_builder or terms)(row, kind)
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
    global _ORDERING_CACHE
    check_stopping()
    size = design.shape[1]
    fingerprint = hashlib.sha256(str((design.shape, design.indptr.dtype.str,
                                      design.indices.dtype.str)).encode())
    fingerprint.update(memoryview(design.indptr))
    fingerprint.update(memoryview(design.indices))
    identity = fingerprint.digest()
    cached = _ORDERING_CACHE
    if cached is not None and cached[0] == identity:
        _identity, order, fill, overhead = cached
        if base_bytes + overhead > memory_bytes:
            raise WorkBudgetExceeded('Joint vibe sparse fill exceeds the fit memory budget')
        return list(order), fill, base_bytes + overhead
    # Drop the previous geometry before allocating another symbolic graph.
    _ORDERING_CACHE = cached = None
    input_bytes = base_bytes
    base_bytes += size * 64 + 128  # one immutable cached ordering plus copies
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
    _ORDERING_CACHE = identity, tuple(order), fill, peak - input_bytes
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
