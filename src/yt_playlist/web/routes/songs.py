"""Playlist actions used by the single, shared song menu on every regular song surface."""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS
from yt_playlist.util.duration import parse_duration


class Song(BaseModel):
    video_id: str = Field(min_length=1, max_length=200, pattern=r"^\S+$")
    title: str = Field(min_length=1, max_length=1000)
    artist: str = Field(default="", max_length=1000)
    album: str = Field(default="", max_length=1000)
    thumbnail: str | None = None
    album_browse: str | None = None
    duration: int | str | None = None

    def track(self):
        values = self.model_dump(exclude={"video_id", "duration"})
        return {**values, "videoId": self.video_id,
                "duration": parse_duration(self.duration) if self.duration is not None else None}


class CreatePlaylist(BaseModel):
    song: Song
    name: str = Field(min_length=1, max_length=200)


class AddSong(BaseModel):
    song: Song
    playlist_id: int = Field(gt=0)


def build(ctx):
    router = APIRouter()
    store = ctx.store

    @router.get("/songs/playlists")
    def destinations(video_id: str):
        clients = ctx.clients() or {}
        identities = {i.id: i.label for i in store.get_identities()}
        containing = store.playlist_ids_for_video(video_id)
        playlists = [p for p in store.get_playlists()
                     if p.ytm_playlist_id not in SYSTEM_PLAYLIST_IDS and p.identity_id in clients]
        return {"playlists": [{"id": p.id, "title": p.title,
                               "identity": identities.get(p.identity_id, ""),
                               "already_present": p.id in containing}
                              for p in sorted(playlists, key=lambda p: (p.title.casefold(), p.id))],
                "multiple_identities": len(clients) > 1}

    @router.post("/songs/create-playlist")
    async def create_playlist(body: CreatePlaylist):
        try:
            result = await asyncio.to_thread(ctx.ops().create_playlist_from_song,
                                             body.name, body.song.track())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            ctx.logger.exception("create playlist from song failed")
            raise HTTPException(502, "Couldn't create the playlist. Please try again.") from exc
        return {"url": f"/playlist/{result['db_pid']}", "title": result["title"], "added": bool(result["added"]),
                "message": ("Playlist created." if result["added"] else
                            "Playlist created, but YouTube couldn't add this song.")}

    @router.post("/songs/add-to-playlist")
    async def add_song(body: AddSong):
        try:
            result = await asyncio.to_thread(ctx.ops().add_song_to_playlist,
                                             body.playlist_id, body.song.track())
            if not result.get("already_present") and not result["added"]:
                raise ValueError("YouTube couldn't add this song. Please try another song or playlist.")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            ctx.logger.exception("add song to playlist failed")
            raise HTTPException(502, "Couldn't add the song. Please try again.") from exc
        playlist = store.get_playlist(body.playlist_id)
        try:
            ctx.bridge.send_control({"type": "refresh-view", "playlist": playlist.ytm_playlist_id})
        except Exception:  # noqa: BLE001 - a disconnected player must not undo a successful add
            ctx.logger.debug("playlist refresh not sent", exc_info=True)
        return {"url": f"/playlist/{playlist.id}", "title": playlist.title,
                "message": ("This song is already in the playlist." if result.get("already_present")
                            else "Song added.")}

    return router
