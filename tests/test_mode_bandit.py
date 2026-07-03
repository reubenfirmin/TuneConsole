"""#87 Thompson sampling over mode pick-through: the posterior sample IS the rotation."""
import random

from yt_playlist.core.store import Store
from yt_playlist.rec.mode_eval import mode_bandit_stats
from yt_playlist.rec.mode_surfaces import thompson_mode_scores


def test_samples_deterministic_per_seed_and_in_range():
    stats = {1: (5, 20), 2: (0, 20)}
    a = thompson_mode_scores(stats, [1, 2, 3], random.Random(42))
    b = thompson_mode_scores(stats, [1, 2, 3], random.Random(42))
    assert a == b
    assert set(a) == {1, 2, 3}
    assert all(0.0 < v < 1.0 for v in a.values())


def test_picked_mode_outsamples_ignored_mode_on_average():
    # Mode 1: 10/20 picked. Mode 2: 0/20 offered, never picked. Over many seeds the
    # posterior for mode 1 must dominate: this is the bandit actually using acceptance.
    stats = {1: (10, 20), 2: (0, 20)}
    wins = sum(1 for seed in range(400)
               if thompson_mode_scores(stats, [1, 2], random.Random(seed))[1]
               > thompson_mode_scores(stats, [1, 2], random.Random(seed))[2])
    assert wins > 360                                    # ~Beta(11,11) vs Beta(1,21)


def test_unknown_mode_gets_wide_uniform_prior():
    # A brand-new mode samples Beta(1,1): sometimes high, sometimes low: it gets explored.
    vals = [thompson_mode_scores({}, [9], random.Random(s))[9] for s in range(200)]
    assert min(vals) < 0.2 and max(vals) > 0.8


def test_mode_bandit_stats_counts():
    s = Store(":memory:")
    s.init_schema()
    s.modes.log_impressions(100, [(0, 1), (1, 1)], 100.0)
    s.modes.log_impressions(101, [(0, 2)], 101.0)
    s.modes.log_pick(77, 1, 102.0)
    assert mode_bandit_stats(s) == {1: (1, 2), 2: (0, 1)}


def _mode(mid, size=50, fams=()):
    return {"mode_id": mid, "size": size, "families": list(fams),
            "centroid": [1.0 if i == mid else 0.1 for i in range(4)]}


def test_select_modes_dominant_learns_from_picks():
    from yt_playlist.rec.mode_surfaces import select_modes
    modes = [_mode(0), _mode(1), _mode(2)]
    # mode 0 gets picked, 1 and 2 ignored. picks=2/20 (not 15/20): Beta(16,6) vs Beta(1,21) is so
    # lopsided (P(ignored sample beats it) ~1e-11) that an ignored mode winning even once across 200
    # fixed epochs is statistically indistinguishable from impossible (confirmed 0/200000 by Monte
    # Carlo) -- that would make the "still explored sometimes" assertion below unfalsifiable-false
    # rather than a real check. Beta(3,19) vs Beta(1,21) keeps the dominance/explore-tension real.
    stats = {0: (2, 20), 1: (0, 20), 2: (0, 20)}
    dominants = [select_modes(None, modes, {}, epoch, n=3, stats=stats)[0]
                 for epoch in range(200)]
    share0 = dominants.count(0) / len(dominants)
    share1 = dominants.count(1) / len(dominants)
    assert share0 > 0.6                                  # acceptance dominates...
    assert share1 > 0.0                                  # ...but ignored modes still explored sometimes


def test_select_modes_no_stats_matches_size_prior_in_tendency():
    from yt_playlist.rec.mode_surfaces import select_modes
    modes = [_mode(0, size=200), _mode(1, size=10), _mode(2, size=10)]
    dominants = [select_modes(None, modes, {}, epoch, n=3)[0] for epoch in range(200)]
    assert dominants.count(0) / len(dominants) > 0.5     # big mode still rolls dominant most often


def test_select_modes_deterministic_per_epoch():
    from yt_playlist.rec.mode_surfaces import select_modes
    modes = [_mode(0), _mode(1)]
    assert select_modes(None, modes, {}, 7, n=2) == select_modes(None, modes, {}, 7, n=2)
