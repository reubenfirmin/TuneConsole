"""Import installed SQLite playback queues before binding the application."""
from contextlib import closing
import json
import sqlite3

from yt_playlist.core import fao, file_collection
from yt_playlist.core.playback_inbox_file import MAX_CAPTURE_BYTES, fence, validate_capture


def legacy_path(database):
    return database.with_name(database.name + '.playback-feedback.sqlite3')


def observation_root(path):
    return path.with_name(path.name + '.observations')


def upgrade(file, source):
    companions = [source.with_name(source.name + suffix) for suffix in ('-wal', '-shm', '-journal')]
    if not source.exists() and not any(path.exists() for path in companions):
        return
    with file.admission():
        queue = file.read()
        if not source.exists():
            if queue.get('legacy_imported'):
                _retire(companions)
            return
        # The old capture path pins observation files before entering its SQL fence.
        with file_collection.collector(observation_root(source)) as admitted:
            if not admitted:
                raise TimeoutError('Installed playback feedback is busy')
            with fence(source.with_name(source.name + '.lock')):
                if not queue.get('legacy_imported'):
                    queue = _read_installed(source, queue)
                # On retry, re-publish before deletion as the previous write
                # may have been interrupted after rename but before directory sync.
                file.write(queue)
                _retire([source, *companions])


def _retire(paths):
    for path in paths:
        path.unlink(missing_ok=True)
    fao._sync_directory(paths[0].parent)


def _read_installed(source, queue, *, read_only=False):
    uri = source.resolve().as_uri() + ('?mode=ro' if read_only else '?mode=rw')
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN' if read_only else 'BEGIN IMMEDIATE')
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError('Unsupported installed playback feedback queue')
        captures, incomplete = [], []
        tables = ('playback_feedback_inbox', 'playback_feedback_inline')
        columns = [{row['name'] for row in conn.execute(f'PRAGMA table_info({table})')} for table in tables]
        if not columns[0]:
            if version != 0 or conn.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone():
                raise ValueError('Missing installed playback feedback queue')
            return {**queue, 'legacy_imported': True}
        sequence = 0
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
            sequence = conn.execute('SELECT max(seq) FROM sqlite_sequence WHERE name IN (?,?)', tables).fetchone()[0] or 0
        for table, fields in zip(tables, columns):
            if not fields:
                continue
            for installed in conn.execute(f'SELECT * FROM {table} ORDER BY sequence'):
                row = {key: installed[key] for key in ('sequence', 'capture_id', 'kind', 'account_id',
                                                        'observed_at', 'video_id', 'rating_status')}
                sequence = max(sequence, row['sequence'])
                try:
                    row['body'] = _installed_body(installed, fields)
                except FileNotFoundError:
                    if 'file_status' not in fields or installed['file_status'] != 'pending':
                        raise
                    # Keep reservations whose bytes were never published as evidence only.
                    incomplete.append(dict(installed))
                    continue
                validate_capture(row)
                captures.append(row)
    captures.sort(key=lambda row: row['sequence'])
    if (len({row['sequence'] for row in captures}) != len(captures)
            or len({row['capture_id'] for row in captures}) != len(captures)):
        raise ValueError('Installed playback feedback has conflicting identities')
    if queue['sequence'] and sequence:
        raise ValueError('Both playback queue formats contain unmerged history')
    result = {**queue, 'legacy_imported': True}
    if sequence:
        result.update(sequence=sequence, captures=captures)
    if incomplete:
        result['incomplete'] = incomplete
    return result


def _installed_body(row, columns):
    if 'body' in columns and row['body'] is not None:
        return json.loads(row['body'])
    size = row['byte_size']
    if type(size) is not int or not 0 < size <= MAX_CAPTURE_BYTES:
        raise ValueError('Invalid installed playback observation size')
    data = fao.read(row['filename'], max_bytes=size, digest=row['content_digest']).bytes
    document = json.loads(data)
    if (len(data) != size or not isinstance(document, dict) or document.get('format') != 1
            or document.get('capture_id') != row['capture_id'] or document.get('kind') != row['kind']):
        raise ValueError('Invalid installed playback observation')
    return document['body']
