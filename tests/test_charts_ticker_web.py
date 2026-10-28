"""Web-route test: the /charts page renders the four new ticker tabs (genre / year / album /
playlist) with candle rows for a seeded library + history."""
from fastapi.testclient import TestClient
import re
import pytest

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.charts import _build_ticker, _ticker_periods


def _seed(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    a = store.tracks.upsert_track("v1", "Alpha", "Artist A", "Album A", 200, album_browse_id="album-a")
    b = store.tracks.upsert_track("v2", "Beta", "Artist B", "Album B", 200, album_browse_id="album-b")
    store.tracks.set_track_enrichment(a, "techno", "1995")
    store.tracks.set_track_enrichment(b, "house", "2005")
    p = store.playlists.upsert_playlist(iid, "P1", "Mix", 0, "h", 0.0)
    store.playlists.set_playlist_tracks(p, [a, b])

    def key(tid):
        return store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (tid,)).fetchone()["k"]

    # Two snapshots ~30d apart so the ticker has a real history span to slice into periods.
    store.history.record_history_plays(iid, NOW - 30 * 86400, [key(a), key(b)], source_timezone="UTC")
    store.history.record_history_plays(iid, NOW, [key(a), key(a), key(b)], source_timezone="UTC")
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
    # Each ranking has a navigation button and its own table. Selection goes
    # through choose() so prepared listening views can load rankings on demand.
    for view, dimension in (("genres", "genre"), ("years", "year"),
                            ("albums", "album"), ("playlists", "playlist")):
        assert f'@click="choose(\'{view}\')"' in body, f"missing {view} tab button"
        assert f'data-dimension="{dimension}"' in body
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
    store.identities.upsert_identity("me", "c", None, True)
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


@pytest.mark.parametrize('other_account', [False, True])
def test_same_title_playlists_keep_counts_history_artwork_and_navigation(store, other_account):
    first = store.identities.upsert_identity('main', 'c', None, True)
    second = store.identities.upsert_identity('other', 'd', None, False) if other_account else first
    a = store.tracks.upsert_track('v1', 'Old song', 'Artist', '', 180)
    b = store.tracks.upsert_track('v2', 'Recent song', 'Artist', '', 180, thumbnail='/recent.png')
    extra = store.tracks.upsert_track('v3', 'Unplayed', 'Artist', '', 180)
    old = store.playlists.upsert_playlist(first, 'P1', 'Mix', 2, 'h', NOW, thumbnail='/old.png')
    recent = store.playlists.upsert_playlist(second, 'P2', 'Mix', 1, 'h', NOW)
    # Repeated membership does not multiply a song's contribution to a playlist.
    store.playlists.set_playlist_tracks(old, [a, a, extra])
    store.playlists.set_playlist_tracks(recent, [b])
    ka = store.tracks.identity_key_for_video('v1')
    kb = store.tracks.identity_key_for_video('v2')
    store.history.record_history_plays(first, NOW - 21 * 86400, [ka], source_timezone='UTC')
    store.history.record_history_plays(second, NOW - 86400, [kb], source_timezone='UTC')
    old_key, recent_key = f'@playlist:{old}', f'@playlist:{recent}'
    assert store.charts.corpus_distribution('playlist') == {old_key: 2, recent_key: 1}
    assert store.charts.listen_distribution('playlist') == {old_key: 1, recent_key: 1}
    assert store.charts.listen_distribution('playlist', since=NOW - 7 * 86400) == {recent_key: 1}
    assert store.charts.listen_distribution('playlist', until=NOW - 7 * 86400) == {old_key: 1}

    periods, _, _ = _ticker_periods(NOW - 21 * 86400, NOW, 7)
    rows = {r['cat']: r for r in _build_ticker(store, 'playlist', periods)['rows']}
    assert set(rows) == {old_key, recent_key}
    assert (rows[old_key]['open'], rows[old_key]['close']) == (1, 0)
    assert (rows[recent_key]['open'], rows[recent_key]['close']) == (0, 1)
    assert rows[old_key]['corpus_share'] == 2 / 3
    assert rows[recent_key]['corpus_share'] == 1 / 3

    app = create_app(store, lambda: {}, now_fn=lambda: NOW)
    body = TestClient(app, base_url='http://127.0.0.1').get('/charts?window=7d&view=playlists').text
    table = re.search(r'<table[^>]*data-dimension="playlist".*?</table>', body, re.S).group()
    rendered = {re.search(r'<a[^>]*href="([^"]+)"', row)[1]: row
                for row in re.findall(r'<tr>.*?</tr>', table, re.S) if '<a ' in row}
    assert set(rendered) == {f'/playlist/{old}', f'/playlist/{recent}'}
    for pid, artwork in [(old, '/old.png'), (recent, '/recent.png')]:
        row = rendered[f'/playlist/{pid}']
        assert f'href="/playlist/{pid}">Mix</a>' in row
        assert f'src="{artwork}"' in row
    assert '@playlist:' not in table
