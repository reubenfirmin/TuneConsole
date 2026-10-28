"""Durable, account-scoped retry deadlines for automatic playlist collection."""
import json
import logging
from datetime import datetime, timezone

from yt_playlist.library.playlist_snapshot import http_status
from yt_playlist.util.retry import error_summary

logger = logging.getLogger(__name__)
PREFIX = "playlist_gc_retry:"
FIRST_DELAY = 3600
MAX_DELAY = 86400


def key(identity_id, ytm):
    return PREFIX + json.dumps([identity_id, ytm], separators=(",", ":"))


def states(store):
    return {name: json.loads(value)
            for name, value in store.settings.get_settings_with_prefix(PREFIX).items()}


def read(store, playlist):
    value = store.settings.get_setting(key(playlist.identity_id, playlist.ytm_playlist_id))
    return json.loads(value) if value else None


def begin(store, playlist, now, previous):
    """Commit the next deadline before network work, including interrupted attempts."""
    attempt = (previous or {}).get("attempts", 0) + 1
    delay = min(MAX_DELAY, FIRST_DELAY * 2 ** min(attempt - 1, 5))
    state = {**(previous or {}), "identity": playlist.identity_id,
             "ytm": playlist.ytm_playlist_id, "attempts": attempt,
             "last_attempt_at": now, "next_retry_at": now + delay}
    store.settings.set_setting(key(playlist.identity_id, playlist.ytm_playlist_id), json.dumps(state))
    return state


def failed(store, playlist, state, exc, phase):
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    status = http_status(root)
    reason = getattr(root, "reason", "").split(";", 1)[0]
    failure_class = f"{phase}:{type(root).__module__}.{type(root).__name__}:{status or ''}:{reason}"
    changed = state.get("failure_class") != failure_class
    state.update(failure_class=failure_class, error=error_summary(root), phase=phase)
    store.settings.set_setting(key(playlist.identity_id, playlist.ytm_playlist_id), json.dumps(state))
    deadline = datetime.fromtimestamp(state["next_retry_at"], timezone.utc).isoformat()
    logger.warning("GC: unresolved playlist %s (account %s); phase=%s; attempt=%d; "
                   "next retry=%s; %s. Playlist and backups retained.",
                   playlist.ytm_playlist_id, playlist.identity_id, phase, state["attempts"],
                   deadline, state["error"], exc_info=changed)


def clear(store, identity_id, ytm):
    store.settings.delete_setting(key(identity_id, ytm))


def next_retry_at(store):
    return min((state["next_retry_at"] for state in states(store).values()), default=None)
