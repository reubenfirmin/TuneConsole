"""#85 learned-weight mean reversion is time-proportional: reinforced weights hold, abandoned ones
relax on the clock (the old version shrank 5% per nudge regardless of time)."""
import pytest

from yt_playlist.core.store import Store

DAY = 86400.0


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def test_nudge_persists_updated_at_and_skips_flat_shrink():
    s = _store()
    w = s.nudge_weight("lane:deep_cut", 2.0, now=1000.0)
    assert w == pytest.approx(2.0)                    # no flat 5% pull anymore
    row = s.conn.execute("SELECT weight, updated_at FROM rec_weights WHERE axis='lane:deep_cut'").fetchone()
    assert row["updated_at"] == 1000.0


def test_read_time_reversion_is_lazy_and_nonpersisting():
    s = _store()
    s.nudge_weight("lane:deep_cut", 2.0, now=1000.0)
    later = 1000.0 + 60 * DAY                         # one 60d half-life later
    assert s.get_weights(now=later)["lane:deep_cut"] == pytest.approx(1.5)   # halfway back to 1.0
    row = s.conn.execute("SELECT weight FROM rec_weights").fetchone()
    assert row["weight"] == pytest.approx(2.0)        # stored value untouched by reads


def test_reinforced_weight_does_not_erode():
    s = _store()
    s.nudge_weight("a", 2.0, now=0.0)
    for d in range(1, 11):
        s.nudge_weight("a", 1.0, now=d * DAY)         # neutral nudges, but frequent
    assert s.get_weights(now=10 * DAY)["a"] > 1.85    # ~2% total decay over 10 active days


def test_legacy_row_without_updated_at_reads_as_prior_age_zero():
    s = _store()
    s.conn.execute("INSERT INTO rec_weights(axis, weight) VALUES ('old', 2.5)")
    s.conn.commit()
    assert s.get_weights(now=1e9)["old"] == pytest.approx(2.5)   # NULL updated_at: no reversion applied


def test_manual_set_weight_is_fresh_evidence():
    s = _store()
    s.nudge_weight("a", 1.5, now=0.0)
    s.set_weight("a", 1.8, now=90 * DAY)                 # manual override re-stamps the clock
    assert s.get_weights(now=90 * DAY)["a"] == pytest.approx(1.8)
    assert s.get_weights(now=150 * DAY)["a"] == pytest.approx(1.4)   # one 60d half-life after the SET
