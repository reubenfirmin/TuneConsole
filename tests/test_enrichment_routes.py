"""Enrichment jobs, page reconnection and provider fallbacks."""
import urllib.error

import pytest
from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.web_support import _client
def test_enrich_playlist_via_musicbrainz(store, monkeypatch):
    import json as _json
    import yt_playlist.providers.musicbrainz as mb
    from tests.conftest import only_provider
    # stub MusicBrainz so the test never hits the network (one enrich_full yields genre, year, MBID)
    monkeypatch.setattr(mb, "enrich_full",
                        lambda title, artist, *, observations=None: {"S0": ("rock", "1998", None, [], []),
                                               "S1": ("jazz", "2003", None, [], [])}.get(title, (None, None, None, [], [])))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PL1", "Mix", 3, "h", 1.0)
    store.playlists.set_playlist_tracks(a, [store.tracks.upsert_track("v0", "S0", "X", "Al", 200, 1),
                                  store.tracks.upsert_track("v1", "S1", "Y", "Al", 200, 1),
                                  store.tracks.upsert_track("v2", "Sx", "Z", "Al", 200, 1)])
    only_provider(store, "musicbrainz")          # the waterfall runs MusicBrainz only
    c = _client(store, lambda: {iid: FakeClient()})

    assert len(store.tracks.tracks_to_enrich(a)) == 3
    jid = c.post(f"/playlist/{a}/enrich").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    events = [_json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    types = [e["type"] for e in events if e["type"] != "activity"]
    assert types == ["info", "track", "track", "track", "done", "end"]
    assert [e["title"] for e in events if e.get("provider") == "musicbrainz"] == ["S0", "S1", "Sx"]

    detail = {t["video_id"]: (t["genre"], t["year"]) for t in store.charts.playlist_tracks_detail(a)}
    assert detail["v0"] == ("rock", "1998") and detail["v1"] == ("jazz", "2003")
    assert detail["v2"] == ("", "")                      # no match this run
    # fully-resolved tracks are done; the no-match one stays eligible for a re-run
    assert [t["video_id"] for t in store.tracks.tracks_to_enrich(a)] == ["v2"]

    # re-run after MusicBrainz gains a genre for v2. It fills the gap and v2 is now complete
    monkeypatch.setattr(mb, "enrich_full",
                        lambda title, artist, *, observations=None: ("ambient", "2010", None, [], []) if title == "Sx"
                        else (None, None, None, [], []))
    jid2 = c.post(f"/playlist/{a}/enrich").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid2}") as st:
        "".join(st.iter_text())
    v2 = next(t for t in store.charts.playlist_tracks_detail(a) if t["video_id"] == "v2")
    assert (v2["genre"], v2["year"]) == ("ambient", "2010")
    assert store.tracks.tracks_to_enrich(a) == []

    # a blank result must never clobber values we already have
    store.tracks.set_track_enrichment(store.tracks.track_ids_for_videos(["v0"])["v0"], "", "")
    v0 = next(t for t in store.charts.playlist_tracks_detail(a) if t["video_id"] == "v0")
    assert (v0["genre"], v0["year"]) == ("rock", "1998")


def test_jobs_find_active():
    from yt_playlist.web.jobs import SyncJobs
    jobs = SyncJobs()
    j1 = jobs.create(); j1.playlist_id = 5; j1.source = "musicbrainz"
    j2 = jobs.create(); j2.playlist_id = 5; j2.source = "discogs"
    assert jobs.find_active(5) is j2            # most recent running job for the playlist
    assert jobs.find_active(9) is None
    j2.done = True
    assert jobs.find_active(5) is j1            # falls back to the still-running older one
    j1.done = True
    assert jobs.find_active(5) is None          # nothing running -> no rejoin


