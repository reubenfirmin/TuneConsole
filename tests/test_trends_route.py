import re

import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.rec import trend_rollups as tr
from yt_playlist.util import genre_map
from tests.conftest import FakeClient


@pytest.fixture(autouse=True)
def toy_genres(monkeypatch):
    monkeypatch.setattr(genre_map, "family", lambda g: (g or "").lower())
    monkeypatch.setattr(genre_map, "family_distance", lambda a, b: 0.0 if a == b else 0.5)


def _snap(store, day, keys):
    """One history snapshot at taken_at = day*86400 containing `keys`. Returns snapshot id."""
    cur = store.conn.execute("INSERT INTO history_snapshots(identity_id, taken_at) VALUES (1, ?)",
                             (day * 86400.0,))
    sid = cur.lastrowid
    for k in keys:
        store.conn.execute("INSERT INTO history_items(snapshot_id, identity_key) VALUES (?, ?)", (sid, k))
    store.conn.commit()
    return sid


def _track(store, key, artist, genre=""):
    store.conn.execute(
        "INSERT INTO tracks(identity_key, video_id, title, artist, genre) VALUES (?,?,?,?,?)",
        (key, "v" + key, "T" + key, artist, genre))
    store.conn.commit()


@pytest.fixture
def client():
    from yt_playlist.web.app import create_app
    store = Store(":memory:")
    store.init_schema()
    iid = store.upsert_identity("main", "cred", None, True)
    _ = iid
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_trends_empty_state(client):
    c, _store = client
    r = c.get("/trends")
    assert r.status_code == 200
    assert "Trends appear once" in r.text          # fail-open empty state, no rollup yet


def test_trends_nav_link_present(client):
    c, _store = client
    r = c.get("/trends")
    assert r.status_code == 200
    assert 'href="/trends"' in r.text


def test_trends_renders_after_build(client):
    c, store = client
    _track(store, "k1", "A1", "house")
    # Two genuine weeks straddling the week-0/week-1 boundary (hand-verified: week_start = (day//7)*7,
    # so days 2 and 5 both bucket to week 0, and day 9 buckets to week 7). A single-week fixture would
    # make area_spark's path a no-op (it needs >= 2 points), so this exercises the real polyline.
    _snap(store, 2, ["k1"])
    _snap(store, 5, ["k1"])
    _snap(store, 9, ["k1"])
    tr.build(store, now=10 * 86400.0)
    r = c.get("/trends")
    assert r.status_code == 200 and 'id="listening"' in r.text and "spark-area" in r.text
    m = re.search(r'<path d="([^"]*)" class="spark-area"', r.text)
    assert m is not None and m.group(1).strip(), "expected a non-empty area path for a 2-week fixture"
    assert " L" in m.group(1), "expected a real multi-point polyline, not a degenerate path"
    # lazy fragments respond 200 on their own, with and without rollups
    assert c.get("/trends/discovery").status_code == 200
    assert c.get("/trends/diversity").status_code == 200
    assert c.get("/trends/review").status_code == 200
    assert c.get("/trends/health").status_code == 200
    # #3: the page's own container class carries the card rhythm, not a global rule
    assert 'class="trends-page"' in r.text
    # #1: the one-time honest footnote lives at the page level, and jargon is gone from the top card
    assert "Measured from your synced listening history" in r.text
    assert "sync-history" not in r.text.lower()


def test_trends_listening_shows_family_color_legend(client):
    """#2: the listening-over-time chart gets a compact legend row (chips + names + "total"),
    reusing the exact same fam-N/total color classes the chart itself draws with."""
    c, store = client
    _track(store, "k1", "A1", "house")
    _track(store, "k2", "A2", "techno")
    _snap(store, 2, ["k1", "k2"])
    _snap(store, 5, ["k1"])
    _snap(store, 9, ["k1", "k2"])
    tr.build(store, now=10 * 86400.0)
    r = c.get("/trends")
    assert r.status_code == 200
    assert 'class="chart-legend"' in r.text
    assert 'class="chip-dot fam-0"' in r.text
    assert 'class="chip-dot chip-total"' in r.text
    assert "Total" in r.text
    assert "house" in r.text and "techno" in r.text


def test_trends_single_week_shows_stat(client):
    """A single-week fixture (no second bucket) should show the one week's play count as a stat, not
    just the 'not enough weeks' empty message (spec: '#76 shows the single available bucket as a
    stat, not a degenerate line'). L5: the old negative assertion checked for "...yet to chart" text
    the template never emits ("history" vs the template's actual "listening"), so it always trivially
    passed regardless of which branch rendered; assert the template's REAL empty-state string is absent
    instead, so a regression back to the degenerate-line branch would actually fail this test."""
    c, store = client
    _track(store, "k1", "A1", "house")
    _snap(store, 2, ["k1"])
    _snap(store, 5, ["k1"])   # both bucket to week 0: (2//7)*7 == (5//7)*7 == 0
    tr.build(store, now=6 * 86400.0)
    r = c.get("/trends")
    assert r.status_code == 200
    assert 'class="stat-grid"' in r.text
    assert "Not enough weeks of listening yet to chart" not in r.text
    assert "tracks heard this week" in r.text
    assert ">2<" in r.text   # the single week's play count (day 2 + day 5, one play each)


