"""Pure assembly + geometry for the charts ticker rows (no DB).

ticker_rows turns corpus + windowed listen distributions into ranked rows. The chart reads as a
ranked bar chart (bar length = recent listen share, like the Songs/Artists tabs); the corpus
baseline is a per-row tick, and the period range is a thin whisker. candle_geometry maps a row's
absolute shares onto a 0..axis_max % axis.
"""
import pytest

from yt_playlist.rec.ticker import candle_geometry, ticker_rows


def _by_cat(out):
    return {r["cat"]: r for r in out["rows"]}


def test_ratio_and_shares_vs_corpus():
    # 5% of the library is techno, but it's 10% of last-7-day listens -> 2.0x over-indexed.
    corpus = {"techno": 5, "rock": 95}
    windows = {"7d": {"techno": 10, "rock": 90}, "1y": {"techno": 7, "rock": 93}}
    r = _by_cat(ticker_rows(corpus, windows))["techno"]
    assert r["corpus_share"] == pytest.approx(0.05)
    assert r["close"] == pytest.approx(0.10)
    assert r["open"] == pytest.approx(0.07)
    assert r["ratio"] == pytest.approx(2.0)
    assert r["trend"] == "up"           # close(.10) > open(.07): heating up


def test_trend_down_when_recent_below_longrun():
    corpus = {"a": 1, "b": 1}
    windows = {"7d": {"a": 1, "b": 9}, "1y": {"a": 9, "b": 1}}   # a: close .1 < open .9
    assert _by_cat(ticker_rows(corpus, windows))["a"]["trend"] == "down"


def test_rows_sorted_by_recent_share_desc():
    corpus = {"a": 1, "b": 1, "c": 1}
    windows = {"7d": {"a": 1, "b": 2, "c": 7}}
    assert [r["cat"] for r in ticker_rows(corpus, windows)["rows"]] == ["c", "b", "a"]


def test_high_low_span_all_windows():
    corpus = {"x": 1, "y": 1}
    windows = {"7d": {"x": 9, "y": 1}, "30d": {"x": 5, "y": 5},
               "90d": {"x": 5, "y": 5}, "1y": {"x": 1, "y": 9}}
    r = _by_cat(ticker_rows(corpus, windows))["x"]
    assert r["high"] == pytest.approx(0.9)
    assert r["low"] == pytest.approx(0.1)


def test_axis_max_is_max_recent_or_corpus_share():
    # axis is the magnitude axis: it reaches the biggest recent-share or corpus tick, so bars rank.
    corpus = {"a": 1, "b": 1}          # a corpus share 0.5
    windows = {"7d": {"a": 3, "b": 1}}  # a recent share 0.75
    assert ticker_rows(corpus, windows)["axis_max"] == pytest.approx(0.75)


def test_zero_recent_share_is_zero_not_pinned():
    # a is in the corpus and was played a year ago, but 0% in the last 7d -> bar length 0,
    # NOT a full bar clamped to an edge (the 0-vs-null bug).
    corpus = {"a": 1, "b": 1}
    windows = {"7d": {"b": 1}, "1y": {"a": 1}}   # a: close 0.0, open 1.0
    out = ticker_rows(corpus, windows)
    r = _by_cat(out)["a"]
    assert r["close"] == 0.0
    g = candle_geometry(r, out["axis_max"])
    assert g["bar_pct"] == pytest.approx(0.0)


def test_open_is_none_when_no_earlier_data():
    # Young library: only the newest period has any plays. There's no "earlier" to compare to,
    # so open is None (null), distinct from 0.0, and there's no trend.
    corpus = {"a": 1, "b": 1}
    windows = {"w0": {"a": 1, "b": 1}, "w1": {}, "w2": {}}   # w1/w2 are null (no snapshots)
    r = _by_cat(ticker_rows(corpus, windows))["a"]
    assert r["open"] is None
    assert r["close"] == pytest.approx(0.5)
    assert r["high"] == pytest.approx(0.5) and r["low"] == pytest.approx(0.5)   # only w0 counts
    assert r["trend"] == "flat"


def test_open_uses_oldest_populated_period_skipping_null():
    corpus = {"a": 1, "b": 1}
    windows = {"w0": {"a": 3, "b": 1}, "w1": {}, "w2": {"a": 1, "b": 3}}   # w1 null, w2 populated
    r = _by_cat(ticker_rows(corpus, windows))["a"]
    assert r["close"] == pytest.approx(0.75)   # w0 (newest)
    assert r["open"] == pytest.approx(0.25)    # w2 (oldest populated); w1 skipped
    assert r["high"] == pytest.approx(0.75) and r["low"] == pytest.approx(0.25)
    assert r["trend"] == "up"


def test_zero_in_a_populated_period_is_real_zero_not_null():
    # 'a' was simply not played in the older period (but the period HAS data) -> open 0.0, not None.
    corpus = {"a": 1, "b": 1}
    windows = {"w0": {"a": 1}, "w1": {"b": 5}}   # w1 populated (total 5), 'a' absent there
    r = _by_cat(ticker_rows(corpus, windows))["a"]
    assert r["open"] == 0.0
    assert r["close"] == pytest.approx(1.0)


def test_zero_corpus_share_gives_null_ratio():
    r = _by_cat(ticker_rows({"known": 1}, {"7d": {"surprise": 1}}))["surprise"]
    assert r["corpus_share"] == 0.0
    assert r["ratio"] is None


def test_empty_inputs():
    out = ticker_rows({}, {"7d": {}})
    assert out["rows"] == []
    assert out["axis_max"] > 0


def test_candle_geometry_magnitude_positions():
    row = {"cat": "techno", "corpus_share": 0.05, "open": 0.07, "close": 0.10,
           "high": 0.12, "low": 0.05, "ratio": 2.0, "trend": "up"}
    g = candle_geometry(row, axis_max=0.20)
    assert g["bar_pct"] == pytest.approx(50.0)        # recent share 0.10 / 0.20
    assert g["corpus_pct"] == pytest.approx(25.0)     # corpus tick at 0.05
    assert g["open_pct"] == pytest.approx(35.0)       # earlier (1y) marker at 0.07
    assert g["whisker_lo_pct"] == pytest.approx(25.0) # range low 0.05
    assert g["whisker_hi_pct"] == pytest.approx(60.0) # range high 0.12
    assert g["over"] is True                          # close >= corpus: over-indexed
    assert g["trend"] == "up"


def test_candle_geometry_clamps_and_guards_zero_axis():
    g0 = candle_geometry({"corpus_share": 0.0, "open": 0.0, "close": 0.0, "high": 0.0,
                          "low": 0.0, "ratio": None, "trend": "flat"}, axis_max=0.0)
    assert g0["bar_pct"] == 0.0 and g0["corpus_pct"] == 0.0
    g1 = candle_geometry({"corpus_share": 0.5, "open": 0.0, "close": 0.3, "high": 0.9,
                          "low": 0.0, "ratio": 0.6, "trend": "up"}, axis_max=0.3)
    assert g1["whisker_hi_pct"] == pytest.approx(100.0)   # 0.9 clamped to the edge
    assert g1["over"] is False                            # close 0.3 < corpus 0.5
