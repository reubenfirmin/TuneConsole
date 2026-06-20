"""Playlists tab: browse every playlist, sort, assign user groups, and run bulk actions."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from yt_playlist.analysis import SYSTEM_PLAYLIST_IDS


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

    def _ids(form):
        return [int(x) for x in (form.get("ids", "") or "").split(",") if x.strip().isdigit()]

    @router.get("/")
    def playlists_page(request: Request):
        labels = {i.id: i.label for i in store.get_identities()}
        groups = store.get_playlist_groups()                 # ytm -> group name
        stats = store.get_playlist_listen_stats()            # pid -> (last_ts, count)
        hidden = store.get_hidden_playlists()                # ytm of playlists hidden from this tab
        rows = []
        for p in store.get_playlists():
            if p.ytm_playlist_id in hidden:
                continue
            last, listens = stats.get(p.id, (None, 0))
            rows.append({
                "id": p.id, "ytm": p.ytm_playlist_id, "title": p.title,
                "identity": labels.get(p.identity_id, "?"),
                "count": p.track_count, "kind": store.playlist_kind(p.id),
                "group": groups.get(p.ytm_playlist_id, ""),
                "last": last, "listens": listens,
            })
        return templates.TemplateResponse(request, "playlists.html", {
            "rows": rows, "has_groups": bool(groups),
            "flash": request.query_params.get("flash"),
            "flash_pl": request.query_params.get("flash_pl"),
        })

    @router.post("/playlists/group")
    async def playlists_group(request: Request):
        form = await request.form()
        name = form.get("name", "")
        n = 0
        for pid in _ids(form):
            pl = store.get_playlist(pid)
            if pl is not None:
                store.set_playlist_group(pl.ytm_playlist_id, name)
                n += 1
        return JSONResponse({"ok": True, "n": n})

    @router.post("/playlists/delete")
    async def playlists_delete(request: Request):
        ops = ctx.ops()
        form = await request.form()
        deleted, hidden, errors = 0, 0, []
        for pid in _ids(form):
            pl = store.get_playlist(pid)
            if pl is None:
                continue
            if pl.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
                # undeletable system playlists (Liked Music, Episodes for Later) -> just hide locally
                store.hide_playlist(pl.ytm_playlist_id)
                hidden += 1
                continue
            try:
                ops.delete(pid)
                deleted += 1
            except ValueError as e:
                errors.append(f"{pl.title}: {e}")
            except Exception:  # noqa: BLE001
                logger.exception("playlists delete of %s failed", pl.ytm_playlist_id)
                errors.append(f"{pl.title}: YouTube returned an unexpected response")
        return JSONResponse({"ok": not errors, "deleted": deleted, "hidden": hidden, "errors": errors})

    return router
