"""Scoring against saved, independently weighted listening contexts."""
import numpy as np


class PlaylistTaste:
    """Normalized context centroids and their relative serving weights."""

    def __init__(self, titles, centroids, weights, pids=()):
        self.titles = list(titles)
        self.centroids = centroids
        self.weights = weights
        self.pids = list(pids)

    def __bool__(self):
        return len(self.titles) > 0

    def score(self, vec, top=3):
        if not self.titles:
            return 0.0, []
        v = vec / (np.linalg.norm(vec) + 1e-9)
        contributions = self.weights * (self.centroids @ v)
        order = np.argsort(-contributions)[:top]
        because = [(self.titles[i], float(contributions[i])) for i in order if contributions[i] > 0]
        return float(contributions.sum()), because

    def score_all(self, vectors):
        if not self.titles or len(vectors) == 0:
            return np.zeros(len(vectors))
        normalized = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)
        return self.weights @ (self.centroids @ normalized.T)
