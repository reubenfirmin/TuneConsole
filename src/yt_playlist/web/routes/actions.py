"""Action log (`/actions`) and undo."""
import json
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from yt_playlist.util.action_kinds import (
    ADD_TRACKS, APPLY_MERGE, COPY_INTO, COPY_PLAYLIST, DELETE_EMPTY, DELETE_PLAYLIST, GC_GENERATED,
    MOVE_IDENTITY, PLAN, REMOVE_TRACK, REMOVE_LOCAL_PLAYLIST, RENAME_PLAYLIST, UNDO, is_undoable)
from yt_playlist.library.executor import deserialize_plan


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
            kept = params.get("kept")
            deleted = params.get("deleted") or []
            members = params.get("members") or []
            if params.get("partial"):
                desc = f"partial merge into “{kept}”"
                if deleted:
                    desc += ", deleted " + ", ".join(f"“{t}”" for t in deleted)
                if params.get("error"):
                    desc += f". {params['error']}"
                return desc
            if deleted:
                return f"merged into “{kept}”, deleted " + ", ".join(f"“{t}”" for t in deleted)
            if members:
                return "merged " + ", ".join(f"“{m}”" for m in members) + " (kept all)"
            return f"merge edit on “{kept}”" if kept else "merge edit (updated both)"
        if action.kind == MOVE_IDENTITY:
            if action.status == "delete_pending":
                return f"copy of “{params.get('title')}”; source deletion unconfirmed"
            if action.status == "delete_failed":
                return f"copy of “{params.get('title')}”; source deletion failed"
            if params.get("partial"):
                return f"partial copy of “{params.get('title')}”; source kept"
            if params.get("delete_error"):
                return f"copy of “{params.get('title')}”; source kept: {params['delete_error']}"
            if params.get("skipped") or params.get("unresolved"):
                return f"partial copy of “{params.get('title')}”; source kept"
            verb = "moved" if params.get("deleted") else "copied"
            return f"{verb} “{params.get('title')}” to another identity"
        if action.kind == DELETE_EMPTY:
            return f"delete empty playlist “{params.get('title')}”"
        if action.kind == REMOVE_LOCAL_PLAYLIST:
            return f"removed empty “{params.get('title')}” from TuneConsole; YouTube unchanged"
        if action.kind == DELETE_PLAYLIST:
            return f"delete playlist “{params.get('title')}”"
        if action.kind == GC_GENERATED:
            return f"collect unplayed generated playlist “{params.get('title')}”"
        if action.kind == COPY_PLAYLIST:
            return f"copied “{params.get('source')}” to “{params.get('title')}”"
        if action.kind == COPY_INTO:
            n = params.get("added", 0)
            return f"copied {n} song{'' if n == 1 else 's'} from “{params.get('source')}” into “{params.get('target')}”"
        if action.kind == ADD_TRACKS:
            n = params.get("added", 0)
            return f"added {n} track{'' if n == 1 else 's'} to “{params.get('playlist')}”"
        if action.kind == REMOVE_TRACK:
            return f"removed a track from “{params.get('playlist')}”"
        if action.kind == RENAME_PLAYLIST:
            return f"renamed “{params.get('from')}” to “{params.get('to')}”"
        if action.kind != PLAN:
            return action.kind
        try:
            pe = deserialize_plan(action)
            params = json.loads(action.params_json or "{}")
        except (ValueError, KeyError, TypeError):
            return action.kind
        src = store.playlists.get_playlist(pe.plan.source_playlist_id)
        tgt = store.playlists.get_playlist(pe.plan.target_playlist_id)
        src_name = params.get("source_title") or (src.title if src else None) or pe.source_ytm_playlist_id
        tgt_name = params.get("target_title") or (tgt.title if tgt else None) or "target"
        if pe.mode == "delete":
            return f"delete “{src_name}” ({pe.source_ytm_playlist_id})"
        if pe.mode == "move":
            return f"move “{src_name}” into “{tgt_name}”, delete source"
        return f"merge “{src_name}” into “{tgt_name}”"

    def _when(ts):
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return ""

    @router.get("/actions")
    def actions(request: Request):
        # Drafts save merge-editor selections; they haven't changed any playlists.
        rows = []
        for a in store.actions.get_actions():
            if a.status == "draft":
                continue
            try:
                undo = json.loads(a.undo_json or "{}")
                recovery = undo.get("recovery", {})
            except (ValueError, TypeError, AttributeError):
                undo, recovery = {}, {}
            missing_snapshot = ((undo.get("new_ytm") and undo.get("post") is None)
                                or any(e.get("post") is None for e in undo.get("restored", [])))
            additions = [undo["addition"]] if undo.get("addition") else [
                e["addition"] for e in undo.get("restored", []) if e.get("addition")]
            steps = recovery.get("steps", [])
            rows.append({"a": a, "desc": _action_desc(a), "undoable": is_undoable(a.kind),
                         "when": _when(a.created_at), "recovery_error": recovery.get("error"),
                         "deletions": undo.get("deletions", []),
                         "missing_snapshot": missing_snapshot, "copy_ytm": undo.get("new_ytm"),
                         "uncertain": sum(len(e.get("uncertain", [])) for e in additions),
                         "unattempted": sum(len(e.get("unattempted", [])) for e in additions),
                         "recovery_progress": f"{sum(s.get('phase') == 'complete' for s in steps)}/{len(steps)}",
                         "recovered": [s["created_id"] for s in steps if s.get("created_id")]})
        return templates.TemplateResponse(request, "actions.html", {
            "rows": rows, "flash": request.query_params.get("flash"),
            "flasherr": request.query_params.get("flasherr")})

    @router.post("/actions/{action_id}/recheck-deletion")
    def recheck_deletion(action_id: int):
        try:
            action = ctx.ops().recheck_deletion(action_id)
        except ValueError as exc:
            return RedirectResponse(f"/actions?flasherr={quote(str(exc))}", status_code=303)
        except Exception:
            logger.exception("deletion recheck %s failed", action_id)
            return RedirectResponse("/actions?flasherr=Could%20not%20recheck%20deletion.%20"
                                    "The%20backup%20is%20saved.", status_code=303)
        message = ("Deletion confirmed; the backup is ready for Undo." if action.status == "executed"
                   else "The playlist still exists. No deletion was retried; the backup is saved.")
        return RedirectResponse(f"/actions?flash={quote(message)}", status_code=303)

    @router.post("/undo/{action_id}")
    def undo(action_id: int):
        act = store.actions.get_action(action_id)
        desc = _action_desc(act) if act is not None else f"action #{action_id}"
        try:
            ctx.ops().undo(action_id)   # reverts + best-effort targeted refresh of the target
        except ValueError as e:
            return RedirectResponse(f"/actions?flasherr={quote(str(e))}", status_code=303)
        except Exception:  # noqa: BLE001
            logger.exception("undo %s failed", action_id)
            return RedirectResponse(f"/actions?flasherr={quote('Could not undo. YouTube returned an '
                'unexpected response.')}", status_code=303)
        return RedirectResponse(f"/actions?flash={quote('Undid #' + str(action_id) + ': ' + desc)}",
                                status_code=303)

    return router
