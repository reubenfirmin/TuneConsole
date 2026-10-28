"""Client views of live and saved radio state; no lifecycle transitions or I/O."""
from yt_playlist.rec import radio_tuning
from yt_playlist.rec.radio_directions import decay_progress
from yt_playlist.web.radio_state import Session, selected_slot


def history_tracks(tracks, heard, position, furthest):
    """Keep observed playback even when a racing suffix edit removed that song."""
    end = min(len(tracks), max(1, furthest + 1, position + 1))
    prefix = tracks[:end]
    indices = {track["video_id"]: i for i, track in enumerate(prefix)}
    history, seen, cursor = [], set(), 0

    def append(track):
        if track["video_id"] not in seen:
            history.append(track)
            seen.add(track["video_id"])

    for track in heard:
        index = indices.get(track["video_id"])
        if index is not None:
            for previous in prefix[cursor:index + 1]:
                append(previous)
            cursor = max(cursor, index + 1)
        else:
            append(track)
    for track in prefix[cursor:]:
        append(track)
    return history


def status_view(s: Session | None, *, active: bool, connected: bool, now: float) -> dict:
    history = history_tracks(s.tracks, s.heard, s.position, s.furthest) if s else []
    directions = s.directions.public(s.tracks[max(0, s.furthest) + 1:]) if s and s.directions else []
    selected = selected_slot(s) if s else None
    for branch in directions:
        branch["active"] = branch["slot"] == selected
    return {
        "can_play_songs": True,
        "song_play_version": 4,
        "active": active, "starting": bool(s and (s.starting or now < s.launch_deadline)),
        "playlist_id": s.playlist if s else None, "local_id": s.local_id if s else None,
        "title": s.title if s else None,
        "playlist_url": f"https://music.youtube.com/playlist?list={s.playlist}" if s and s.playlist else None,
        "position": s.position if s else -1,
        "ahead": max(0, len(s.tracks) - s.position - 1) if s and s.position >= 0 else None,
        "tracks": list(s.tracks) if s else [],
        "history": history, "history_count": len(history),
        "error": s.error if s else None, "observer_error": s.observer_error if s else None,
        "selection_error": s.selection_error if s else None,
        "exhausted": bool(s and s.exhausted), "connected": connected,
        "browser": s.browser if s else None,
        "browser_age": now - s.browser_seen_at if s and s.browser_seen_at is not None else None,
        "directions": directions, "menu_id": s.directions.menu_id if s and s.directions else None,
        "revision": s.directions.revision if s and s.directions else 0,
        "tuning": dict(s.candidates.tuning) if s else dict(radio_tuning.DEFAULTS),
        "metadata_revision": s.metadata_revision if s else 0,
        "updating": bool(s and (s.replacement or s.requested_direction)),
        "pending_slot": selected if s and (s.replacement or s.requested_direction) else None,
    }


def saved_view(saved: dict, *, active: bool) -> dict:
    plan = saved.get("directions") or {}
    tracks = saved.get("tracks", [])
    history = history_tracks(tracks, saved.get("heard", []), saved.get("position", -1), saved.get("furthest", -1))
    scheduled = {t["video_id"] for t in tracks[max(0, saved.get("furthest", -1)) + 1:]}
    replacement = saved.get("requested_direction") or saved.get("replacement")
    selected = replacement["slot"] if replacement else plan.get("active", 0)
    return {"active": active, "can_play_songs": True, "song_play_version": 4,
        "starting": bool(active and saved.get("initial_tracks")), "local_id": saved.get("local_id"),
        "playlist_id": saved.get("playlist"), "title": saved.get("title"), "tracks": tracks,
        "position": saved.get("position", -1), "menu_id": plan.get("menu_id"),
        "tuning": saved.get("tuning", radio_tuning.DEFAULTS), "metadata_revision": 0,
        "history": history, "history_count": len(history),
        "updating": bool(replacement), "pending_slot": replacement["slot"] if replacement else None,
        "directions": [{**branch, "slot": slot, "active": slot == selected,
            **decay_progress(branch, plan.get("decay", {}).get(str(slot)), saved.get("transition", 0)),
            "tracks": [{**t, "saved": t["video_id"] in scheduled} for t in branch["tracks"]]}
            for slot, branch in enumerate(plan.get("slots", [])) if branch]}
