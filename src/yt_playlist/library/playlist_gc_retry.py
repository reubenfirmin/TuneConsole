"""Durable, account-scoped retry deadlines for automatic playlist collection."""
import logging
from datetime import datetime, timezone

from yt_playlist.library.playlist_snapshot import http_status
from yt_playlist.util.retry import error_summary
from yt_playlist.library.playlist_policy import retry_state

logger = logging.getLogger(__name__)
FIRST_DELAY = 3600
MAX_DELAY = 86400


def states(store):
    return store.playlists.cleanup_attempts()


def deadlines(store):
    return store.playlists.cleanup_retry_deadlines()


def read(store, playlist):
    return store.playlists.cleanup_attempt(playlist.account_id, playlist.provider_playlist_id)


def begin(store, playlist, now, previous):
    """Commit the next deadline before network work, including interrupted attempts."""
    state = retry_state(playlist.account_id, playlist.provider_playlist_id, now, previous,
                        first_delay=FIRST_DELAY, max_delay=MAX_DELAY)
    store.playlists.set_cleanup_attempt(playlist.account_id, playlist.provider_playlist_id, state)
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
    store.playlists.set_cleanup_attempt(playlist.account_id, playlist.provider_playlist_id, state)
    deadline = datetime.fromtimestamp(state["next_retry_at"], timezone.utc).isoformat()
    logger.warning("GC: unresolved playlist %s (account %s); phase=%s; attempt=%d; "
                   "next retry=%s; %s. Playlist and backups retained.",
                   playlist.provider_playlist_id, playlist.account_id, phase, state["attempts"],
                   deadline, state["error"], exc_info=changed)


def clear(store, account_id, ytm):
    store.playlists.set_cleanup_attempt(account_id, ytm, None)


def next_retry_at(store):
    return min((deadline for _account, _remote, deadline in deadlines(store)), default=None)