def test_trends_fragments_empty_state_without_rollup():
    from yt_playlist.web.app import create_app
    store = Store(":memory:")
    store.init_schema()
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    for path, phrase in (
        ("/trends/discovery", "Discovery rate appears"),
        ("/trends/diversity", "Diversity trends appear"),
        ("/trends/review", "Your month in review appears"),
        ("/trends/health", "Library health appears"),
    ):
        r = c.get(path)
        assert r.status_code == 200
        assert phrase in r.text


def test_trends_tolerates_pre_rebuild_payload_missing_new_fields():
    """T10e/f/g degraded state: a rollup persisted by OLD code (pre-T10d) has no `insights` key at
    all, and a `review`/`health` shape missing play_days/top_artists/top_track/binge/rediscover/
    unopened_albums. Every new element must degrade silently (Jinja's lenient Undefined on the
    missing dict keys), not 500."""
    from yt_playlist.web.app import create_app
    store = Store(":memory:")
    store.init_schema()
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    old_payload = {
        "built_at": 1000.0, "first_play_floor_day": 0,
        "weeks": [{"week_start_day": 0, "plays": 3, "distinct_artists": 1, "new_artist_plays": 0,
                   "new_track_plays": 0, "families": {}, "diversity": 0.0}],
        "months": [],
        "review": {"month": "1970-01", "plays": 3, "listen_days": 2, "longest_streak": 2,
                   "top_new_artist": None, "riser": None, "faller": None},
        "health": {"total_tracks": 5, "never_played": 1, "never_played_share": 0.2,
                   "staleness": [{"bucket": "played <30d", "n": 4}, {"bucket": "never", "n": 1}],
                   "dead_playlists": []},
        "spotlight": None,
    }
    store.put_proposals("trend_rollups", old_payload, 1000.0)
    for path in ("/trends", "/trends/review", "/trends/health"):
        r = c.get(path)
        assert r.status_code == 200
    assert 'id="insights"' not in c.get("/trends").text          # no `insights` key at all -> omitted
    assert "review-calendar" not in c.get("/trends/review").text  # no play_days -> calendar omitted
    assert "artist-podium" not in c.get("/trends/review").text    # no top_artists -> podium omitted
    assert "rediscover-list" not in c.get("/trends/health").text  # no rediscover key -> list omitted
    assert "unopened-albums" not in c.get("/trends/health").text  # no unopened_albums key -> omitted


_playlist_seq = iter(range(1, 10_000))


def _playlist(store, title, track_keys=()):
    """A playlist with the given title, optionally containing tracks (by identity_key). Tracks with no
    history_items ever remain "no listens" -- exactly the dead_playlists() shape. ytm_playlist_id is a
    synthetic sequence, independent of title, since two playlists can legitimately share a title."""
    ytm_id = f"ytm{next(_playlist_seq)}"
    cur = store.conn.execute(
        "INSERT INTO playlists(identity_id, ytm_playlist_id, title, track_count) VALUES (1, ?, ?, ?)",
        (ytm_id, title, len(track_keys)))
    pid = cur.lastrowid
    for i, key in enumerate(track_keys):
        row = store.conn.execute("SELECT id FROM tracks WHERE identity_key = ?", (key,)).fetchone()
        store.conn.execute("INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?, ?, ?)",
                           (pid, row["id"], i))
    store.conn.commit()
    return pid


def test_trends_health_dedupes_and_caps_dead_playlists(client):
    """#80 fix: dead-weight playlists must be deduped by title (a title can legitimately repeat, e.g. a
    re-imported or re-synced playlist) and capped so the card can't become a 17-item bullet dump; the
    template links the overflow out to /cleanup instead of dumping every row."""
    c, store = client
    for i in range(10):
        _track(store, f"k{i}", f"A{i}")
    dupe_pid = _playlist(store, "Dupe Playlist", [f"k{0}"])
    _playlist(store, "Dupe Playlist", [f"k{1}"])   # same title, second (never-listened) playlist
    for i in range(2, 10):
        _playlist(store, f"Solo {i}", [f"k{i}"])   # 8 more distinct titles -> 9 distinct total, 1 overflow
    tr.build(store, now=1000.0)
    r = c.get("/trends/health")
    assert r.status_code == 200
    assert r.text.count("Dupe Playlist") == 1                 # deduped, not shown twice
    assert "and 1 more" in r.text                              # 9 distinct titles, capped at 8 -> 1 overflow
    assert 'href="/cleanup"' in r.text
    # #6: each dead-weight chip links to its own playlist detail page
    assert f'href="/playlist/{dupe_pid}"' in r.text


