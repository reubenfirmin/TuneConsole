"""Order-preserving merge of several playlists.

When merging N playlists, "playlist order" should read naturally: each track sits
roughly where it lives in the playlists that contain it, and tracks shared between
playlists land at their *average* spot — so unique tracks weave around the shared
anchors instead of being shunted into a block.

We use each track's average **normalized position** (0 = first, 1 = last) across the
playlists it appears in. This weaves shared and unique tracks together by where they
actually sit, which a strict topological merge can't do when one playlist keeps all
its unique tracks in a contiguous block.
"""
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


def merge_order(seqs: list[list]) -> list:
    """Return the ids merged into one list, ascending by average normalized position.

    Ties (e.g. two tracks that both sit at the very start of their playlists) are broken
    by first-encounter order, so the result is deterministic.
    """
    pos = track_positions(seqs)
    rank = {}
    for seq in seqs:
        for x in seq:
            rank.setdefault(x, len(rank))
    return sorted(pos, key=lambda x: (pos[x], rank[x]))
