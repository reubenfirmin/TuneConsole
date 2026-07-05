"""#86 catalog scoring: novelty times a rank base, no shift-by-min pool dependence."""
from yt_playlist.rec import surfaces


def test_catalog_score_kernel_pool_independent():
    # The kernel is exercised through _catalog_scores (extracted in this task): a track's
    # novelty-x-fit ordering must not change when junk joins the pool.
    plays = {"a": 0, "b": 0}
    fit = {"a": 0.2, "b": 0.8}
    o1 = sorted(surfaces._catalog_scores(fit, plays), key=lambda k: -surfaces._catalog_scores(fit, plays)[k])
    fit_junk = dict(fit, junk=-9.0)
    plays_junk = dict(plays, junk=0)
    scored = surfaces._catalog_scores(fit_junk, plays_junk)
    o2 = [k for k in sorted(scored, key=lambda k: -scored[k]) if k != "junk"]
    assert o1 == o2


def test_missing_fit_key_gets_below_worst_floor():
    scored = surfaces._catalog_scores({"a": 0.4}, {"a": 0, "mystery": 0}, all_keys=["a", "mystery"])
    assert 0 < scored["mystery"] < scored["a"]
