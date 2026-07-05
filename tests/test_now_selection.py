"""#88 The NOW layer's read (layers.now_mode_posterior) boosts select_modes' dominant draw."""
from yt_playlist.core.store import Store
from yt_playlist.rec.mode_surfaces import select_modes


def _mode(mid, size=50, fams=()):
    return {"mode_id": mid, "size": size, "families": list(fams),
            "centroid": [1.0 if i == mid else 0.1 for i in range(4)]}


def test_now_posterior_makes_that_mode_dominant_in_a_clear_majority():
    # A carries the full posterior share (1.0), two equal-sized, equal-stats rivals carry none. At the
    # default now_gain (0.6) that's a 1.6x boost on A's sampled score vs a bare 1.0x on the rivals. With
    # no bandit stats every mode samples Beta(1,1) (uniform(0,1)), so this reduces to
    # P(1.6*U_A > max(U_1, U_2)) for iid U ~ Uniform(0,1), which integrates to ~0.583 analytically.
    # Simulated directly against this code (200 epochs, several seed offsets) landed 0.555-0.61, so
    # share0 > 0.5 is a clear, comfortably-margined majority rather than a coin flip.
    s = Store(":memory:")
    s.init_schema()
    modes = [_mode(0), _mode(1), _mode(2)]
    posterior = {0: 1.0}
    dominants = [select_modes(s, modes, {}, epoch, n=3, now_posterior=posterior)[0]
                 for epoch in range(200)]
    share0 = dominants.count(0) / len(dominants)
    assert share0 > 0.5


def test_none_posterior_reproduces_pre_88_behavior_byte_for_byte():
    # No now_posterior kwarg at all vs an explicit now_posterior=None must select identically, across
    # several epochs, several mode/stat configurations, and crucially with store=None (existing callers
    # pass None for store when they have no now_posterior; that path must never touch rec_params, so it
    # must never touch store either).
    modes = [_mode(0), _mode(1), _mode(2)]
    stats = {0: (2, 20), 1: (0, 20), 2: (0, 20)}
    for epoch in range(50):
        assert (select_modes(None, modes, {}, epoch, n=3, stats=stats)
                == select_modes(None, modes, {}, epoch, n=3, stats=stats, now_posterior=None))
    for epoch in range(50):
        assert (select_modes(None, modes, {}, epoch, n=3)
                == select_modes(None, modes, {}, epoch, n=3, now_posterior=None))


def test_mode_absent_from_posterior_gets_boost_one():
    # A mode missing from the posterior dict and a mode explicitly present at share 0.0 must behave
    # identically (both land on now_boost = 1.0 + now_gain * 0.0 = 1.0): dict.get's default IS the
    # "no evidence, no boost" case, not a special-cased absence.
    s = Store(":memory:")
    s.init_schema()
    modes = [_mode(0), _mode(1), _mode(2)]
    posterior_absent = {0: 1.0}
    posterior_explicit_zero = {0: 1.0, 1: 0.0, 2: 0.0}
    for epoch in range(50):
        assert (select_modes(s, modes, {}, epoch, n=3, now_posterior=posterior_absent)
                == select_modes(s, modes, {}, epoch, n=3, now_posterior=posterior_explicit_zero))


def test_empty_posterior_dict_is_falsy_like_none():
    # An empty dict is falsy, so `if now_posterior:` treats {} the same as None: no store access, no
    # boost anywhere. Guards against a caller passing {} instead of None when nothing was played.
    modes = [_mode(0), _mode(1), _mode(2)]
    for epoch in range(50):
        assert (select_modes(None, modes, {}, epoch, n=3)
                == select_modes(None, modes, {}, epoch, n=3, now_posterior={}))
