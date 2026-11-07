"""Exact marginal-likelihood derivatives with bounded inverse-column batches."""
import numpy as np

from yt_playlist.core.activity import check_stopping

BLOCK = 64


def working_bytes(observations, effects):
    # RHS, solved columns, sparse-product output and its multiplication
    # workspace; diagonals, residual derivatives and gradient scratch.
    return (2 * effects * BLOCK + 2 * observations * BLOCK
            + 12 * observations + 4 * effects) * 8


def inverse_diagonals(design, factor):
    """diag(A^-1) and diag(Z A^-1 Z.T), without either dense square matrix."""
    observations, effects = design.shape
    diagonal, leverage = np.empty(effects), np.zeros(observations)
    for start in range(0, effects, BLOCK):
        check_stopping()
        stop = min(effects, start + BLOCK)
        rhs = np.zeros((effects, stop - start))
        rhs[np.arange(start, stop), np.arange(stop - start)] = 1.
        columns = factor.solve(rhs)
        diagonal[start:stop] = columns[np.arange(start, stop), np.arange(stop - start)]
        leverage += np.asarray(design[:, start:stop].multiply(design @ columns).sum(axis=1)).ravel()
    return diagonal, leverage


def gradient(design, factor, mean, response, diagonal, kind_ids, variances,
             families, supports, residuals):
    """Derivatives in log variance components and the support slope.

    Group effects have independent N(0, variance) priors. The observation
    diagonal is a bounded logistic transform of source-family log variance
    and centered log support. The fixed baseline prior is not optimized.
    """
    covariance, leverage = inverse_diagonals(design, factor)
    groups = len(variances) - 2
    result = np.zeros(len(variances) + 1)
    for kind in range(groups):
        selected = kind_ids == kind
        if np.any(selected):
            result[kind] = .5 * (selected.sum()
                - np.sum(covariance[selected] + mean[selected] ** 2) / variances[kind])
    residual = response - design @ mean
    diagonal_gradient = .5 * (1 / diagonal - leverage / diagonal ** 2 - (residual / diagonal) ** 2)
    width = .25 - 1e-5
    probability = np.clip((variances[families] - 1e-5) / width, 1e-9, 1 - 1e-9)
    fitted = (residuals - 1e-5) / width
    derivative = width * fitted * (1 - fitted)
    for family in (groups, groups + 1):
        selected = families == family
        if np.any(selected):
            scale = variances[family] / (width * probability[selected] * (1 - probability[selected]))
            result[family] = np.sum(diagonal_gradient[selected] * derivative[selected] * scale)
    result[-1] = np.sum(diagonal_gradient * derivative * supports)
    return result
