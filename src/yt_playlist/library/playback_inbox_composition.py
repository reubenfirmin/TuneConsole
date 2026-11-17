"""Bind the independent inbox database and its database-free observation files."""
from pathlib import Path
import tempfile

from yt_playlist.controllers.playback_inbox import PlaybackInbox
from yt_playlist.core.playback_observation_files import ObservationFiles
from yt_playlist.repos.playback_inbox import PlaybackInboxRepo


def inbox_for_database(database, memory_key):
    path = (Path(database).with_name(Path(database).name + '.playback-feedback.sqlite3') if database else
            Path(tempfile.gettempdir()) / ('tuneconsole-playback-' + memory_key + '.sqlite3'))
    return PlaybackInbox(PlaybackInboxRepo(path), ObservationFiles(
        path.with_name(path.name + '.observations'), path.with_name(path.name + '.lock')))
