"""Read-only Spotify playlist chooser and import workflow (#22)."""
import json
import secrets
import threading
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from yt_playlist.providers.spotify import SpotifyError
from yt_playlist.library.importing import resolve_track
from yt_playlist.library.executor import add_items_resilient
from yt_playlist.util.action_kinds import COPY_PLAYLIST
from yt_playlist.web.spotify_session import SpotifyDisconnected, import_client

_PREVIEW_TTL_S = 1800


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/spotify/import")
    def spotify_import_page(request: Request):
        try:
            playlists = import_client(store, ctx.now()).playlists()
        except SpotifyDisconnected:
            return RedirectResponse(
                f"/setup?tab=import&flash={quote('Connect Spotify before choosing playlists.')}",
                status_code=303)
        except SpotifyError as exc:
            if exc.status == 401:
                message = "Spotify authorization expired. Disconnect and connect it again."
            elif exc.reason == "QUOTA_EXCEEDED":
                message = "Spotify’s API quota is exhausted. Try again after it resets."
            elif exc.status == 429:
                message = "Spotify is rate-limiting requests. Wait a moment and try again."
            else:
                message = "Spotify playlists could not be loaded."
            return templates.TemplateResponse(request, "spotify_import.html", {
                "playlists": [], "identities": store.get_identities(), "error": message})
        return templates.TemplateResponse(request, "spotify_import.html", {
            "playlists": playlists, "identities": store.get_identities(), "error": None})

    @router.post("/spotify/import/preview")
    async def spotify_import_preview(request: Request):
        form = await request.form()
        selected = list(dict.fromkeys(form.getlist("playlist")))[:10]
        try:
            target_identity = int(form.get("target_identity") or 0)
        except (TypeError, ValueError):
            target_identity = 0
        target = ctx.clients().get(target_identity)
        if not selected or target is None:
            return RedirectResponse(
                f"/spotify/import?flash={quote('Choose a playlist and a connected destination.')}",
                status_code=303)
        try:
            source = import_client(store, ctx.now())
            names = {p.external_id: p.name for p in source.playlists() if p.external_id in selected}
            previews = []
            for playlist_id in selected:
                if playlist_id not in names:     # never fetch an arbitrary id injected into the form
                    continue
                tracks = source.playlist_tracks(playlist_id)
                rows = [resolve_track(target, track) for track in tracks]
                previews.append({"source_id": playlist_id, "name": names[playlist_id], "rows": rows})
        except (SpotifyDisconnected, SpotifyError):
            return RedirectResponse(
                f"/setup?tab=import&flash={quote('Spotify could not be read. Reconnect and try again.')}",
                status_code=303)
        if not previews:
            return RedirectResponse(
                f"/spotify/import?flash={quote('No importable playlists were selected.')}", status_code=303)
        now = ctx.now()
        for old in [k for k, v in ctx.spotify_imports.items()
                    if now - v["created_at"] > _PREVIEW_TTL_S]:
            ctx.spotify_imports.pop(old, None)
        token = secrets.token_urlsafe(24)
        ctx.spotify_imports[token] = {"created_at": now, "target_identity": target_identity,
                                      "playlists": previews}
        expiry = threading.Timer(_PREVIEW_TTL_S, ctx.spotify_imports.pop, args=(token, None))
        expiry.daemon = True
        expiry.start()
        return templates.TemplateResponse(request, "spotify_import_preview.html", {
            "token": token, "previews": previews,
            "target": next((i for i in store.get_identities() if i.id == target_identity), None)})

    @router.post("/spotify/import/confirm")
    async def spotify_import_confirm(request: Request):
        form = await request.form()
        token = form.get("token") or ""
        preview = ctx.spotify_imports.pop(token, None)  # consume first: refresh/replay cannot duplicate
        if not preview or ctx.now() - preview["created_at"] > _PREVIEW_TTL_S:
            return RedirectResponse(
                f"/spotify/import?flash={quote('That preview expired. Preview the import again.')}",
                status_code=303)
        target_identity = preview["target_identity"]
        target = ctx.clients().get(target_identity)
        if target is None:
            return RedirectResponse(
                f"/spotify/import?flash={quote('The YouTube destination is no longer connected.')}",
                status_code=303)
        results = []
        for playlist in preview["playlists"]:
            ids = [row.target_video_id for row in playlist["rows"]
                   if row.status == "matched" and row.target_video_id]
            if not ids:
                results.append({"name": playlist["name"], "playlist_id": None, "added": 0,
                                "unmatched": len(playlist["rows"]), "failed": 0,
                                "error": "No confident YouTube Music matches were found."})
                continue
            try:
                new_id = target.create_playlist(playlist["name"], "Imported from Spotify by TuneConsole")
                added, skipped = add_items_resilient(target, new_id, ids)
                store.record_action(
                    COPY_PLAYLIST,
                    json.dumps({"source": f"Spotify: {playlist['name']}",
                                "source_id": playlist["source_id"], "title": playlist["name"],
                                "added": added, "unmatched": len(playlist["rows"]) - len(ids),
                                "failed": len(skipped)}),
                    "{}", "executed",
                    json.dumps({"new_ytm": new_id, "target_identity": target_identity}), ctx.now())
                results.append({"name": playlist["name"], "playlist_id": new_id, "added": added,
                                "unmatched": len(playlist["rows"]) - len(ids),
                                "failed": len(skipped), "error": None})
            except Exception:  # noqa: BLE001 - one playlist failure must not block the others
                ctx.logger.warning("Spotify playlist import failed for %s", playlist["name"], exc_info=True)
                results.append({"name": playlist["name"], "playlist_id": None, "added": 0,
                                "unmatched": len(playlist["rows"]), "failed": 0,
                                "error": "YouTube Music could not create this playlist."})
        return templates.TemplateResponse(request, "spotify_import_result.html", {"results": results})

    return router
