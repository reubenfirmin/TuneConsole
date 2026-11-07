"""Artist-correlated source assignment, separate from suitability likelihood.

Original measurement families are not assigned at random across music. A
beta-binomial hierarchy estimates source prevalence by genre and artist cluster
without treating ten recordings from one artist as ten independent artists.
Its posterior predictive weights describe the observed measurement scale; they
do not identify a source-independent, true suitability scale.
"""
from collections import defaultdict
import math

import numpy as np
from scipy.optimize import minimize
from scipy.special import betaln, expit, logit, logsumexp, roots_jacobi

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from . import vibe_pooling_evaluation as evaluation

QUADRATURE = 96


def grouped(rows):
    assignment = evaluation.assignments(rows)
    groups, artists = {}, {}
    for row in rows:
        cluster = assignment[str(row['recording'])]['cluster']
        for artist in row['artists']:
            artists[str(artist)] = cluster
        key = row['genre'], cluster
        count = groups.setdefault(key, [0, 0])
        count[0] += 1
        count[1] += int(row['source'].startswith('audio_measurement/'))
    genres = defaultdict(dict)
    for (genre, cluster), count in groups.items():
        genres[genre][cluster] = count
    return genres, artists


def genre_posteriors(groups, prevalence, concentration, dependence, *, points=QUADRATURE):
    alpha, beta = prevalence * concentration, (1 - prevalence) * concentration
    nodes, weights = roots_jacobi(points, beta - 1, alpha - 1)
    nodes, weights = (nodes + 1) / 2, weights / weights.sum()
    if not np.all(weights > 0) or not np.isfinite(weights).all():
        raise WorkBudgetExceeded('Source assignment quadrature is unstable')
    output, loss = {}, 0.
    for genre, clusters in groups.items():
        check_stopping()
        counts = np.asarray(list(clusters.values()))
        totals, audio = counts[:, 0, None], counts[:, 1, None]
        left, right = dependence * nodes, dependence * (1 - nodes)
        likelihood = (betaln(audio + left, totals - audio + right) - betaln(left, right)).sum(axis=0)
        marginal = logsumexp(np.log(weights) + likelihood)
        posterior = np.exp(np.log(weights) + likelihood - marginal)
        output[genre] = {'audio_mean': float(posterior @ nodes),
            'audio_second_moment': float(posterior @ (nodes * nodes)), 'clusters': clusters}
        loss -= float(marginal)
    return float(loss), output


def fit(rows, *, base_bytes=0, memory_bytes=128 * 1024 * 1024):
    if not rows:
        raise ValueError('Source assignment requires original recordings')
    groups, artists = grouped(rows)
    accounted = (base_bytes + len(rows) * 1024 + 8 * 1024 * 1024
        + max(map(len, groups.values())) * QUADRATURE * 2 * 128)
    if accounted > memory_bytes:
        raise WorkBudgetExceeded('Source assignment exceeds its accounted memory budget')
    audio = sum(row['source'].startswith('audio_measurement/') for row in rows)
    if audio in (0, len(rows)):
        return {'prevalence': audio / len(rows), 'concentration': None, 'dependence': None,
                'genres': [], 'artists': artists, 'method': 'one-family-only', 'accounted_memory_bytes': accounted}
    def objective(parameters):
        check_stopping()
        return genre_posteriors(groups, expit(parameters[0]), math.exp(parameters[1]), math.exp(parameters[2]))[0]
    result = minimize(objective, [logit(audio / len(rows)), math.log(10.), math.log(10.)],
        method='L-BFGS-B', jac='3-point', bounds=[(-8., 8.), (math.log(.01), math.log(1000.)),
            (math.log(.01), math.log(1000.))], options={'maxiter': 100, 'ftol': 1e-9})
    if not result.success:
        raise WorkBudgetExceeded('Source assignment fit did not converge: ' + str(result.message))
    prevalence, concentration, dependence = expit(result.x[0]), math.exp(result.x[1]), math.exp(result.x[2])
    loss, posterior = genre_posteriors(groups, prevalence, concentration, dependence)
    # Check the integration error rather than silently trusting one grid.
    refined_loss, refined = genre_posteriors(groups, prevalence, concentration, dependence, points=QUADRATURE * 2)
    if abs(loss - refined_loss) > .01 or any(abs(posterior[g]['audio_mean'] - refined[g]['audio_mean']) > .001 for g in groups):
        raise WorkBudgetExceeded('Source assignment quadrature does not meet its accuracy bound')
    return {'prevalence': float(prevalence), 'concentration': concentration, 'dependence': dependence,
        'genres': [{'genre': genre, **value} for genre, value in refined.items()], 'artists': artists,
        'method': 'artist-cluster-beta-binomial-marginal-maximum-likelihood',
        'iterations': int(result.nit), 'negative_log_likelihood': refined_loss,
        'accounted_memory_bytes': accounted,
        'quadrature_points': QUADRATURE * 2,
        'assumptions': 'Source choices share dependence within artist-cluster/genre; remaining cross-genre dependence is not identified.'}


def probability(value, artists, genre):
    if value['dependence'] is None:
        return value['prevalence']
    posterior = next((item for item in value['genres'] if item['genre'] == genre), None)
    mean = posterior['audio_mean'] if posterior else value['prevalence']
    clusters = {value['artists'][str(artist)] for artist in artists if str(artist) in value['artists']}
    if not clusters or posterior is None:
        return mean
    strength = value['dependence']
    predictions = []
    for cluster in clusters:
        count, audio = posterior['clusters'].get(cluster, [0, 0])
        predictions.append((audio + strength * mean) / (count + strength))
    return float(np.mean(predictions))