def test_page_rejoins_active_enrichment(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    store.playlists.set_playlist_tracks(a, [store.tracks.upsert_track("v0", "S", "X", "Al", 200, 1)])
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")

    assert f"enrichPanel({a}, false, 0, '', null, 0)" in c.get(f"/playlist/{a}").text   # nothing to rejoin
    job = app.state.ctx.jobs.create(); job.playlist_id = a; job.source = "waterfall"
    page = c.get(f"/playlist/{a}").text
    assert f"enrichPanel({a}, false, {job.id}, 'waterfall', null, 0)" in page and "rejoinIfActive()" in page
    job.done = True
    assert f"enrichPanel({a}, false, 0, '', null, 0)" in c.get(f"/playlist/{a}").text   # finished -> no rejoin


def test_discogs_fills_genre_and_year(store, monkeypatch):
    import json as _json
    import yt_playlist.providers.discogs as dc
    # Styles map to the whitelist; the earliest year among matching releases wins.
    monkeypatch.setattr(dc, "_search", lambda q, tok: [
        {"title": "Underworld - Born Slippy", "year": "1996", "genre": ["Electronic"],
         "style": ["Techno", "Drum n Bass"]},
        {"title": "Underworld - Born Slippy", "year": "1995", "genre": ["Electronic"], "style": ["Techno"]},
        {"title": "Unrelated Artist - Different Song", "year": "1952", "genre": ["Classical"]},
    ] if "Born Slippy" in q else [])
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    store.playlists.set_playlist_tracks(a, [store.tracks.upsert_track("v0", "Born Slippy", "Underworld", "Al", 200, 1),
                                  store.tracks.upsert_track("v1", "Unknown", "Nobody", "Al", 200, 1)])
    from tests.conftest import only_provider
    only_provider(store, "discogs")
    c = _client(store, lambda: {iid: FakeClient()})

    jid = c.post(f"/playlist/{a}/enrich").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    events = [_json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    types = [e["type"] for e in events if e["type"] != "activity"]
    assert types == ["info", "track", "track", "done", "end"]
    assert [e["title"] for e in events if e.get("provider") == "discogs"] == ["Born Slippy", "Unknown"]
    detail = {t["video_id"]: (t["genre"], t["year"]) for t in store.charts.playlist_tracks_detail(a)}
    assert detail["v0"] == ("Techno", "1995")        # style->genre, earliest year
    assert detail["v1"] == ("", "")


def test_lastfm_fills_missing_genre_and_year(store, monkeypatch):
    import json as _json
    import yt_playlist.providers.lastfm as lf
    monkeypatch.setenv("LASTFM_API_KEY", "testkey")
    # stub the Last.fm lookup: (genre, year) per track; one fully unknown
    monkeypatch.setattr(lf, "enrich", lambda title, artist, key, *, observations=None:
                        {"S0": ("Trip Hop", None), "S1": ("House", "2010")}.get(title, (None, None)))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PL1", "Mix", 3, "h", 1.0)
    t0 = store.tracks.upsert_track("v0", "S0", "X", "Al", 200, 1)
    store.playlists.set_playlist_tracks(a, [t0, store.tracks.upsert_track("v1", "S1", "Y", "Al", 200, 1),
                                  store.tracks.upsert_track("v2", "S?", "Z", "Al", 200, 1)])
    store.tracks.set_track_year(t0, "1998")                 # fill-only must not clobber this
    from tests.conftest import only_provider
    only_provider(store, "lastfm")
    c = _client(store, lambda: {iid: FakeClient()})

    jid = c.post(f"/playlist/{a}/enrich").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    evs = [_json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    track_evs = [e for e in evs if e["type"] == "track"]
    # events report the effective stored values (so the UI matches the DB), both fields present
    assert track_evs and all("genre" in e and "year" in e for e in track_evs)

    detail = {t["video_id"]: (t["genre"], t["year"]) for t in store.charts.playlist_tracks_detail(a)}
    assert detail["v0"] == ("Trip Hop", "1998")      # genre filled, existing year preserved
    assert detail["v1"] == ("House", "2010")         # both filled from Last.fm
    assert detail["v2"] == ("", "")                  # nothing found -> still missing


def test_lastfm_enrich_scrapes_release_year(monkeypatch):
    import yt_playlist.providers.lastfm as lf
    # one getInfo gives tags + the album page URL; the album page carries the Release Date
    monkeypatch.setattr(lf, "_get", lambda params: {"track": {
        "album": {"url": "https://www.last.fm/music/Ph+1/Sizzling+Love"},
        "toptags": {"tag": [{"name": "seen live"}, {"name": "house"}]}}})
    fetched = []
    def fake_fetch(url):
        fetched.append(url)
        return ('<dt class="catalogue-metadata-heading">Release Date</dt>'
                '<dd class="catalogue-metadata-description">11 March 1996</dd>')
    monkeypatch.setattr(lf, "_fetch_text", fake_fetch)
    assert lf.enrich("Sizzling Love", "Ph 1", "key") == ("House", "1996")
    assert fetched == ["https://www.last.fm/music/Ph+1/Sizzling+Love"]   # the album page, not the track


def test_lastfm_takes_the_year_from_tags_without_fetching_the_page(monkeypatch):
    """The tags come back in the same getInfo call, so a year tag costs nothing. Only when they
    don't carry one is the album page worth a request."""
    import yt_playlist.providers.lastfm as lf
    monkeypatch.setattr(lf, "_get", lambda params: {"track": {
        "album": {"url": "https://www.last.fm/music/Ph+1/Sizzling+Love"},
        "toptags": {"tag": [{"name": "house"}, {"name": "1996"}]}}})
    fetched = []
    monkeypatch.setattr(lf, "_fetch_text", lambda url: fetched.append(url) or "")

    assert lf.enrich("Sizzling Love", "Ph 1", "key") == ("House", "1996")
    assert fetched == []


def test_lastfm_stands_down_after_the_album_pages_keep_failing(monkeypatch):
    """Last.fm 502s its album pages in bursts. After a few, stop asking for a while: the answer
    costs a request every time and no longer arrives. Genre and every other year source are
    untouched."""
    import yt_playlist.providers.lastfm as lf
    monkeypatch.setattr(lf, "_get", lambda params: {"track": {
        "album": {"url": "https://www.last.fm/music/A/B"},
        "toptags": {"tag": [{"name": "house"}]}}})           # tags carry no year
    attempts = []

    def boom(url):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(lf, "_fetch_text", boom)
    monkeypatch.setattr(lf, "_page_fails", 0)
    monkeypatch.setattr(lf, "_page_blocked_until", 0.0)

    for _ in range(lf._PAGE_FAIL_LIMIT + 3):
        assert lf.enrich("S", "A", "key") == ("House", None)   # genre still comes back

    assert len(attempts) == lf._PAGE_FAIL_LIMIT      # then it stopped asking
    monkeypatch.setattr(lf, "_page_blocked_until", 0.0)        # cooldown elapses -> it tries again
    lf.enrich("S", "A", "key")
    assert len(attempts) == lf._PAGE_FAIL_LIMIT + 1


def test_lastfm_page_fetch_does_not_retry_a_server_error(monkeypatch):
    """A 502 is the server answering. Asking again a second later returned the same 502 in practice,
    at twice the cost - so only connection-level failures are worth a retry."""
    import yt_playlist.providers.lastfm as lf
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(lf.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        lf._fetch_text("https://www.last.fm/music/A/B")
    assert len(calls) == 1


def test_lastfm_without_key_reports_error(store, monkeypatch, tmp_path):
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))   # empty config -> no key
    import json as _json
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    store.playlists.set_playlist_tracks(a, [store.tracks.upsert_track("v0", "S0", "X", "Al", 200, 1)])
    from tests.conftest import only_provider
    only_provider(store, "lastfm")
    c = _client(store, lambda: {iid: FakeClient()})
    jid = c.post(f"/playlist/{a}/enrich").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    assert "no API key" in body                       # Last.fm enabled but keyless -> skipped, not run


