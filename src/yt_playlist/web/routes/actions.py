"""Action log (`/actions`) and undo."""
import json
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from yt_playlist import sync as sync_mod
from yt_playlist.action_kinds import (
    APPLY_MERGE, DELETE_EMPTY, MOVE_IDENTITY, PLAN, UNDO, is_undoable)
from yt_playlist.executor import deserialize_plan, undo_action


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

    def _action_desc(action):
        # Human summary of what an action touched, surviving pruned playlists (fall back to ytm id).
        params = {}
        try:
            params = json.loads(action.params_json or "{}")
        except (ValueError, TypeError):
            pass
        if action.kind == UNDO:
            return f"undo of action #{params.get('undid')}"
        if action.kind == APPLY_MERGE:
            t = params.get("title")
            return f"merge edit, deleted “{t}”" if t else "merge edit (updated both)"
        if action.kind == MOVE_IDENTITY:
            verb = "moved" if params.get("deleted") else "copied"
            return f"{verb} “{params.get('title')}” to another identity"
        if action.kind == DELETE_EMPTY:
            return f"deleted empty “{params.get('title')}”"
        if action.kind != PLAN:
            return action.kind
        try:
            pe = deserialize_plan(action)
            params = json.loads(action.params_json or "{}")
        except (ValueError, KeyError, TypeError):
            return action.kind
        src = store.get_playlist(pe.plan.source_playlist_id)
        tgt = store.get_playlist(pe.plan.target_playlist_id)
        src_name = params.get("source_title") or (src.title if src else None) or pe.source_ytm_playlist_id
        tgt_name = params.get("target_title") or (tgt.title if tgt else None) or "target"
        if pe.mode == "delete":
            return f"delete “{src_name}” ({pe.source_ytm_playlist_id})"
        if pe.mode == "move":
            return f"move “{src_name}” into “{tgt_name}”, delete source"
        return f"merge “{src_name}” into “{tgt_name}”"

    @router.get("/actions")
    def actions(request: Request):
        rows = [{"a": a, "desc": _action_desc(a), "undoable": is_undoable(a.kind)}
                for a in store.get_actions()]
        return templates.TemplateResponse(request, "actions.html", {"rows": rows})

    @router.post("/undo/{action_id}")
    def undo(action_id: int):
        clients = ctx.client_provider()
        try:
            undo_action(store, action_id, clients, now_fn())
        except ValueError as e:
            return RedirectResponse(f"/?flasherr={quote(str(e))}", status_code=303)
        except Exception:  # noqa: BLE001
            logger.exception("undo %s failed", action_id)
            return RedirectResponse(f"/?flasherr={quote('Could not undo — YouTube returned an '
                'unexpected response.')}", status_code=303)
        # targeted refresh of the target (added tracks were removed); recreated source picked up
        # on the next full Sync. Avoids a slow full re-sync that left the browser spinning.
        try:
            tgt = store.get_playlist(deserialize_plan(store.get_action(action_id)).plan.target_playlist_id)
            if tgt is not None and tgt.identity_id in clients:
                sync_mod.refresh_playlist(store, tgt.identity_id, clients[tgt.identity_id],
                                          tgt.ytm_playlist_id, tgt.title, now_fn())
        except Exception:  # noqa: BLE001 - best-effort refresh
            logger.exception("post-undo refresh failed (non-fatal)")
        return RedirectResponse("/?flash=Undone.", status_code=303)

    return router
