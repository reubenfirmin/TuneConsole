"""Cross-process claims shared by forward playlist writes and recovery workflows."""
import os
from contextlib import contextmanager

from yt_playlist.core import paths


@contextmanager
def claim_action(store, action_id, *, operation="Undo"):
    # Keep the established directory/key so existing processes contend on the same inode.
    directory = paths.data_dir() / "undo-locks"
    directory.mkdir(exist_ok=True)
    # Never unlink a lock: every contender must refer to the same inode. OS ownership ends
    # automatically on process exit, so an abandoned durable journal remains resumable.
    with (directory / (store.actions.undo_lock_key(action_id) + ".lock")).open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError(f"{operation} is already running for this action. Reload the Action Log.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f"{operation} is already running for this action. Reload the Action Log.") from exc
        yield