def test_trends_health_renders_staleness_ribbon(client):
    c, store = client
    _track(store, "k1", "A1", "house")
    _snap(store, 2, ["k1"])
    tr.build(store, now=1000.0)
    r = c.get("/trends/health")
    assert r.status_code == 200
    assert "breadth-ribbon" in r.text and 'class="seg"' in r.text
    assert "stale-legend" in r.text
    # labels + counts are no longer smashed together (the "played <30d544" bug): each bucket's count is
    # in its own element, not string-concatenated onto the label. #80: the legend now also shows a
    # share percentage alongside the raw count ("proper size + labels").
    assert re.search(
        r'<span class="bucket-label">played &lt;30d</span>'
        r'<span class="bucket-pct mono">\d+%</span><span class="bucket-n mono">\(\d+\)</span>', r.text)


def test_trends_review_renders_stat_grid(client):
    """#79 T10f: the three stat tiles are now a thin `.stat-row` (the calendar grid + podium carry
    the primary visual weight), and the old heat-strip is gone in favor of `.review-calendar`."""
    c, store = client
    _track(store, "k1", "A1", "house")
    _snap(store, 0, ["k1"])
    _snap(store, 1, ["k1"])
    tr.build(store, now=40 * 86400.0)
    r = c.get("/trends/review")
    assert r.status_code == 200
    assert 'class="stat-row"' in r.text
    assert "tracks heard" in r.text and "listen days" in r.text and "best streak" in r.text
    # #1: plain-language copy, no "sync-history" jargon in the panel itself
    assert "sync-history" not in r.text.lower()
    assert "tracks across" in r.text
    # #4a: a "Your <Month>" wrapped-style headline derived from the review month (1970-01 -> January)
    assert '<p class="review-headline">Your January</p>' in r.text


def test_trends_discovery_and_diversity_render_area_and_axis(client):
    """#77/#78 fix: both charts now draw a closed area (class spark-area, sitting on a bottom baseline)
    plus a crisp stroke on top (class spark-line), instead of a bare open polyline with no fill rule --
    and both show first/last bucket labels under the chart."""
    c, store = client
    _track(store, "k1", "A1", "house")
    for d in (0, 8, 16, 24, 32, 40, 48, 56, 64, 72):
        _snap(store, d, ["k1"])
    tr.build(store, now=80 * 86400.0)
    r = c.get("/trends/discovery")
    assert r.status_code == 200
    assert 'class="spark-area"' in r.text and 'class="spark-line"' in r.text
    assert 'class="chart-axis"' in r.text

    r2 = c.get("/trends/diversity")
    assert r2.status_code == 200
    if 'class="spark-area"' in r2.text:   # only renders once >= 2 months of history exist
        assert 'class="spark-line"' in r2.text
        assert 'class="chart-axis"' in r2.text
        assert 'class="stat-grid mini"' in r2.text


def test_trends_hover_labels_have_no_stray_midnight(client):
    """Root-cause regression: viz._bands used to format every point with a tz-naive %H:%M, and the
    Trends charts feed day/week/month-anchored timestamps -- so every hover-band label showed a stray
    00:00. Week/month points now carry their own 'Week of ...' / 'Mon YYYY' label instead."""
    c, store = client
    _track(store, "k1", "A1", "house")
    for d in (0, 8, 16, 24, 32, 40, 48, 56, 64, 72):
        _snap(store, d, ["k1"])
    tr.build(store, now=80 * 86400.0)
    assert "00:00" not in c.get("/trends").text
    assert "00:00" not in c.get("/trends/discovery").text
    assert "00:00" not in c.get("/trends/diversity").text


# ── T10e: Insights section (top of Trends) ──────────────────────────────────────────────

def test_trends_insights_section_renders_when_fired(client):
    """Reuses the exact fixture/arithmetic already hand-verified in
    tests/rec/test_trend_rollups_build.py::test_build_emits_ranked_insights_and_spotlight: A1/k1 on
    day 50 sets floor_day=50 (so it's censored for emergence); B1/k2 ramps 1 -> 12 plays across weeks
    98 -> 119, clearing EMERGENCE_MIN_LATEST/EMERGENCE_GROWTH uncensored, and becomes rank-1 (score
    0.2500) ahead of song_of_week (0.2050) and one_song (0.1625)."""
    c, store = client
    _track(store, "k1", "A1", "house")
    _snap(store, 50, ["k1"])
    _track(store, "k2", "B1", "house")
    _snap(store, 98, ["k2"])
    _snap(store, 119, ["k2"] * 12)
    tr.build(store, now=126 * 86400.0)
    r = c.get("/trends")
    assert r.status_code == 200
    assert 'id="insights"' in r.text
    assert "insight-card" in r.text
    assert "B1 is taking off" in r.text            # rank-1 insight's headline
    assert 'href="/artist?name=B1"' in r.text       # baked anchor (artist-subject insight)
    assert "&#9670;" in r.text                      # evidence-line glyph separator


