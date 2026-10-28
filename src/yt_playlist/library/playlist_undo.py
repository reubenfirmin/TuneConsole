"""Claimed, journaled playlist recovery with verified remote outcomes."""
import json
from collections import Counter
from pathlib import Path
from uuid import uuid4

from yt_playlist.library.action_claim import claim_action
from yt_playlist.library.sync import refresh_playlist
from yt_playlist.library.playlist_mutation import mutation_status, restore_playlist_sequence
from yt_playlist.library.playlist_positions import cancel_insertions
from yt_playlist.library.playlist_snapshot import (
    http_status as _http_status, playlist_if_present)
from yt_playlist.library.undo_conflicts import keeper_target, playlist_state, require_unchanged_copy


def _existing(client, playlist_id):
    """Only an explicit remote 404 establishes absence; connection/parser failures do not."""
    try:
        return playlist_if_present(client, playlist_id)
    except Exception as exc:
        raise ValueError(f"Could not establish whether playlist {playlist_id} exists. "
                         "Reconnect YouTube Music and retry Undo.") from exc


def _client(clients, identity):
    if identity not in clients:
        raise ValueError(f"Account {identity} is unavailable. Reconnect it and retry Undo.")
    return clients[identity]


def _refresh(store, client, identity, playlist_id, title, now, *, created_at=None):
    expected_revision = store.playlists.playlist_revision(identity, playlist_id)
    detail = _existing(client, playlist_id)
    if detail is None:
        raise ValueError(f"Recovered playlist {playlist_id} is missing. Review it before retrying Undo.")
    title = detail.get("title") or title or "Restored"
    refresh_playlist(store, identity, client, playlist_id, title, now, snapshot=detail,
                     expected_revision=expected_revision, created_at=created_at)


def _steps(undo, action_id):
    steps = []
    backups = list(undo.get("backups", []))
    if undo.get("backup"):
        backups.append(undo["backup"])
    # Restore deleted sources before removing transferred songs from keepers or a move destination.
    for index, backup in enumerate(dict.fromkeys(backups)):
        steps.append({"kind": "backup", "backup": backup, "phase": "pending",
                      "marker": f"TuneConsole undo {action_id}/{index}/{uuid4().hex}"})
    steps.extend({"kind": "keeper", "entry": entry, "phase": "pending"}
                 for entry in undo.get("restored", []))
    if undo.get("new_ytm") is not None:
        steps.append({"kind": "copy", "ytm": undo["new_ytm"],
                      "identity": undo.get("target_identity"), "post": undo.get("post"),
                      "phase": "pending"})
    return steps


def _verify_restored_sources(clients, steps):
    """Recheck durable completed steps before taking away another remote copy on a retry."""
    for step in steps:
        if step["kind"] != "backup":
            continue
        payload = json.loads(Path(step["backup"]).read_text(encoding="utf-8"))
        client = _client(clients, payload.get("identity_id"))
        detail = _existing(client, step["created_id"])
        required = Counter(t["video_id"] for t in payload.get("tracks", []))
        actual = Counter(t["videoId"] for t in detail["tracks"]) if detail is not None else Counter()
        if detail is None or required - actual:
            raise ValueError("A restored source is missing or has changed. The remaining playlists "
                             "were preserved. Review the source in YouTube Music before retrying Undo.")


def _restore(client, playlist_id, target, step, save):
    """Persist the target before writes, and retain a verified partial state for a safe retry."""
    initial = _existing(client, playlist_id)
    if initial is None:
        raise ValueError(f"Recovery playlist {playlist_id} is not available yet. Retry Undo to check again.")
    if "target" in step:
        state = playlist_state(initial)
        if state["videos"] == step["target"]:
            step["phase"] = "remote_complete"
            save()
            return
        observed = step.get("observed")
        if observed is None and state == step.get("before"):
            observed = state  # a crash before any observable write can resume from that baseline
        if observed is None:
            raise ValueError("Recovery's last remote state is uncertain. The playlist was preserved. "
                             "Review it in YouTube Music before retrying Undo.")
        target = keeper_target({"prev": step["target"], "post": observed["videos"],
                                "post_state": observed}, state)
    step["target"] = list(target)
    step["before"] = playlist_state(initial)
    step["phase"] = "restoring"
    step["observed"] = None
    save()
    result = restore_playlist_sequence(client, playlist_id, target)
    detail = _existing(client, playlist_id)
    if detail is not None:
        step["observed"] = playlist_state(detail)
    if (result.complete and step["observed"] is not None
            and step["observed"]["videos"] == list(target)):
        step["phase"] = "remote_complete"
        save()
        return
    save()
    raise ValueError(result.error or "The complete recovery could not be verified. Retry Undo to continue.")


