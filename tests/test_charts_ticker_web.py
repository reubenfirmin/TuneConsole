"""Web-route test: the /charts page renders the four new ticker tabs (genre / year / album /
playlist) with candle rows for a seeded library + history."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.charts import _ticker_periods


def _seed(store):
    iid = store.upsert_identity("me", "c", None, True)
    a = store.upsert_track("v1", "Alpha", "Artist A", "Album A", 200)
    b = store.upsert_track("v2", "Beta", "Artist B", "Album B", 200)
    store.set_track_enrichment(a, "techno", "1995")
    store.set_track_enrichment(b, "house", "2005")
    p = store.upsert_playlist(iid, "P1", "Mix", 0, "h", 0.0)
    store.set_playlist_tracks(p, [a, b])

    def key(tid):
        return store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (tid,)).fetchone()["k"]

    # Two snapshots ~30d apart so the ticker has a real history span to slice into periods.
    store.add_history_snapshot(iid, NOW - 30 * 86400, [key(a), key(b)])
    store.add_history_snapshot(iid, NOW, [key(a), key(a), key(b)])
    return iid


NOW = 2_000_000_000.0


def test_selected_chart_range_is_the_full_latest_period():
    periods, close_days, span_days = _ticker_periods(None, NOW, 7)
    assert periods[0][1] == (NOW - 7 * 86400, None)
    assert periods[1][1] == (NOW - 14 * 86400, NOW - 7 * 86400)
    assert close_days == 7 and span_days == 28


def test_all_time_chart_uses_the_complete_history_as_its_period():
    earliest = NOW - 24 * 86400
    periods, close_days, span_days = _ticker_periods(earliest, NOW)
    assert periods == [("w0", (earliest, None))]
    assert close_days == 24 and span_days == 24


def test_charts_ticker_tabs_render(store):
    _seed(store)
    app = create_app(store, lambda: {}, now_fn=lambda: NOW)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.get("/charts")
    assert r.status_code == 200
    body = r.text
    # the four new tab buttons (match the Alpine binding so we don't catch the nav's /genres link)
    for view in ("genres", "years", "albums", "playlists"):
        assert f"view = '{view}'" in body, f"missing {view} tab button"
    assert "chart-toolbar" not in body
    assert 'class="chart-context-actions"' in body
    assert 'class="modal chart-key-popup"' in body
    assert 'class="key-window"' in body
    # category labels from each dimension show up in their tables
    assert "techno" in body          # genre
    assert "1990" in body            # year decade bucket
    assert "Album A" in body         # album
    assert "Mix" in body             # playlist
    # the candle SVG class is present (the ticker row visual)
    assert "ticker-candle" in body


def test_charts_ticker_handles_no_history(store):
    store.upsert_identity("me", "c", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.get("/charts")
    assert r.status_code == 200
    assert "Genres" in r.text          # tabs still render with empty data


def test_chart_range_controls_apply_to_and_preserve_every_view(store):
    _seed(store)
    app = create_app(store, lambda: {}, now_fn=lambda: NOW)
    body = TestClient(app, base_url="http://127.0.0.1").get(
        "/charts?window=7d&view=genres").text

    assert "view: 'genres'" in body
    assert "x-show=\"view === 'songs'\"" not in body.split('class="chart-context-actions"', 1)[1].split(">", 1)[0]
    assert "'/charts?window=30d&amp;view=' + view" in body
    assert "How to read this chart" in body


def test_charts_root_opens_top_songs(store):
    _seed(store)
    app = create_app(store, lambda: {}, now_fn=lambda: NOW)
    body = TestClient(app, base_url="http://127.0.0.1").get("/charts").text
    assert "view: 'songs'" in body
