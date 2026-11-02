"""Count likelihoods and train-only selection for #711; no product imports."""
from collections import Counter
import re

import numpy as np
from scipy.optimize import minimize
from scipy.special import betaln, digamma, expit, gammaln, logit

from . import individual_protocol as protocol
from .external_training import standardize
from .focus_mapping import split

ALPHAS = (1., 10., 100., 1000.)
KAPPAS = (1., 4., 19.)
TARGETS = ('Usage-Reading', 'Usage-Studying')


def reconstruct(rows, base, ref):
    yes, totals = np.zeros((len(rows), 2), dtype=int), np.zeros((len(rows), 2), dtype=int)
    uids, missing, unchecked, files, counts = Counter(), Counter(), Counter(), [], []
    for i, row in enumerate(rows):
        paths = sorted((base / 'annotations').glob(row['id'].removeprefix('cal500:') + '_[0-9][0-9].txt'))
        counts.append(len(paths))
        for path in paths:
            fields = dict(re.findall(r'^([^=]+?) = "([^"]*)"', ref(path).read_text(), re.M))
            uid = fields.get('uid')
            if uid:
                uids[uid] += 1
            files.append(str(path))
            for j, name in enumerate(TARGETS):
                value = fields.get(name)
                omitted_checkbox = value is None and fields.get('PageStatus') == 'Annotation'
                if value in ('Yes', 'No') or omitted_checkbox:
                    totals[i, j] += 1
                    yes[i, j] += value == 'Yes'
                    unchecked[name] += omitted_checkbox
                else:
                    missing[name] += 1
    if np.any(totals == 0):
        raise ValueError('Unsupported activity row: no affirmative/negative responses')
    return yes, totals, {'files': len(files), 'response_support': dict(Counter(counts)),
        'missing_activity_values': dict(missing), 'omitted_unselected_checkboxes': dict(unchecked),
        'encoding': 'Preserve the established CAL500 raw checkbox task: omitted Usage in an Annotation response is unselected, explicit unknown values remain missing.',
        'identified_responses': sum(uids.values()),
        'distinct_nonempty_uids': len(uids), 'uid_response_counts': dict(uids),
        'independence': 'Response counts are not independently identified people. Unknown/repeated listeners remain unresolved; beta-binomial only permits within-song overdispersion.'}


def nll(k, n, probability, kappa=None):
    p = np.clip(probability, 1e-9, 1 - 1e-9)
    choose = gammaln(n + 1) - gammaln(k + 1) - gammaln(n - k + 1)
    if kappa is None:
        return -(choose + k * np.log(p) + (n - k) * np.log1p(-p))
    a, b = p * kappa, (1 - p) * kappa
    return -(choose + betaln(k + a, n - k + b) - betaln(a, b))


def objective(theta, z, k, n, alpha, kappa):
    eta = z @ theta[:-1] + theta[-1]
    raw = expit(eta)
    p = np.clip(raw, 1e-9, 1 - 1e-9)
    if kappa is None:
        derivative = n * p - k
    else:
        a, b = p * kappa, (1 - p) * kappa
        derivative = -kappa * (digamma(k + a) - digamma(n - k + b) - digamma(a) + digamma(b)) * p * (1 - p)
    derivative = np.where((raw > 1e-9) & (raw < 1 - 1e-9), derivative, 0.)
    loss = nll(k, n, p, kappa).sum() + .5 * alpha * np.dot(theta[:-1], theta[:-1])
    gradient = np.r_[z.T @ derivative + alpha * theta[:-1], derivative.sum()]
    return float(loss), gradient


def fit(x, yes, total, alpha, kappa=None):
    z, medians, mean, scale = standardize(x)
    heads, diagnostics = [], []
    for col in range(2):
        k, n = yes[:, col], total[:, col]
        initial = np.r_[np.zeros(x.shape[1]), logit((k.sum() + .5) / (n.sum() + 1))]
        result = minimize(objective, initial, args=(z, k, n, alpha, kappa), method='L-BFGS-B', jac=True,
                          options={'maxiter': 400, 'ftol': 1e-10, 'gtol': 1e-6, 'maxls': 40})
        if not result.success or not np.isfinite(result.fun):
            raise ValueError('Count fit failed: ' + str(result.message))
        heads.append(result.x)
        diagnostics.append({'iterations': int(result.nit), 'objective': float(result.fun),
                            'gradient_max': float(np.max(np.abs(result.jac))), 'message': str(result.message)})
    return {'kind': 'count_logit', 'weights': np.asarray(heads)[:, :-1].T.tolist(),
            'intercept': np.asarray(heads)[:, -1].tolist(), 'medians': medians.tolist(),
            'mean': mean.tolist(), 'scale': scale.tolist(), 'alpha': alpha, 'kappa': kappa}, diagnostics


def predict(model, x):
    z = (np.where(np.isfinite(x), x, model['medians']) - model['mean']) / model['scale']
    return expit(z @ np.asarray(model['weights']) + model['intercept'])


def select(x, yes, total, groups, family):
    assignment = split(groups)
    candidates = [(a, k) for k in ((None,) if family == 'binomial' else KAPPAS) for a in ALPHAS]
    losses = np.empty((len(x), len(candidates), 2))
    trace = {'training_groups': groups.tolist(), 'inner_folds': assignment.tolist(), 'fits': []}
    for fold in sorted(set(assignment)):
        train, valid = assignment != fold, assignment == fold
        for index, (alpha, kappa) in enumerate(candidates):
            model, diagnostics = fit(x[train], yes[train], total[train], alpha, kappa)
            losses[valid, index] = nll(yes[valid], total[valid], predict(model, x[valid]), kappa)
            trace['fits'].append({'fold': int(fold), 'candidate': index, 'training_groups': groups[train].tolist(),
                'validation_groups': groups[valid].tolist(), 'diagnostics': diagnostics})
    chosen = losses.mean(axis=0).argmin(axis=0)
    full, final = {}, []
    for index in sorted(set(chosen)):
        full[int(index)], diagnostic = fit(x, yes, total, *candidates[index])
        final.append({'candidate': int(index), 'diagnostics': diagnostic})
    result = {**full[int(chosen[0])],
        'weights': np.column_stack([np.asarray(full[int(i)]['weights'])[:, col] for col, i in enumerate(chosen)]).tolist(),
        'intercept': [full[int(i)]['intercept'][col] for col, i in enumerate(chosen)],
        'selected_alpha': [candidates[i][0] for i in chosen], 'selected_kappa': [candidates[i][1] for i in chosen]}
    trace.update(candidates=candidates, mean_native_nll=losses.mean(0).tolist(), selected=chosen.tolist(), final=final)
    return result, trace
