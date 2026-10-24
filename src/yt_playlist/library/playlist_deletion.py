"""Durable forward deletion intent, shared by manual deletion, merges and collection.

Every remote write has a committed backup reference first. Interrupted writes are
read back on recovery; recovery never replays a destructive request.
"""
import json
from contextlib import nullcontext

from yt_playlist.library.playlist_snapshot import http_status, playlist_if_present
from yt_playlist.library.playlist_mutation import mutation_status
from yt_playlist.library.action_claim import claim_action
from yt_playlist.util.action_kinds import APPLY_MERGE, GC_GENERATED, MOVE_IDENTITY, PLAN


def deletion_status(undo):
    phases = {entry["phase"] for entry in undo.get("deletions", [])}
    if phases - {"complete", "failed"}:
        return "delete_pending"
    return "delete_failed" if "failed" in phases else "executed"


def _save(store, action_id, undo, now):
    store.actions.update_action(action_id, deletion_status(undo), now,
                                undo_json=json.dumps(undo))


def _prune(store, action, entry):
    # IDs can change after a re-sync. Match the remote account and playlist too.
    for playlist in store.playlists.get_playlists():
        if (playlist.identity_id == entry["identity"]
                and playlist.ytm_playlist_id == entry["ytm"]):
            store.playlists.remove_playlist(playlist.id)
    if action.kind == GC_GENERATED:
        store.playlist_recipes.delete_cluster_canvas(entry["ytm"])


def pending_deletion(store, playlist):
    """Find an interrupted attempt before a repeated UI request or GC can start a new one."""
    return store.actions.get_pending_deletion(playlist.identity_id, playlist.ytm_playlist_id)


def reconcile_deletion(store, action_id, clients, now, *, locked=False):
    """Resolve a pending attempt using complete reads, without another deletion."""
    with nullcontext() if locked else claim_action(store, action_id, operation="Deletion"):
        action = store.actions.get_action(action_id)
        if action is None or action.status not in ("delete_pending", "delete_failed"):
            raise ValueError("This action has no pending deletion. Reload the Action Log.")
        undo = json.loads(action.undo_json)
        for entry in undo.get("deletions", []):
            if entry["phase"] == "complete":
                continue
            client = clients.get(entry["identity"])
            if client is None:
                raise ValueError("Reconnect the playlist's account and recheck its deletion.")
            try:
                present = playlist_if_present(client, entry["ytm"])
            except Exception as exc:
                entry["phase"] = "uncertain"
                _save(store, action_id, undo, now)
                raise ValueError("Deletion is still unconfirmed. The backup is saved; reconnect "
                                 "and recheck in the Action Log. No deletion was retried.") from exc
            if present is not None:
                entry["phase"] = "failed"
                entry["error"] = "The playlist still exists on YouTube. No deletion was retried."
            else:
                entry["phase"] = "remote_complete"
                entry.pop("error", None)
                _save(store, action_id, undo, now)
                _prune(store, action, entry)
                entry["phase"] = "complete"
            _save(store, action_id, undo, now)
        params = json.loads(action.params_json)
        if action.kind == MOVE_IDENTITY:
            params["deleted"] = all(entry["phase"] == "complete" for entry in undo["deletions"])
            if params["deleted"]:
                params["delete_error"] = None
        elif action.kind == APPLY_MERGE:
            params["deleted"] = list(dict.fromkeys([
                *params.get("deleted", []),
                *(entry.get("title", entry["ytm"]) for entry in undo["deletions"]
                  if entry["phase"] == "complete")]))
        store.actions.update_action(action_id, deletion_status(undo), now, params_json=json.dumps(params))
        return store.actions.get_action(action_id)


def resume_pending_deletion(store, playlist, client, now):
    action = pending_deletion(store, playlist)
    if action is None:
        return None
    action = reconcile_deletion(store, action.id, {playlist.identity_id: client}, now)
    if action.status != "executed":
        raise ValueError("The previous deletion did not complete. The playlist was preserved. "
                         "Review the saved action before requesting deletion again.")
    undo = json.loads(action.undo_json)
    return next(entry["backup"] for entry in undo["deletions"]
                if entry["identity"] == playlist.identity_id and entry["ytm"] == playlist.ytm_playlist_id)


def delete_backed_up(store, playlist, client, backup, now, *, kind, action_id=None,
                     params=None, locked=False):
    """Attach durable intent to its owning action, then issue exactly one remote write."""
    new_action = action_id is None
    entry = {"identity": playlist.identity_id, "ytm": playlist.ytm_playlist_id,
             "title": playlist.title, "backup": backup, "phase": "pending"}
    if new_action:
        action_id = store.actions.record_action(
            kind, json.dumps(params or {"ytm": playlist.ytm_playlist_id, "title": playlist.title}),
            "{}", "delete_pending", json.dumps({"backup": backup, "deletions": [entry]}), now)
    with nullcontext() if locked else claim_action(store, action_id, operation="Deletion"):
        action = store.actions.get_action(action_id)
        if action.status not in ("planned", "executed", "delete_pending"):
            raise ValueError("This action changed before deletion. Reload the Action Log.")
        undo = json.loads(action.undo_json)
        entries = undo.setdefault("deletions", [])
        if new_action:
            entry = entries[0]
            if entry["phase"] != "pending":
                raise ValueError("This deletion was already reconciled. Reload the Action Log.")
        elif any(item["identity"] == playlist.identity_id and item["ytm"] == playlist.ytm_playlist_id
                 for item in entries):
            raise ValueError("This deletion already has a saved attempt. Recheck it in the Action Log.")
        else:
            entries.append(entry)
        if action.kind in (PLAN, MOVE_IDENTITY):
            undo["backup"] = backup
        if backup != undo.get("backup") and backup not in undo.setdefault("backups", []):
            undo["backups"].append(backup)
        _save(store, action_id, undo, now)  # must commit before transport, including planned actions
        try:
            result = client.delete_playlist(playlist.ytm_playlist_id)
        except Exception as exc:
            entry["phase"] = "failed" if http_status(exc) in (400, 401, 403) else "uncertain"
            entry["error"] = "Deletion was not confirmed. The backup is saved; recheck this action."
            _save(store, action_id, undo, now)
            raise
        status = mutation_status(result)
        if status is not None and status != "STATUS_SUCCEEDED":
            entry["phase"] = "failed"
            _save(store, action_id, undo, now)
            raise ValueError("YouTube rejected playlist deletion. The playlist and backup were retained.")
        try:
            present = playlist_if_present(client, playlist.ytm_playlist_id)
        except Exception as exc:
            entry["phase"] = "uncertain"
            _save(store, action_id, undo, now)
            raise ValueError("Deletion was not confirmed. Recheck the saved action before retrying.") from exc
        if present is not None:
            entry["phase"] = "failed"
            _save(store, action_id, undo, now)
            raise ValueError("YouTube has not confirmed deletion. The playlist and backup were retained.")
        entry["phase"] = "remote_complete"
        _save(store, action_id, undo, now)  # retain remote completion even if local pruning fails
        _prune(store, action, entry)
        entry["phase"] = "complete"
        _save(store, action_id, undo, now)
    return action_id