def _find_created(client, marker):
    listing = client.get_library_playlists(limit=None)
    if not isinstance(listing, list) or any(not isinstance(p, dict) or not p.get("playlistId") for p in listing):
        raise ValueError("Could not scan the complete playlist library for the pending recovery.")
    found = []
    for item in listing:
        try:
            detail = client.get_playlist(item["playlistId"], limit=None)
        except Exception:
            continue
        if isinstance(detail, dict) and marker in (detail.get("description") or ""):
            found.append(item["playlistId"])
    if len(set(found)) == 1:
        return found[0]
    raise ValueError(f"The playlist creation response was lost. Retry Undo to locate recovery marker "
                     f"'{marker}'. A second playlist will not be created automatically.")


def _restore_backup(store, clients, step, now, save):
    payload = json.loads(Path(step["backup"]).read_text(encoding="utf-8"))
    if payload.get('snapshot_version', 1) not in (1, 2):
        raise ValueError('Unsupported playlist backup version. Review it before recovering manually.')
    identity = payload.get("identity_id")
    client = _client(clients, identity)
    from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION, PROMOTED_DESCRIPTION, is_generated_description
    provenance = payload.get('provenance') or store.playlists.playlist_provenance(payload['ytm_playlist_id'])
    description = payload.get('description') or provenance.get('description') or 'Recreated by TuneConsole undo'
    if is_generated_description(description) and provenance['origin'] == 'ordinary':
        provenance = {'origin': 'tuneconsole', 'adopted': False, 'description': description}
    if provenance['origin'] == 'tuneconsole':
        marker = PROMOTED_DESCRIPTION if provenance['adopted'] else GENERATED_DESCRIPTION
        if provenance['adopted'] and is_generated_description(description):
            description = PROMOTED_DESCRIPTION
        elif marker.casefold() not in description.casefold():
            description += '\n' + marker
    target = [t.get("video_id") for t in payload.get("tracks", [])]
    if any(not isinstance(v, str) or not v for v in target):
        raise ValueError("The backup contains unidentified items. Review it before recovering manually.")
    if not step.get("created_id"):
        original = _existing(client, payload["ytm_playlist_id"])
        if original is not None:
            # A failed/lost forward deletion can leave the source alive. Preserve any later
            # edits and use that original rather than creating a duplicate restoration.
            step["created_id"] = payload["ytm_playlist_id"]
            step["phase"] = "remote_complete"
            step["original_survived"] = True
            save()
        elif step["phase"] == "creating":
            step["created_id"] = _find_created(client, step["marker"])
            step["created_at"] = now
            save()
        else:
            step["phase"] = "creating"
            save()
            try:
                new_id = client.create_playlist(payload["title"], description + '\n' + step["marker"])
            except Exception as exc:
                if _http_status(exc) in (400, 401, 403):
                    step["phase"] = "pending"
                    save()
                    raise ValueError("YouTube rejected playlist creation. Reconnect and retry Undo.") from exc
                new_id = _find_created(client, step["marker"])
            if not isinstance(new_id, str) or not new_id:
                raise ValueError("YouTube did not return the recovery playlist ID. Retry Undo to locate it.")
            step["created_id"] = new_id
            step["created_at"] = now
            step["phase"] = "created"
            save()  # commit the ID before any track writes or local materialization
    new_id = step["created_id"]
    if not step.get('original_survived'):
        store.playlists.set_playlist_provenance(new_id, origin=provenance['origin'],
                                               adopted=provenance['adopted'], description=description)
        group = payload.get('group', store.playlists.get_playlist_groups().get(payload['ytm_playlist_id']))
        if group:
            store.playlists.set_playlist_group(new_id, group)
    if step["phase"] != "remote_complete":
        _restore(client, new_id, target, step, save)
    _refresh(store, client, identity, new_id, payload.get("title"), now,
             created_at=step.get("created_at"))


