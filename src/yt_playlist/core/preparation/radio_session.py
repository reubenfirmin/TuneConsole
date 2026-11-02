"""Prepare a retained Radio checkpoint without pausing or activating its owner.

The exact wire document remains in settings. File encoding and publication take
place outside a SQLite write transaction. A revision fence admits at most one
bounded queue batch; source or catalog changes invalidate the prepared proof.
"""
import hashlib
import json
from pathlib import Path
from .. import fao
import time

from .. import collection_recovery, entity_names, radio_checkpoint
from ..schema_contract import MigrationError
from ..policy_documents import _publish_encoded
from .rows import Job, main as run


KEY = 'radio_current_session'
HEAD = 'prepared_radio_sessions'
QUEUE = 'prepared_radio_queue'
PHASES = ('preparing', 'awaiting_playback', 'playing', 'appending', 'replacing', 'conflict', 'stopping', 'stopped')
STATUSES = ','.join(repr(phase) for phase in PHASES)


def _unused(*args):
    raise MigrationError('Radio preparation requires its bounded queue worker')


DEFINITION = Job('radio', 79, 93, {'settings': ('key',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        generation INTEGER PRIMARY KEY CHECK(generation>=0),
        source_present INTEGER NOT NULL CHECK(source_present IN (0,1)),
        filename TEXT, digest TEXT CHECK(digest IS NULL OR length(digest)=64),
        byte_size INTEGER CHECK(byte_size BETWEEN 0 AND 16777216),
        active INTEGER NOT NULL CHECK(active IN (0,1)),
        episode_id TEXT, episode_resolution TEXT, account_id INTEGER,
        phase TEXT CHECK(phase IN ({STATUSES})),
        playlist_provider_id TEXT, queue_size INTEGER NOT NULL CHECK(queue_size>=0),
        CHECK((filename IS NULL)=(digest IS NULL)),
        CHECK((filename IS NULL)=(byte_size IS NULL)),
        CHECK(source_present=1 OR filename IS NULL),
        CHECK(active=0 OR (account_id IS NOT NULL AND phase IS NOT NULL AND
            ((episode_id IS NOT NULL AND episode_resolution IS NULL) OR
             (episode_id IS NULL AND episode_resolution='missing_episode_id'))))
    )'''),
    (QUEUE, f'''CREATE TABLE {QUEUE} (
        generation INTEGER NOT NULL REFERENCES {HEAD}(generation),
        position INTEGER NOT NULL CHECK(position>=0),
        section TEXT NOT NULL CHECK(section IN ('tracks','initial_tracks','pending')),
        section_position INTEGER NOT NULL CHECK(section_position>=0),
        state TEXT NOT NULL CHECK(state IN ('played','current','queued','initial','pending')),
        video_id TEXT NOT NULL CHECK(length(video_id)>0),
        recording_id INTEGER, entry_id INTEGER, unresolved_reason TEXT,
        PRIMARY KEY(generation,position),
        CHECK((recording_id IS NOT NULL AND unresolved_reason IS NULL) OR
              (recording_id IS NULL AND entry_id IS NULL AND unresolved_reason='unadmitted_video'))
    ) WITHOUT ROWID'''),
), _unused, _unused, dependencies=('identities', 'recordings', 'youtube_items', 'catalog_entries'),
    filters={'settings': "{prefix}key='radio_current_session'"})


def directory(conn):
    # Serving collectors only scan their own roots. Preparation files have a
    # separate lifetime and are retained, including interrupted generations.
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Radio preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'radio'


def read(row):
    """Verify and read the exact retained wire bytes, including whitespace."""
    if row['filename'] is None:
        return None
    with fao.open_reader(row['filename']) as stream:
        encoded = stream.read(row['byte_size'] + 1)
    if (len(encoded) != row['byte_size'] or hashlib.sha256(encoded).hexdigest() != row['digest']
            or Path(row['filename']).stem != row['digest']):
        raise MigrationError('Prepared Radio file does not match its digest')
    return encoded.decode('utf-8')


def _decode(raw):
    if raw is None:
        return None, (), None
    encoded = raw.encode('utf-8')
    if len(encoded) > collection_recovery.MAX_BYTES:
        raise MigrationError('Radio checkpoint exceeds its preparation byte budget')
    if raw == '':
        return None, (), encoded
    state = json.loads(raw)
    if state is None:
        return None, (), encoded
    if not isinstance(state, dict):
        raise MigrationError('Radio checkpoint must be an object or null')
    normalized = radio_checkpoint.normalize(state)
    if normalized['phase'] not in PHASES:
        raise MigrationError('Unsupported Radio phase')
    if type(state.get('identity')) is not int:
        raise MigrationError('Radio checkpoint requires its recorded account')
    episode = state.get('episode_id')
    if episode is not None and (not isinstance(episode, str) or not episode):
        raise MigrationError('Invalid recorded Radio episode ID')
    playlist = state.get('playlist')
    if playlist is not None and (not isinstance(playlist, str) or not playlist):
        raise MigrationError('Invalid recorded Radio playlist')
    position = state.get('position', -1)
    if type(position) is not int or position < -1:
        raise MigrationError('Invalid Radio playback position')
    queue = []
    for section in ('tracks', 'initial_tracks'):
        tracks = state.get(section, [])
        if not isinstance(tracks, list):
            raise MigrationError('Radio queue must be a list')
        for index, track in enumerate(tracks):
            status = ('initial' if section == 'initial_tracks' else
                      'played' if index < position else 'current' if index == position else 'queued')
            queue.append((section, index, status, track))
    if state.get('pending') is not None:
        queue.append(('pending', 0, 'pending', state['pending']))
    # Shape checks and file hashing occur without a writer reservation. Subject
    # resolution is restricted to the bounded slice admitted by each commit.
    for _, _, _, track in queue:
        if not isinstance(track, dict) or not isinstance(track.get('video_id'), str) or not track['video_id']:
            raise MigrationError('Radio queue requires its exact recorded upload')
    return normalized, queue, encoded


def subject(conn, video):
    observed = conn.execute('SELECT recording_id FROM main.youtube_items WHERE video_id=?', (video,)).fetchone()
    entry = conn.execute('SELECT id,recording_id FROM main.catalog_entries WHERE video_id=? ORDER BY id LIMIT 1', (video,)).fetchone()
    if entry and (not observed or entry[1] != observed[0]):
        raise MigrationError('Radio upload and catalog entry belong to different recordings')
    return (observed[0] if observed else None, entry[0] if entry else None,
            None if observed else 'unadmitted_video')


class RadioJob:
    name = DEFINITION.name
    state = DEFINITION.state
    install = staticmethod(DEFINITION.install)
    progress = staticmethod(DEFINITION.progress)

    def step(self, conn, *, batch_size=128, retry=False, clock=time.time):
        if type(batch_size) is not int or not 1 <= batch_size <= 512:
            raise ValueError('Preparation batch size must be from 1 through 512')
        if conn.in_transaction:
            raise MigrationError('Preparation requires an idle connection')
        current = None
        try:
            # Pin a coherent checkpoint, then release the read transaction
            # before decoding, hashing or fsync. Concurrent writers stay usable.
            conn.execute('BEGIN')
            DEFINITION._version(conn)
            DEFINITION.validate(conn)
            current = self.progress(conn)
            if current is None:
                raise MigrationError('Preparation is not initialized')
            if current.status == 'failed' and not retry:
                raise MigrationError('Repair the input and retry explicitly: ' + current.error)
            if current.status == 'prepared' and not retry:
                return current
            source = conn.execute("SELECT COALESCE(substr(CAST(value AS BLOB),1,?),X''),value IS NULL "
                                  'FROM main.settings WHERE key=?',
                                  (collection_recovery.MAX_BYTES + 1, KEY)).fetchone()
            raw = bytes(source[0]).decode('utf-8') if source and not source[1] else None
            conn.rollback()
            state, queue, encoded = _decode(raw)
            generation = current.input_revision
            filename = digest = None
            if encoded is not None:
                digest = hashlib.sha256(encoded).hexdigest()
                filename = _publish_encoded(directory(conn), encoded, digest)
            conn.execute('BEGIN IMMEDIATE')
            if self.progress(conn) != current:
                return self.progress(conn)
            head = conn.execute(f'SELECT generation FROM {HEAD} WHERE generation=?', (generation,)).fetchone()
            cursor = json.loads(current.cursor)[1] if head and current.cursor else 0
            if not head:
                account = state['identity'] if state else None
                if account is not None and not conn.execute(
                        'SELECT 1 FROM ' + entity_names.owner(conn, 'accounts').table + ' WHERE id=?', (account,)).fetchone():
                    raise MigrationError('Radio checkpoint requires its recorded account')
                conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (generation, int(source is not None), filename, digest, len(encoded) if encoded is not None else None,
                     int(state is not None), state.get('episode_id') if state else None,
                     'missing_episode_id' if state and state.get('episode_id') is None else None,
                     account, state['phase'] if state else None, state.get('playlist') if state else None, len(queue)))
            end = min(cursor + batch_size, len(queue))
            for position in range(cursor, end):
                section, index, status, track = queue[position]
                video = track['video_id']
                conn.execute(f'INSERT INTO {QUEUE} VALUES (?,?,?,?,?,?,?,?,?)',
                    (generation, position, section, index, status, video, *subject(conn, video)))
            changes = conn.execute(f'SELECT owner,source_key FROM {DEFINITION.changes} '
                                   'ORDER BY owner,source_key LIMIT ?', (batch_size,)).fetchall()
            conn.executemany(f'DELETE FROM {DEFINITION.changes} WHERE owner=? AND source_key=?', changes)
            complete = end == len(queue) and not conn.execute(
                f'SELECT 1 FROM {DEFINITION.changes} LIMIT 1').fetchone()
            conn.execute(f'UPDATE {self.state} SET status=?,owner=?,cursor=?,processed=processed+?,error=NULL,'
                         'prepared_revision=CASE WHEN ? THEN input_revision END,updated_at=? WHERE id=1',
                ('prepared' if complete else 'copying', None if complete else 'settings',
                 json.dumps([generation, end]), end - cursor, int(complete), clock()))
            conn.commit()
            return self.progress(conn)
        except Exception as exc:
            conn.rollback()
            if current is not None and not (current.status == 'failed' and not retry):
                conn.execute('BEGIN IMMEDIATE')
                if self.progress(conn) == current:
                    conn.execute(f"UPDATE {self.state} SET status='failed',error=?,prepared_revision=NULL,updated_at=? WHERE id=1",
                                 (str(exc)[:2000], clock()))
                conn.commit()
            raise
        finally:
            conn.rollback()


JOB = RadioJob()


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
