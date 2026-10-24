import importlib.util
import inspect

import pytest
from yt_playlist.core.store import Store


# A sync Playwright driver owns a running asyncio loop until its fixture ends.
# Apply the same lifetime to every browser test, including packaged tests outside
# tests/browser/, so mixed runs can return to asyncio.run() in later modules.
def browser_fixture_scope(*, fixture_name, config):
    return "session" if config.getoption("markexpr") == "browser" else "module"


if importlib.util.find_spec("pytest_playwright") is not None:
    from pytest_playwright import pytest_playwright as browser_fixtures

    playwright = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.playwright))
    browser_type = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.browser_type))
    launch_browser = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.launch_browser))
    browser = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.browser))
    browser_context_args = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.browser_context_args))


@pytest.fixture(autouse=True)
def isolated_app_home(tmp_path, monkeypatch):
    """Backups, action locks and settings must never touch the user's app data."""
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path / "app-home"))


@pytest.fixture(autouse=True)
def disable_periodic_library_sync(monkeypatch):
    """Tests drive sync/update checks explicitly, without periodic jobs leaking into later tests."""
    from yt_playlist.web import app as webapp

    monkeypatch.setattr(webapp, "_background_sync_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp, "BACKGROUND_WORK", False)


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


@pytest.fixture
def legacy_store(tmp_path):
    """A genuine pre-catalog database for upgrade tests, never a damaged current schema."""
    from yt_playlist.core.migrations import migrate
    from yt_playlist.core.migrations.registry import MIGRATIONS
    s = Store(tmp_path / 'legacy.db')
    migrate(s.conn, registry=MIGRATIONS[:7])
    s._bind_repositories()
    yield s
    s.conn.close()


def only_provider(store, name):
    """Configure the enrichment waterfall to run exactly one provider (others disabled), lets a
    route-level test exercise the single-icon waterfall while isolating one provider's behavior."""
    from yt_playlist.providers import enrichment as E
    E.save_config(store, [{"name": p["name"], "enabled": p["name"] == name} for p in E.PROVIDERS])


def seed_catalog_track(store, key, title, artist, *, video_id=None, track_id=None, **metadata):
    """Seed explicit catalog subjects for tests whose opaque keys/IDs are part of the fixture."""
    with store.conn:
        rid = store.conn.execute(
            'INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
            (track_id,key,title,artist)).lastrowid
        if video_id:
            store.conn.execute(
                'INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?,?)',
                (video_id,rid,title,artist,title,artist))
        store.conn.execute('INSERT INTO catalog_entries(id,recording_id,video_id,source_ref) VALUES (?,?,?,?)',
                           (rid,rid,video_id,None if video_id else 'fixture:'+key))
        if not video_id:
            store.conn.execute('INSERT INTO unresolved_items(entry_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?)',
                               (rid,title,artist,title,artist))
        store.conn.execute("INSERT INTO library_memberships VALUES (?,'fixture',NULL)", (rid,))
        store.conn.execute("INSERT INTO recording_aliases VALUES (?,?,'fixture')", (key,rid))
        if metadata:
            columns = {r[1] for r in store.conn.execute('PRAGMA table_info(catalog_tracks)')}
            assert metadata.keys() <= columns
            store.conn.execute('UPDATE catalog_tracks SET '+','.join(f'{field}=?' for field in metadata)+' WHERE id=?',
                               [*metadata.values(),rid])
        from yt_playlist.core import artists
        artists.observe(store.conn,rid,artist,browse_id=metadata.get('artist_browse_id'))
    return rid


class FakeClient:
    def __init__(self, playlists=None, tracks=None, history=None, search_results=None, catalog=None,
                 albums=None, song_durations=None, uploads=None, artists=None):
        self._playlists = playlists or []          # [{"playlistId","title","count"}]
        self._tracks = tracks or {}                # {playlistId: [track dict, ...]}
        self._history = history or []              # [track dict, ...]
        self._search_results = search_results or []  # list returned by search()
        self._catalog = dict(catalog or {})        # {video_id: track_dict}
        self._song_durations = dict(song_durations or {})  # {video_id: seconds} for get_song
        self._albums = albums or {}                # {browseId: album dict (get_album shape)}
        self._uploads = uploads or {}              # {browseId: album dict (upload shape)}
        self._artists = artists or {}              # {browseId: artist dict (get_artist shape)}
        for tlist in self._tracks.values():        # auto-seed: a client knows its own tracks
            for t in tlist:
                if t.get("videoId"):
                    self._catalog.setdefault(t["videoId"], t)
        self.created = []; self.added = []; self.removed = []; self.deleted = []; self.edited = []
        self.rated = []                            # [(videoId, rating), ...] from rate_song
    def get_library_playlists(self, limit=25):
        playlists = list(self._playlists)
        return playlists[:limit] if limit is not None else playlists
    def get_playlist(self, playlistId, limit=100):
        if playlistId in self.deleted:
            from ytmusicapi.exceptions import YTMusicServerError
            raise YTMusicServerError("Server returned HTTP 404: Not Found.")
        tracks = list(self._tracks.get(playlistId, []))
        return {"tracks": tracks[:limit] if limit is not None else tracks}
    def get_history(self): return list(self._history)
    def get_library_albums(self, limit=25): return []
    def get_album(self, browseId): return self._albums.get(browseId, {})
    def get_library_upload_album(self, browseId): return self._uploads.get(browseId, {})
    def get_artist(self, browseId): return self._artists.get(browseId, {})
    def create_playlist(self, title, description):  # description required, matching real YTMusic API
        pid = f"PL_NEW_{len(self.created)}"; self.created.append((pid, title, description))
        self._tracks.setdefault(pid, []); return pid
    def add_playlist_items(self, playlistId, videoIds, *, duplicates=False):
        self.added.append((playlistId, list(videoIds)))
        dst = self._tracks.setdefault(playlistId, [])
        results = []
        for vid in videoIds:
            t = self._catalog.get(vid)
            if t is not None:
                dst.append(t)
                if t.get("setVideoId"):
                    results.append({"videoId": vid, "setVideoId": t["setVideoId"]})
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": results}
    def remove_playlist_items(self, playlistId, videos):
        self.removed.append((playlistId, list(videos)))
        handles = {v["setVideoId"] for v in videos if v.get("setVideoId")}
        gone = {v.get("videoId") for v in videos if not v.get("setVideoId")}
        self._tracks[playlistId] = [t for t in self._tracks.get(playlistId, [])
                                   if t.get("setVideoId") not in handles and t.get("videoId") not in gone]
    def delete_playlist(self, playlistId): self.deleted.append(playlistId)
    def rate_song(self, videoId, rating): self.rated.append((videoId, rating))
    def search(self, query, filter="songs"): return list(self._search_results)
    def get_song(self, videoId):
        secs = self._song_durations.get(videoId)
        if secs is None:
            secs = (self._catalog.get(videoId) or {}).get("duration_seconds")
        details = {"videoId": videoId}
        if secs is not None:
            details["lengthSeconds"] = str(secs)   # real ytmusicapi returns it as a string
        return {"videoDetails": details}
    def edit_playlist(self, playlistId, **kw):
        self.edited.append((playlistId, kw))
        move = kw.get("moveItem")
        if move:
            moved, successor = move if isinstance(move, tuple) else (move, None)
            tracks = self._tracks.get(playlistId, [])
            item = next(t for t in tracks if t.get("setVideoId") == moved)
            tracks.remove(item)
            index = next((i for i, t in enumerate(tracks) if t.get("setVideoId") == successor), len(tracks))
            tracks.insert(index, item)
        return "STATUS_SUCCEEDED"

def _track(vid, title, artist, dur=200, album="Alb", set_video_id=None):
    t = {"videoId": vid, "title": title, "artists": [{"name": artist}],
         "album": {"name": album}, "duration_seconds": dur}
    if set_video_id is not None:
        t["setVideoId"] = set_video_id
    return t
