"""Bind the FAO-managed inbox after any installed queue cutover."""
from pathlib import Path

from yt_playlist.controllers.playback_inbox import PlaybackInbox
from yt_playlist.core.playback_inbox_file import InboxFile, inbox_path
from yt_playlist.core.playback_inbox_upgrade import legacy_path, upgrade


def inbox_for_database(database, memory_key):
    file = InboxFile(inbox_path(database, memory_key))
    if database is not None:
        upgrade(file, legacy_path(Path(database).resolve()))
    return PlaybackInbox(file)
