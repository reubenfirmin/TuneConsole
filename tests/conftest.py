import pytest
from yt_playlist.core.store import Store

@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


class FakeClient:
    def __init__(self, playlists=None, tracks=None, history=None, search_results=None, catalog=None,
                 albums=None, song_durations=None):
        self._playlists = playlists or []          # [{"playlistId","title","count"}]
        self._tracks = tracks or {}                # {playlistId: [track dict, ...]}
        self._history = history or []              # [track dict, ...]
        self._search_results = search_results or []  # list returned by search()
        self._catalog = dict(catalog or {})        # {video_id: track_dict}
        self._song_durations = dict(song_durations or {})  # {video_id: seconds} for get_song
        self._albums = albums or {}                # {browseId: album dict (get_album shape)}
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
        tracks = list(self._tracks.get(playlistId, []))
        return {"tracks": tracks[:limit] if limit is not None else tracks}
    def get_history(self): return list(self._history)
    def get_album(self, browseId): return self._albums.get(browseId, {})
    def create_playlist(self, title, description):  # description required, matching real YTMusic API
        pid = f"PL_NEW_{len(self.created)}"; self.created.append((pid, title, description))
        self._tracks.setdefault(pid, []); return pid
    def add_playlist_items(self, playlistId, videoIds):
        self.added.append((playlistId, list(videoIds)))
        dst = self._tracks.setdefault(playlistId, [])
        for vid in videoIds:
            t = self._catalog.get(vid)
            if t is not None:
                dst.append(t)
    def remove_playlist_items(self, playlistId, videos):
        self.removed.append((playlistId, list(videos)))
        gone = {v.get("videoId") for v in videos}
        self._tracks[playlistId] = [t for t in self._tracks.get(playlistId, []) if t.get("videoId") not in gone]
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
        self.edited.append((playlistId, kw)); return "STATUS_SUCCEEDED"

def _track(vid, title, artist, dur=200, album="Alb", set_video_id=None):
    t = {"videoId": vid, "title": title, "artists": [{"name": artist}],
         "album": {"name": album}, "duration_seconds": dur}
    if set_video_id is not None:
        t["setVideoId"] = set_video_id
    return t
