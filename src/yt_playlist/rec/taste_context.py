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

    def allocated_scores(self, vectors):
        """Rank within each interest, then allocate slots by its serving weight.

        Unlike a linear sum of cosines this cannot turn separated interests into a midpoint.
        Shared candidates appear once. Deterministic ties preserve the candidate input order.
        """
        size = len(vectors)
        if not self.titles or not size:
            return np.zeros(size)
        normalized = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)
        similarities = self.centroids @ normalized.T
        if len(self.titles) == 1:
            # No allocation is needed for a single interest. Keep equal fits equal
            # and preserve cosine scale for the recent/session/negative overlays.
            return similarities[0]
        orders = np.argsort(-similarities, axis=1, kind="stable")
        positions = np.zeros(len(self.titles), dtype=int)
        taken = np.zeros(len(self.titles), dtype=int)
        seen, ranked = set(), []
        while len(ranked) < size:
            priorities = np.where(positions < size, self.weights / (taken + 1), -1)
            tied = np.flatnonzero(priorities == priorities.max())
            context = max(tied, key=lambda i: similarities[i, orders[i, min(positions[i], size - 1)]])
            if priorities[context] <= 0:
                break
            while positions[context] < size and int(orders[context, positions[context]]) in seen:
                positions[context] += 1
            if positions[context] == size:
                continue
            candidate = int(orders[context, positions[context]])
            positions[context] += 1
            taken[context] += 1
            seen.add(candidate)
            ranked.append(candidate)
        result = np.zeros(size)
        for rank, candidate in enumerate(ranked):
            result[candidate] = 1 - rank / size
        return result
