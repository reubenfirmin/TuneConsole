"""Everything that makes Liked Music behave unlike a normal playlist, in one place.

Liked Music (the YouTube "LM" system playlist) is special: YouTube owns its membership and rejects
any direct add/remove of playlist items. The only lever the app has is *liking* and *unliking*
songs. So we fake LM membership — rate the song on YouTube and mirror that in the local LM playlist
so the UI flips immediately (a later sync reconciles with YouTube). Concentrating these shims here
lets the rest of the app keep treating LM like an ordinary playlist: add/remove calls land in this
class and get translated into the right rate_song + local-membership update.
"""
import json

from yt_playlist.util.action_kinds import ADD_TRACKS, REMOVE_TRACK
from yt_playlist.util.retry import with_retry

LM_PLAYLIST_ID = "LM"


class LikedMusic:
    """LM-specific operations over a Store. Clients are resolved by the caller (ops) and passed in,
    since they're per-identity; this class only knows how to translate playlist ops into likes."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def is_lm(pl) -> bool:
        """True if `pl` is the YouTube Liked Music system playlist (the one needing these shims)."""
        return pl is not None and pl.ytm_playlist_id == LM_PLAYLIST_ID

    def set_rating(self, identity_id, video_id, on, client) -> None:
        """The one primitive every LM shim shares: rate the song on YouTube and mirror the like in
        the local LM playlist so the derived `liked` flag / membership flips at once."""
        with_retry(lambda: client.rate_song(video_id, "LIKE" if on else "INDIFFERENT"))
        self.store.set_song_liked(identity_id, video_id, on)

    def add(self, playlist_id, tracks, client, now) -> dict:
        """"Add" on Liked Music = like each song — YouTube rejects directly-added LM items, so this is
        what actually lands a song there. Used when adding an alternate version or a 'complete this
        playlist' suggestion while viewing Liked Music. Counterpart to executor.add_tracks_to_playlist.
        """
        pl = self._require_lm(playlist_id)
        items = [t for t in tracks if t.get("videoId")]
        if not items:
            raise ValueError("no tracks to add")
        titles = []
        for t in items:
            # Seed the catalog row FIRST: set_song_liked (inside set_rating) looks the song up by
            # video_id, so a brand-new alternate version must exist in `tracks` before we can like it.
            self.store.upsert_track(t["videoId"], t.get("title", ""), t.get("artist"),
                                    t.get("album"), t.get("duration"), 1,
                                    None, None, t.get("album_browse"), t.get("thumbnail"))
            self.set_rating(pl.identity_id, t["videoId"], True, client)
            titles.append(t.get("title", ""))
        self.store.record_action(ADD_TRACKS,
                                 json.dumps({"playlist": pl.title, "added": len(items), "titles": titles}),
                                 "{}", "executed", "{}", now)
        return {"added": len(items), "skipped": 0, "count": len(items)}

    def remove(self, playlist_id, video_id, client, now) -> dict:
        """"Remove" on Liked Music = unlike the song — LM has no removable playlist item to delete.
        Counterpart to executor.remove_track for the LM case."""
        pl = self._require_lm(playlist_id)
        self.set_rating(pl.identity_id, video_id, False, client)
        count = len(self.store.get_playlist_track_ids(playlist_id))
        self.store.set_playlist_track_count(playlist_id, count, now)
        self.store.record_action(REMOVE_TRACK,
                                 json.dumps({"playlist": pl.title, "video_id": video_id, "unliked": True}),
                                 "{}", "executed", "{}", now)
        return {"count": count}

    def _require_lm(self, playlist_id):
        pl = self.store.get_playlist(playlist_id)
        if pl is None:
            raise ValueError("playlist no longer exists")
        if not self.is_lm(pl):                            # guard: these shims are Liked-Music-only
            raise ValueError("not a Liked Music playlist")
        return pl
