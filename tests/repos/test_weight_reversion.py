"""#85 learned-weight mean reversion is time-proportional: reinforced weights hold, abandoned ones
relax on the clock (the old version shrank 5% per nudge regardless of time)."""
import pytest

DAY = 86400.0


def test_nudge_persists_updated_at_and_skips_flat_shrink(store):
    s = store
    w = s.rec_model.nudge_weight("lane:deep_cut", 2.0, now=1000.0)
    assert w == pytest.approx(2.0)                    # no flat 5% pull anymore
    row = s.rec_model.weight_records()["lane:deep_cut"]
    assert row["updated_at"] == 1000.0


def test_read_time_reversion_is_lazy_and_nonpersisting(store):
    s = store
    s.rec_model.nudge_weight("lane:deep_cut", 2.0, now=1000.0)
    later = 1000.0 + 60 * DAY                         # one 60d half-life later
    assert s.rec_model.get_weights(now=later)["lane:deep_cut"] == pytest.approx(1.5)   # halfway back to 1.0
    row = s.conn.execute('SELECT weight FROM taste_axes WHERE weight IS NOT NULL').fetchone()
    assert row["weight"] == pytest.approx(2.0)        # stored value untouched by reads


def test_reinforced_weight_does_not_erode(store):
    s = store
    s.rec_model.nudge_weight("a", 2.0, now=0.0)
    for d in range(1, 11):
        s.rec_model.nudge_weight("a", 1.0, now=d * DAY)         # neutral nudges, but frequent
    assert s.rec_model.get_weights(now=10 * DAY)["a"] > 1.85    # ~2% total decay over 10 active days


def test_legacy_row_without_updated_at_reads_as_prior_age_zero(store):
    s = store
    s.rec_model.set_weight("old", 2.5, now=0.0)
    # Retained evidence can lack a timestamp; keep its typed axis identity.
    s.conn.execute('UPDATE taste_axes SET weight_updated_at=NULL WHERE weight IS NOT NULL')
    s.conn.commit()
    assert s.rec_model.get_weights(now=1e9)["old"] == pytest.approx(2.5)   # NULL updated_at: no reversion applied


def test_manual_set_weight_is_fresh_evidence(store):
    s = store
    s.rec_model.nudge_weight("a", 1.5, now=0.0)
    s.rec_model.set_weight("a", 1.8, now=90 * DAY)                 # manual override re-stamps the clock
    assert s.rec_model.get_weights(now=90 * DAY)["a"] == pytest.approx(1.8)
    assert s.rec_model.get_weights(now=150 * DAY)["a"] == pytest.approx(1.4)   # one 60d half-life after the SET


@pytest.mark.parametrize("factor", [0.5, 1.0, 2.0])
@pytest.mark.parametrize("updated_at", [None, 0.0])
def test_automatic_nudges_preserve_explicit_genre_mutes(store, factor, updated_at):
    # Legacy mutes and fresh mutes survive both elapsed-time reversion and a positive clamp floor.
    store.rec_model.set_weight("genre:jazz", 0.0, lo=0.0, now=0.0)
    if updated_at is None:
        store.conn.execute('UPDATE taste_axes SET weight_updated_at=NULL WHERE weight IS NOT NULL')
        store.conn.commit()
    for day in (1, 60, 365):
        assert store.rec_model.nudge_weight("genre:jazz", factor, now=day * DAY) == 0.0
        assert store.rec_model.nudge_weight("genre:jazz", factor, lo=0.0, hi=2.0, now=day * DAY) == 0.0
        assert store.rec_model.get_weights(now=day * DAY)["genre:jazz"] == 0.0
        assert store.rec_model.weight_records()["genre:jazz"]["weight"] == 0.0


def test_manual_unmute_allows_automatic_learning_again(store):
    store.rec_model.set_weight("genre:jazz", 0.0, lo=0.0, hi=2.0, now=0.0)
    assert store.rec_model.nudge_weight("genre:jazz", 1.1, now=DAY) == 0.0
    store.rec_model.set_weight("genre:jazz", 0.8, lo=0.0, hi=2.0, now=DAY)
    assert store.rec_model.nudge_weight("genre:jazz", 1.1, now=DAY) == pytest.approx(0.88)
    assert store.rec_model.get_weights(now=DAY)["genre:jazz"] == pytest.approx(0.88)


def test_startup_removes_historical_dislikes_even_through_subsequent_likes(store):
    axis = 'genre:jazz'
    model = store.rec_model

    def graduate(source, multiplier, day, halflife):
        weight = model.nudge_weight(axis, multiplier, lo=0., hi=2., now=day * DAY,
                                    revert_halflife_d=halflife)
        store.rec_signals.log_graduation(axis, source, 1., multiplier, weight, day * DAY)

    graduate('like', 1.05, 1, 60.)
    graduate('dislike', .95, 2, 60.)
    graduate('dislike', .95, 10, 30.)
    graduate('like', 1.05, 20, 30.)
    # The worker's dated audit records do not write the retained weight.
    store.rec_signals.log_graduation(axis, 'dated-support', 1., 1.01, 1.01, 40 * DAY)
    model.set_weight('genre:mute', 0., lo=0., now=20 * DAY)
    model.set_weight('lane:deep_cut', 1.8, now=20 * DAY)
    retained = model.weight_records()

    weights = model.baseline_weights(20 * DAY)
    clean_expected = (1. + .05 * .5 ** (1. / 60. + 18. / 30.)) * 1.05
    assert weights[axis] == pytest.approx(clean_expected)
    assert weights['genre:mute'] == 0.
    assert weights['lane:deep_cut'] == 1.8
    assert retained[axis]['weight'] < weights[axis]
    assert model.weight_records() == retained
    assert store.rec_signals.graduation_log_counts()['dislike'] == 2

    model.set_weight(axis, .8, now=21 * DAY)
    assert model.baseline_weights(21 * DAY)[axis] == .8
