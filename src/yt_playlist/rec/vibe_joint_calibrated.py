"""Format-four masked-metadata mixed model and source/support prediction.

The metadata covariate is an input, never a response or a second recording.
Provider support predicts residual variance with a learned slope of either
sign. Equal-tail intervals integrate the empirical source/support population;
variance-component uncertainty and unidentified classifier bias remain explicit.
"""
from collections import Counter
import json
import math

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.numerics import single_threaded
from . import vibe_joint_common as joint

MAX_MIXTURE = joint.MAX_MIXTURE
RECIPE = joint.RECIPE


def terms(row, kind):
    if kind == 'album':
        return [(json.dumps([artist, row['album']]), 1 / len(row['artists']))
                for artist in row['artists']] if row.get('album') and row['artists'] else []
    return joint.terms(row, kind)


def design(rows):
    import numpy as np
    from scipy.sparse import csr_matrix, hstack

    matrix, features, kinds = joint._design(rows, term_builder=terms)
    prior = csr_matrix(np.asarray([row['metadata_prior'] - .5 for row in rows])[:, None])
    return (hstack((matrix, prior), format='csr'),
            [*features, ('baseline', 'metadata-prior')], [*kinds, 'baseline'])


def population(rows):
    result = Counter((row['source'], row['support'], row.get('noise', 0.)) for row in rows)
    if len(result) > MAX_MIXTURE:
        raise WorkBudgetExceeded('Joint vibe source/support mixture exceeds component bound')
    return result


def validate_rows(rows):
    if any(not row['source'].startswith(('audio_measurement/', 'provider_measurement/'))
           or not math.isfinite(row['metadata_prior']) or not 0 <= row['metadata_prior'] <= 1
           or not math.isfinite(row['score']) or not 0 <= row['score'] <= 1
           or not math.isfinite(row['support']) or not 0 < row['support'] <= 1
           or not math.isfinite(row.get('noise', 0.)) or row.get('noise', 0.) < 0 for row in rows):
        raise ValueError('Invalid calibrated original measurement or masked metadata prior')
    population(rows)


def fit(rows, vibe, *, fixed=None, memory_bytes=joint.MEMORY_BYTES):
    import numpy as np
    from scipy.optimize import minimize
    from scipy.sparse import diags
    from scipy.special import expit, logit

    rows = joint.unique(rows)
    if not rows:
        return None
    if len(rows) > joint.MAX_ROWS:
        raise WorkBudgetExceeded('Joint vibe originals exceed the fit memory budget')
    validate_rows(rows)
    base_bytes = len(joint.canonical(rows)) * 4 + len(rows) * 2048 + 8 * 1024 * 1024
    if base_bytes > memory_bytes:
        raise WorkBudgetExceeded('Joint vibe originals exceed the fit memory budget')
    with single_threaded():
        matrix, features, kinds = design(rows)
        order, fill, accounted = joint._ordering(matrix, base_bytes, memory_bytes)
        matrix, kinds = matrix[:, order], [kinds[index] for index in order]
        groups = joint.COMPONENTS[:-1]
        names, offset = (*groups, 'residual_provider', 'residual_audio'), len(groups)
        families = np.asarray([offset + int(row['source'].startswith('audio_measurement/')) for row in rows])
        active = [index for index, present in enumerate(
            [kind in kinds for kind in groups] + [bool(np.any(families == index))
                                                for index in (offset, offset + 1)]) if present]
        kind_ids = np.asarray([-1 if kind == 'baseline' else groups.index(kind) for kind in kinds])
        response = np.asarray([row['score'] - .5 for row in rows])
        provider = families == offset
        support = np.asarray([math.log(row['support']) for row in rows])
        center = float(support[provider].mean()) if np.any(provider) else 0.
        support = np.where(provider, support - center, 0.)
        noise = np.asarray([row.get('noise', 0.) for row in rows])

        def residuals(variances, slope):
            probability = np.clip((variances[families] - 1e-5) / (.25 - 1e-5), 1e-9, 1 - 1e-9)
            return 1e-5 + (.25 - 1e-5) * expit(logit(probability) + slope * support)

        def posterior(variances, slope):
            check_stopping()
            prior = np.where(kind_ids < 0, joint.BASELINE_VARIANCE, variances[np.maximum(kind_ids, 0)])
            diagonal = residuals(variances, slope) + noise
            precision = matrix.T @ matrix.multiply((1 / diagonal)[:, None]) + diags(1 / prior)
            # The continuous covariate can introduce roundoff asymmetry.
            precision = ((precision + precision.T) * .5).tocsc()
            precision.sort_indices()
            factor = joint._factor(precision, fill)
            rhs = matrix.T @ (response / diagonal)
            mean = factor.solve(rhs)
            loss = .5 * (response @ (response / diagonal) - rhs @ mean + np.log(diagonal).sum()
                + np.log(prior).sum() + np.log(factor.U.diagonal()).sum() + len(rows) * np.log(2 * np.pi))
            check_stopping()
            return precision, mean, float(loss)

        def objective(parameters):
            variances = np.zeros(len(names))
            variances[active] = np.exp(parameters[:-1])
            return posterior(variances, parameters[-1])[2]

        initial = [joint.INITIAL[name] if name in groups else joint.INITIAL['residual'] for name in names]
        if fixed is None and len(rows) >= 4:
            result = minimize(objective, [*np.log([initial[index] for index in active]), 0.],
                method='L-BFGS-B', jac='3-point',
                bounds=[(math.log(1e-5), math.log(.25))] * len(active) + [(-10., 10.)],
                options={'maxiter': 100, 'ftol': 1e-9, 'finite_diff_rel_step': 1e-5})
            if not result.success:
                raise WorkBudgetExceeded('Joint vibe variance fit did not converge: ' + str(result.message))
            variances = np.zeros(len(names))
            variances[active] = np.exp(result.x[:-1])
            slope = float(result.x[-1])
            diagnostics = {'method': 'marginal-maximum-likelihood', 'iterations': int(result.nit)}
        else:
            settings = fixed if fixed is not None else joint.INITIAL
            if any(not math.isfinite(settings[name]) or settings[name] <= 0 for name in joint.COMPONENTS):
                raise ValueError('Fixed variance settings must be positive and finite')
            variances = np.zeros(len(names))
            variances[active] = [settings[names[index]] if index < offset else settings['residual']
                                 for index in active]
            slope = 0.
            diagnostics = {'method': 'fixed-control' if fixed is not None else 'sparse-prior', 'iterations': 0}
        precision, mean, loss = posterior(variances, slope)
        residual = float(np.mean(residuals(variances, slope)))
    return {'format': 4, 'vibe': vibe, 'configuration': joint.configuration(), 'inputs': rows,
        'variances': {**dict(zip(groups, map(float, variances[:offset]))), 'residual': residual},
        'order': order, 'effect_mean': mean.tolist(),
        'precision': {'indptr': joint._encoded(precision.indptr, '<i4'),
            'indices': joint._encoded(precision.indices, '<i4'), 'values': joint._encoded(precision.data, '<f8')},
        'diagnostics': {**diagnostics, 'negative_log_likelihood': loss, 'recordings': len(rows),
            'accounted_memory_bytes': accounted, 'effects': len(features), 'factor_nonzeros': fill,
            'residual_by_family': dict(zip(names[offset:], map(float, variances[offset:]))),
            'support_slope': slope, 'provider_log_support_center': center,
            'uncertainty': 'conditional-on-fitted-variance-components', 'noise_identified': False,
            'residual': 'between-track-plus-unidentified-measurement-noise',
            'bias': 'shared-classifier-bias-not-identified-by-measurement-holdout'}}


