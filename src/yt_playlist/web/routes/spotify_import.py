"""Read-only Spotify playlist chooser and import workflow (#22)."""
import json
import secrets
import threading
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from yt_playlist.providers.spotify import SpotifyError
from yt_playlist.library.importing import existing_overlaps, resolve_track
from yt_playlist.library.executor import add_items_resilient, add_tracks_to_playlist
from yt_playlist.util.action_kinds import COPY_PLAYLIST
from yt_playlist.util.matching import identity_key
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
                overlaps = existing_overlaps(store, target_identity, rows)
                previews.append({"source_id": playlist_id, "name": names[playlist_id], "rows": rows,
                                 "overlaps": overlaps})
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
        decisions = {}
        for raw in form.getlist("decision"):
            parts = raw.split("|", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1] in ("skip", "create", "merge"):
                decisions[int(parts[0])] = (parts[1], int(parts[2]) if parts[2].isdigit() else None)
        results = []
        for index, playlist in enumerate(preview["playlists"]):
            ids = [row.target_video_id for row in playlist["rows"]
                   if row.status == "matched" and row.target_video_id]
            exact = next((o for o in playlist.get("overlaps", []) if o.exact), None)
            default = ("skip", exact.playlist_id) if exact else ("create", None)
            decision, existing_id = decisions.get(index, default)
            allowed = {o.playlist_id: o for o in playlist.get("overlaps", [])}
            if decision == "merge" and existing_id not in allowed:
                decision, existing_id = default
            if decision == "skip":
                results.append({"name": playlist["name"], "playlist_id": None, "added": 0,
                                "unmatched": 0, "failed": 0, "error": None,
                                "skipped": True, "merged_into": None})
                continue
            if decision == "merge":
                existing = store.get_playlist(existing_id)
                current = store.get_playlist_track_keys(existing_id) if existing else set()
                missing = [row for row in playlist["rows"]
                           if row.status == "matched" and row.target_video_id
                           and identity_key(row.source.title, row.source.artist) not in current]
                if existing is None:
                    results.append({"name": playlist["name"], "playlist_id": None, "added": 0,
                                    "unmatched": 0, "failed": 0,
                                    "error": "The selected existing playlist is no longer available.",
                                    "skipped": False, "merged_into": None})
                    continue
                merge_result = add_tracks_to_playlist(store, existing_id, [{
                    "videoId": row.target_video_id, "title": row.target_title,
                    "artist": row.target_artist, "album": None, "duration": row.source.duration_s,
                } for row in missing], target, ctx.now()) if missing else {"added": 0, "skipped": 0}
                results.append({"name": playlist["name"], "playlist_id": existing.ytm_playlist_id,
                                "added": merge_result["added"],
                                "unmatched": len(playlist["rows"]) - len(ids),
                                "failed": merge_result["skipped"], "error": None, "skipped": False,
                                "merged_into": existing.title})
                continue
            if not ids:
                results.append({"name": playlist["name"], "playlist_id": None, "added": 0,
                                "unmatched": len(playlist["rows"]), "failed": 0,
                                "error": "No confident YouTube Music matches were found.",
                                "skipped": False, "merged_into": None})
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
                                "failed": len(skipped), "error": None, "skipped": False,
                                "merged_into": None})
            except Exception:  # noqa: BLE001 - one playlist failure must not block the others
                ctx.logger.warning("Spotify playlist import failed for %s", playlist["name"], exc_info=True)
                results.append({"name": playlist["name"], "playlist_id": None, "added": 0,
                                "unmatched": len(playlist["rows"]), "failed": 0,
                                "error": "YouTube Music could not create this playlist.",
                                "skipped": False, "merged_into": None})
        return templates.TemplateResponse(request, "spotify_import_result.html", {"results": results})

    return router
