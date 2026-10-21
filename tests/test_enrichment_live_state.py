"""Enrichment delivery and replay invalidate a row; historical events cannot restore old facts."""
import asyncio
import json

import pytest
from starlette.requests import Request

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.playlists import build


@pytest.mark.parametrize("scope", ["playlist", "album", "generated"])
def test_delivery_and_reconnect_render_current_authoritative_row(store, scope):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PL", "Mix", 1, "", 0)
    store.playlists.upsert_playlist(iid, "LM", "Liked Music", 0, "", 0)
    entry = store.tracks.upsert_track("video", "Old title", "Old artist", "Album", 200,
                               album_browse_id="BID")
    store.playlists.set_playlist_tracks(pid, [entry])
    if scope == "generated":
        store.playlists.set_playlist_group("PL", "Generated")
        store.playlists.mark_generated_playlist('PL')
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000)
    job = app.state.ctx.jobs.create()
    if scope == "album":
        job.album_browse = "BID"
    else:
        job.playlist_id = pid
    job.events.append({"type": "info", "text": "started"})
    endpoint = next(route.endpoint for route in build(app.state.ctx).routes
                    if getattr(route, "path", "") == "/playlist/enrich/events/{job_id}")
    request = Request({"type": "http", "app": app, "headers": []})

    async def scenario():
        response = await endpoint(request, job.id)
        assert "started" in await anext(response.body_iterator)
        # These succeed after the connection opens and before the enrichment event is delivered.
        store.tracks.set_track_title(entry, "Edited title")
        store.tracks.set_track_artist(entry, "Edited artist")
        store.playlists.set_song_liked(iid, "video", True)
        store.tracks.set_track_genre(entry, "Chosen genre")
        store.tracks.set_track_year(entry, "1999")
        key = store.tracks.catalog_subject(entry)["identity_key"]
        store.rec_surface.record_mood([key], 1, now=1000)
        job.events.append({"type": "track", "video_id": "video", "genre": "Old genre", "year": "2000"})
        job.done = True
        delivered = json.loads((await anext(response.body_iterator))[6:])
        await response.body_iterator.aclose()
        # A subsequent user choice wins over replay of the same historical event on reconnect.
        store.tracks.set_track_genre(entry, "Newest genre")
        store.tracks.set_track_year(entry, "2001")
        replay = await endpoint(request, job.id)
        events = [json.loads(chunk[6:]) async for chunk in replay.body_iterator]
        return delivered, next(e for e in events if e["type"] == "track")

    delivered, replayed = asyncio.run(scenario())
    for event, genre, year in ((delivered, "Chosen genre", "1999"), (replayed, "Newest genre", "2001")):
        html = event["row_html"]
        assert event["genre"] == genre and event["year"] == year
        assert 'data-title="edited title"' in html and 'data-artist="edited artist"' in html
        assert f'data-genre="{genre.lower()}"' in html and f'data-year="{year}"' in html
        assert "Old title" not in html and "Old genre" not in html
        assert "Edited title" in html and "Edited artist" in html
        assert 'class="like-btn on"' in html and 'aria-pressed="true"' in html
        if scope != "album":
            assert "Restore original title" in html and "Restore original artist" in html
        if scope == "generated":
            assert 'x-data="{ mood: 1 }"' in html


def test_replayed_event_for_removed_track_cannot_restore_a_row(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PL", "Mix", 1, "", 0)
    entry = store.tracks.upsert_track("video", "Song", "Artist", None, 200)
    store.playlists.set_playlist_tracks(pid, [entry])
    app = create_app(store, lambda: {}, now_fn=lambda: 1000)
    job = app.state.ctx.jobs.create()
    job.playlist_id = pid
    job.events = [{"type": "track", "video_id": "video", "row_html": "obsolete fragment"}]
    job.done = True
    store.playlists.set_playlist_tracks(pid, [])
    endpoint = next(route.endpoint for route in build(app.state.ctx).routes
                    if getattr(route, "path", "") == "/playlist/enrich/events/{job_id}")
    async def read():
        response = await endpoint(Request({"type": "http", "app": app, "headers": []}), job.id)
        return [json.loads(chunk[6:]) async for chunk in response.body_iterator]
    events = asyncio.run(read())
    assert "row_html" not in events[0]