def predict(posterior, artists, genre, *, album, metadata_prior):
    import numpy as np
    from scipy.optimize import brentq
    from scipy.special import expit, logit, ndtr

    if metadata_prior is None or not math.isfinite(metadata_prior) or not 0 <= metadata_prior <= 1:
        raise ValueError('A calibrated prediction requires its masked metadata prior')
    if not artists and not genre:
        return None
    vector = np.zeros(len(posterior.features))
    vector[posterior.features['baseline', 'intercept']] = 1.
    vector[posterior.features['baseline', 'metadata-prior']] = metadata_prior - .5
    query = {'artists': artists, 'genre': genre, 'album': album}
    prior = joint.BASELINE_VARIANCE * (1 + (metadata_prior - .5) ** 2)
    missing = 0.
    for kind in joint.COMPONENTS[:-2]:
        pairs = terms(query, kind)
        if not pairs:
            missing += posterior.variances[kind]
            prior += posterior.variances[kind]
        for name, weight in pairs:
            variance = posterior.variances[kind] * weight * weight
            prior += variance
            index = posterior.features.get((kind, name))
            if index is None:
                missing += variance
            else:
                vector[index] = weight
    diagnostics = posterior.value['diagnostics']
    means, variances, weights = [], [], []
    average = np.zeros(len(posterior.features))
    source_count = len({row['source'] for row in posterior.rows})
    with single_threaded():
        for (source, support, noise), count in posterior.population.items():
            check_stopping()
            conditional = vector.copy()
            absent = missing
            index = posterior.features.get(('source', source))
            if index is not None:
                conditional[index] = 1.
            elif source_count > 1:
                absent += posterior.variances['source']
            family = 'residual_audio' if source.startswith('audio_measurement/') else 'residual_provider'
            residual = diagnostics['residual_by_family'][family]
            probability = np.clip((residual - 1e-5) / (.25 - 1e-5), 1e-9, 1 - 1e-9)
            centered = math.log(support) - diagnostics['provider_log_support_center'] if family == 'residual_provider' else 0.
            residual = 1e-5 + (.25 - 1e-5) * expit(logit(probability) + diagnostics['support_slope'] * centered)
            means.append(float(.5 + conditional @ posterior.mean))
            variances.append(max(0., float(absent + conditional @ posterior.factor.solve(conditional))) + residual + noise)
            weight = count / len(posterior.rows)
            weights.append(weight)
            average += weight * conditional
        means, variances, weights = map(np.asarray, (means, variances, weights))
        source_weights = [average[index] for (kind, _), index in posterior.features.items() if kind == 'source']
        prior += posterior.variances['source'] * sum(weight ** 2 for weight in source_weights)
        mean_variance = max(0., float(missing + average @ posterior.factor.solve(average)))
        mean = float(weights @ means)
        new_track_variance = max(mean_variance, float(weights @ (variances + (means - mean) ** 2)))

    def quantile(probability):
        def residual(value):
            check_stopping()
            return float(weights @ ndtr((value - means) / np.sqrt(variances)) - probability)
        return 0. if residual(0.) >= 0 else 1. if residual(1.) <= 0 else float(brentq(residual, 0., 1., xtol=1e-12))

    known_artist = bool(posterior.artists.intersection(artists))
    known_genre = bool(genre) and genre in posterior.genres
    kind = ('artist_genre' if known_artist and known_genre else 'artist' if known_artist
            else 'genre' if known_genre else 'artist' if artists else 'genre')
    return {'score': float(np.clip(mean, 0., 1.)),
            'support': max(0., min(1., posterior.coverage * (1 - mean_variance / prior))),
            'source': kind, 'recordings': len(posterior.rows), 'mean_variance': mean_variance,
            'new_track_variance': new_track_variance, 'interval90': [quantile(.05), quantile(.95)]}
