"""Wall-clock decay and the bounded facet multiplier, without input acquisition."""


def decay_weight(age_s, half_life_d):
    return 1.0 if age_s <= 0 else 0.5 ** (age_s / (half_life_d * 86400.0))


def facet_multiplier(lean, gain, lo, hi):
    return max(lo, min(hi, 1.0 + gain * lean))
