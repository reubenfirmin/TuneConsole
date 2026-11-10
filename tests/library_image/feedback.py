"""Read-only capture of the independent playback inbox at the SQL boundary."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def admission(database):
    from yt_playlist.core import playback_inbox, policy_documents
    queue = playback_inbox.Inbox(database, 'library-image')
    if not queue.path.exists():
        yield None
        return
    # Never use Inbox.admission(): it opens the native lock in append mode.
    with queue.path.with_name(queue.path.name + '.lock').open('rb') as stream:
        release = policy_documents._try_file_lock(stream, exclusive=True)
        if release is None:
            raise RuntimeError('Playback feedback admission is busy; retry preparation')
        try:
            with policy_documents.retained_reader(queue.directory):
                yield queue
        finally:
            release()


def capture(queue, target, report):
    from yt_playlist.core import fao, playback_inbox
    if queue is None:
        report['playback_inbox'] = None
        return
    target = target / 'state.db.playback-feedback.sqlite3'
    observations = target.with_name(target.name + '.observations')
    with closing(sqlite3.connect(queue.path.as_uri() + '?mode=ro', uri=True)) as source, \
            closing(sqlite3.connect(target)) as copied:
        source.execute('PRAGMA query_only=ON')
        source.backup(copied)
        copied.row_factory = sqlite3.Row
        rows = copied.execute('SELECT * FROM playback_feedback_inbox ORDER BY sequence').fetchall()
        columns = {row[1] for row in copied.execute('PRAGMA table_info(playback_feedback_inbox)')}
        if 'filename' in columns:
            with copied:
                for row in rows:
                    playback_inbox.Inbox.body(row)  # Verify owner and content before relocation.
                    relative = Path(row['content_digest'][:2]) / (row['content_digest'] + '.json')
                    fao.write(observations / relative, fao.read(row['filename'], max_bytes=row['byte_size'], digest=row['content_digest']))
                    logical = Path('/library') / observations.name / relative
                    copied.execute('UPDATE playback_feedback_inbox SET filename=? WHERE sequence=?',
                                   (str(logical), row['sequence']))
        assert copied.execute('PRAGMA integrity_check').fetchall()[0][0] == 'ok'
        report['playback_inbox'] = dict(rows=len(rows), legacy='filename' not in columns,
                                       source_total_changes=source.total_changes)
    target.with_name(target.name + '.lock').touch()
    observations.mkdir(exist_ok=True)
    (observations / '.readers.lock').touch()


def verify(root):
    from yt_playlist.core.playback_inbox import Inbox
    database = root / 'state.db.playback-feedback.sqlite3'
    if not database.exists():
        return
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute('PRAGMA table_info(playback_feedback_inbox)')}
        if 'filename' not in columns:
            return  # The normal private Inbox.connect() retains its legacy conversion.
        for row in connection.execute('SELECT * FROM playback_feedback_inbox'):
            if not Path(row['filename']).resolve().is_relative_to(root):
                raise ValueError('Unisolated playback observation coordinate')
            Inbox.body(row)