def test_trends_insights_song_of_week_names_the_track(client):
    """M2: song_of_week's rendered card must name the track (baked into the headline by _bake_art),
    not just say "Song of the week" -- the generic phrasing never told the user WHICH track. A single
    artist/track fixture with exactly one completed week of >= SOTW_MIN_PLAYS plays fires ONLY
    song_of_week (too little history for any other detector), so it's unambiguously rank-1."""
    c, store = client
    _track(store, "k1", "A1", "house")
    _snap(store, 14, ["k1"] * 6)      # week 14: 6 plays >= SOTW_MIN_PLAYS(5)
    tr.build(store, now=21 * 86400.0)  # latest full week: 14 (14+7<=21)
    r = c.get("/trends")
    assert r.status_code == 200
    assert 'id="insights"' in r.text
    assert "Tk1" in r.text            # the track's title ("T"+key, per _track's convention)


def test_trends_insights_section_omitted_when_empty(client):
    """A minimal fixture well below every detector's threshold fires nothing -- the section is
    omitted entirely (silence by default), not rendered empty."""
    c, store = client
    _track(store, "k1", "A1", "house")
    _snap(store, 2, ["k1"])
    _snap(store, 5, ["k1"])
    tr.build(store, now=6 * 86400.0)
    r = c.get("/trends")
    assert r.status_code == 200
    assert 'id="insights"' not in r.text


# ── T10f: Month in review redesign ───────────────────────────────────────────────────────

def test_trends_review_renders_calendar_and_podium(client):
    """A seeded full month (Jan 1970, all 31 days) renders the calendar heat grid (one `.cal-day`
    per calendar day) and the top-3 artist podium with the top artist's name."""
    c, store = client
    _track(store, "k1", "A1", "house")
    _track(store, "k2", "A2", "techno")
    for d in range(31):
        _snap(store, d, ["k1"])
    _snap(store, 5, ["k2"])
    tr.build(store, now=32 * 86400.0)
    r = c.get("/trends/review")
    assert r.status_code == 200
    assert 'class="review-calendar"' in r.text
    assert r.text.count('class="cal-day"') == 31
    assert 'class="artist-podium"' in r.text
    assert "A1" in r.text
    assert 'class="song-of-month"' in r.text


def test_trends_review_binge_callout(client):
    """A day at >=3x the trailing median with >=50% one artist renders `.binge-callout` with the
    weekday-qualified day label threaded from the route's `_day_label` helper."""
    c, store = client
    _track(store, "k1", "A1", "house")
    for d in range(1, 8):
        _snap(store, d, ["k1"])
    _snap(store, 8, ["k1"] * 20)
    tr.build(store, now=32 * 86400.0)
    r = c.get("/trends/review")
    assert r.status_code == 200
    assert 'class="binge-callout"' in r.text
    assert "20 plays" in r.text and "100% A1" in r.text


# ── T10g: Library health redesign ────────────────────────────────────────────────────────

def test_trends_health_rediscover_list(client):
    """A cold, high-play track (last played well over REDISCOVER_QUIET_DAYS ago) renders the
    `.rediscover-list` with its title and a months-ago label computed in the route."""
    c, store = client
    _track(store, "k1", "A1", "house")
    store.conn.execute("UPDATE tracks SET title=?, thumbnail=? WHERE identity_key=?",
                       ("Old Favorite", "http://img/old.jpg", "k1"))
    store.conn.commit()
    _snap(store, 0, ["k1"] * 5)
    tr.build(store, now=200 * 86400.0)
    r = c.get("/trends/health")
    assert r.status_code == 200
    assert 'class="rediscover-list"' in r.text
    assert "Old Favorite" in r.text
    assert "months ago" in r.text


def test_trends_health_unopened_albums(client):
    """A saved album with no track history at all (recency=None) renders `.unopened-albums` with
    the album title and an `/album?browse=` link."""
    c, store = client
    _track(store, "k1", "A1", "house")
    store.conn.execute(
        "INSERT INTO saved_albums(browse_id,title,artist,year,type,thumbnail) VALUES (?,?,?,?,?,?)",
        ("br1", "Forgotten LP", "A1", "2020", "Album", None))
    store.conn.commit()
    tr.build(store, now=1000.0)
    r = c.get("/trends/health")
    assert r.status_code == 200
    assert 'class="unopened-albums"' in r.text
    assert "Forgotten LP" in r.text
    assert 'href="/album?browse=br1"' in r.text
