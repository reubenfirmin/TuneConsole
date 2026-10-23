"""Normalized track positions used by playlist merge ordering."""
from __future__ import annotations


def track_positions(seqs: list[list]) -> dict:
    """Map each id to its average normalized position across the sequences containing it.

    A sequence of length 1 contributes position 0.0 for its single id. Ids absent from
    every sequence simply won't appear in the result.
    """
    acc: dict = {}
    for seq in seqs:
        n = len(seq)
        for i, x in enumerate(seq):
            acc.setdefault(x, []).append(i / (n - 1) if n > 1 else 0.0)
    return {x: sum(v) / len(v) for x, v in acc.items()}