def _delete_copy(store, clients, step, save):
    client = _client(clients, step["identity"])
    detail = _existing(client, step["ytm"])
    if detail is not None:
        require_unchanged_copy(step.get("post"), playlist_state(detail))
        step["phase"] = "deleting"
        save()
        error = None
        try:
            result = client.delete_playlist(step["ytm"])
        except Exception as exc:
            error, result = exc, None
        status = mutation_status(result)
        if status is not None and status != "STATUS_SUCCEEDED":
            raise ValueError("YouTube has not confirmed deletion of the copied playlist: "
                             "it rejected the request. Retry Undo to recheck it.")
        if _existing(client, step["ytm"]) is not None:
            raise ValueError(f"YouTube has not confirmed deletion of copy {step['ytm']}. "
                             "It was preserved. Retry Undo to check again.") from error
    for pl in store.playlists.get_playlists():
        if pl.identity_id == step["identity"] and pl.ytm_playlist_id == step["ytm"]:
            store.playlists.remove_playlist(pl.id)


def undo_action(store, action_id, clients, now):
    with claim_action(store, action_id):
        action = store.actions.get_action(action_id)
        if action is not None and action.status in ("delete_pending", "delete_failed"):
            from yt_playlist.library.playlist_deletion import reconcile_deletion
            reconcile_deletion(store, action_id, clients, now, locked=True)
        action = store.actions.claim_undo(action_id, now)
        undo = json.loads(action.undo_json or "{}")
        recovery = undo.get("recovery")
        if recovery is None:
            recovery = undo["recovery"] = {"version": 2, "steps": _steps(undo, action_id)}

        def save():
            store.actions.save_undo_progress(action_id, undo, now)

        try:
            if recovery["version"] == 1:
                # Preserve every marker, created ID, conflict baseline and completed phase from
                # an older journal; only its execution order changes, before any remote write.
                recovery["steps"].sort(key=lambda step: step["kind"] != "backup")
                recovery["version"] = 2
            elif recovery["version"] != 2:
                raise ValueError("Unsupported recovery journal version. Review this action manually.")
            recovery.pop("error", None)
            save()
            if not recovery["steps"]:
                raise ValueError("This action has no recovery snapshot. Review it before recovering manually.")
            sources_verified = False
            for step in recovery["steps"]:
                if step["phase"] == "complete":
                    continue
                if step["kind"] in ("keeper", "copy") and not sources_verified:
                    _verify_restored_sources(clients, recovery["steps"])
                    sources_verified = True
                if step["kind"] == "keeper":
                    entry = step["entry"]
                    client = _client(clients, entry["identity"])
                    target = step.get("target")
                    if step["phase"] != "remote_complete":
                        if target is None:
                            detail = _existing(client, entry["ytm"])
                            if detail is None:
                                raise ValueError("The kept playlist is missing. Review it in YouTube Music.")
                            target = keeper_target(entry, playlist_state(detail))
                    pl = next((p for p in store.playlists.get_playlists() if p.identity_id == entry["identity"]
                               and p.ytm_playlist_id == entry["ytm"]), None)
                    if pl is not None:
                        # Retire placement intent before recovery writes or its targeted
                        # refresh; a failed recovery must not resume the superseded move.
                        cancel_insertions(store, pl.id, now)
                    if step["phase"] != "remote_complete":
                        _restore(client, entry["ytm"], target, step, save)
                    _refresh(store, client, entry["identity"], entry["ytm"], pl.title if pl else None, now)
                elif step["kind"] == "backup":
                    _restore_backup(store, clients, step, now, save)
                elif step["kind"] == "copy":
                    _delete_copy(store, clients, step, save)
                else:
                    raise ValueError("Unsupported recovery step. Review this action before continuing.")
                step["phase"] = "complete"
                save()
            store.actions.finish_undo(action_id, undo, now)
        except Exception as exc:
            recovery["error"] = str(exc) or type(exc).__name__
            store.actions.save_undo_progress(action_id, undo, now, failed=True)
            complete = sum(s["phase"] == "complete" for s in recovery["steps"])
            raise ValueError(f"Undo incomplete ({complete}/{len(recovery['steps'])} steps complete). "
                             f"{recovery['error']} Progress is saved; retry this same Undo to continue.") from exc
