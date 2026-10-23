"""Visit every ordered co-member pair without storing pairs or an epoch-sized permutation."""
import numpy as np

from yt_playlist.core.activity import check_stopping


def _shuffled_indices(count, rng, batch_size):
    """A seeded bijection of [0, count), evaluated one batch at a time.

    Odd multiplication, addition and xor-shifts are each invertible on a power-of-two
    domain. Cycle walking restricts their composition to [0, count): repeatedly apply
    it to out-of-range values. Every index appears exactly once, without sampling or
    an in-memory Fisher-Yates permutation. This is a training shuffle, not cryptography.
    """
    bits = max(1, (count - 1).bit_length())
    mask = np.uint64((1 << bits) - 1)
    shift = max(1, bits // 2)
    parameters = rng.integers(0, np.iinfo(np.uint64).max, size=(3, 2), dtype=np.uint64)
    parameters[:, 0] |= np.uint64(1)

    def permute(values):
        for multiplier, offset in parameters:
            values = ((values ^ (values >> shift)) * multiplier + offset) & mask
        return values

    for start in range(0, count, batch_size):
        check_stopping()
        values = permute(np.arange(start, min(start + batch_size, count), dtype=np.uint64))
        outside = values >= count
        while outside.any():
            check_stopping()
            values[outside] = permute(values[outside])
            outside = values >= count
        yield values.astype(np.int64)


class PairStream:
    """Store basket memberships and pair offsets; decode only the current training batch.

    Pair order changes from the former full-array shuffle, but the objective, pair
    multiplicities and negative-sampling frequencies remain unchanged. Rebuilds with
    the same baskets and seed remain deterministic.
    """
    def __init__(self, baskets, keys):
        index = {key: i for i, key in enumerate(keys)}
        members, offsets, sizes, ends = [], [], [], []
        self.count = 0
        self.context_counts = np.zeros(len(keys), dtype=np.float64)
        for basket in baskets:
            check_stopping()
            ids = [index[key] for key in dict.fromkeys(basket)]
            size = len(ids)
            if size < 2:
                continue
            offsets.append(len(members))
            members.extend(ids)
            sizes.append(size)
            self.count += size * (size - 1)
            ends.append(self.count)
            self.context_counts[ids] += size - 1
        self.members = np.asarray(members, dtype=np.int64)
        self.offsets = np.asarray(offsets, dtype=np.int64)
        self.sizes = np.asarray(sizes, dtype=np.int64)
        self.ends = np.asarray(ends, dtype=np.int64)

    def batches(self, rng, batch_size):
        for indices in _shuffled_indices(self.count, rng, batch_size):
            basket = np.searchsorted(self.ends, indices, side="right")
            local = indices - (self.ends[basket] - self.sizes[basket] * (self.sizes[basket] - 1))
            width = self.sizes[basket] - 1
            a, b = local // width, local % width
            b += b >= a                        # skip the diagonal without storing its pairs
            yield self.members[self.offsets[basket] + a], self.members[self.offsets[basket] + b]
