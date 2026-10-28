"""Cross-process claims shared by forward playlist writes and recovery workflows."""
import os
import json
from contextlib import contextmanager

from yt_playlist.core import paths


@contextmanager
def claim_action(store, action_id, *, operation="Undo"):
    with _claim(store, action_id, f"{operation} is already running for this action. Reload the Action Log."):
        yield


@contextmanager
def claim_playlist_deletion(store, playlist):
    """Admit one deletion owner before reading intent or taking a backup."""
    with claim_deletion_target(store, playlist.identity_id, playlist.ytm_playlist_id):
        if store.playlists.get_playlist(playlist.id) is None:
            raise ValueError("playlist no longer exists")
        yield


@contextmanager
def claim_deletion_target(store, identity_id, remote_id):
    key = json.dumps(["playlist-deletion", identity_id, remote_id])
    with _claim(store, key, "Deletion is already running for this playlist. Reload the Action Log."):
        yield


@contextmanager
def _claim(store, key, message):
    # Keep the established directory/key so existing processes contend on the same inode.
    directory = paths.data_dir() / "undo-locks"
    directory.mkdir(exist_ok=True)
    # Never unlink a lock: every contender must refer to the same inode. OS ownership ends
    # automatically on process exit, so an abandoned durable journal remains resumable.
    with (directory / (store.actions.undo_lock_key(key) + ".lock")).open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError(message) from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(message) from exc
        yield
