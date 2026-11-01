"""Stage retained album-note files without changing their serving owner.

Exact proposal bytes and every observed generation remain retained separately
from serving-file collectors. File work happens without a SQLite transaction;
the progress revision fences each bounded commit against continuing writes.
"""
import hashlib
import json
from pathlib import Path
import time

from .. import album_note_outputs, album_note_requests, recommendation_outputs
from ..migrations.runner import MigrationError
from ..policy_documents import _publish_encoded
from ..recommendation_work import finite
from .rows import Job, main as run


SURFACE = 'album_notes'
HEAD = 'prepared_album_note_generations'
FILES = 'prepared_album_note_files'
MAX_SOURCE_BYTES = album_note_outputs.MAX_NOTES * recommendation_outputs.MAX_BYTES


def _unused(*args):
    raise MigrationError('Album-note preparation requires its bounded file worker')


DEFINITION = Job('album_notes', 79, 83, {'rec_proposals': ('surface',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        generation INTEGER PRIMARY KEY CHECK(generation>=0),
        source_present INTEGER NOT NULL CHECK(source_present IN (0,1)),
        filename TEXT, digest TEXT CHECK(digest IS NULL OR length(digest)=64),
        byte_size INTEGER CHECK(byte_size BETWEEN 1 AND {MAX_SOURCE_BYTES}),
        built_at REAL, notes_count INTEGER NOT NULL CHECK(notes_count BETWEEN 0 AND 128),
        CHECK((filename IS NULL)=(digest IS NULL)),
        CHECK((filename IS NULL)=(byte_size IS NULL)),
        CHECK(source_present=(filename IS NOT NULL)),
        CHECK(source_present=1 OR (built_at IS NULL AND notes_count=0)))'''),
    (FILES, f'''CREATE TABLE {FILES} (
        generation INTEGER NOT NULL REFERENCES {HEAD}(generation),
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 127),
        browse_id TEXT NOT NULL, album_provider_id TEXT, unresolved_reason TEXT,
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 1048576),
        PRIMARY KEY(generation,position), UNIQUE(generation,browse_id),
        CHECK((album_provider_id IS NULL)=(unresolved_reason IS NOT NULL))) WITHOUT ROWID'''),
), _unused, _unused, dependencies=('album_provider_ids',),
    filters={'rec_proposals': "{prefix}surface='album_notes'"})


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Album-note preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'album-notes'


def read_original(row):
    if row['filename'] is None:
        return None
    with Path(row['filename']).open('rb') as stream:
        encoded = stream.read(row['byte_size'] + 1)
    if (len(encoded) != row['byte_size'] or hashlib.sha256(encoded).hexdigest() != row['digest']
            or Path(row['filename']).stem != row['digest']):
        raise MigrationError('Prepared album-note original does not match its digest')
    return encoded.decode('utf-8')


def _decode(source):
    if source is None:
        return None, ()
    encoded, at = bytes(source[0]), source[1]
    if len(encoded) > MAX_SOURCE_BYTES:
        raise MigrationError('Album-note proposal exceeds its preparation byte budget')
    if at is not None:
        finite(at)
    value = recommendation_outputs.decode_json(encoded)
    if not isinstance(value, dict) or len(value) > album_note_outputs.MAX_NOTES:
        raise MigrationError('Album notes must be a bounded release map')
    for browse, note in value.items():
        if not isinstance(browse, str) or not browse or not isinstance(note, dict):
            raise MigrationError('Album note output requires a release ID and an object')
    return encoded, tuple(value.items())


class AlbumNotesJob:
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
            conn.execute('BEGIN')
            DEFINITION._version(conn)
            DEFINITION.validate(conn)
            current = self.progress(conn)
            if current is None:
                raise MigrationError('Preparation is not initialized')
            if current.status == 'failed' and not retry:
                raise MigrationError('Repair the input and retry explicitly: ' + current.error)
            if current.status == 'prepared':
                return current
            source = conn.execute('SELECT substr(CAST(payload AS BLOB),1,?),built_at '
                                  'FROM main.rec_proposals WHERE surface=?',
                                  (MAX_SOURCE_BYTES + 1, SURFACE)).fetchone()
            generation = current.input_revision
            head = conn.execute(f'SELECT 1 FROM {HEAD} WHERE generation=?', (generation,)).fetchone()
            cursor = json.loads(current.cursor)[1] if head and current.cursor else 0
            conn.rollback()
            encoded, notes = _decode(source)
            root = directory(conn)
            filename = digest = None
            if encoded is not None:
                digest = hashlib.sha256(encoded).hexdigest()
                filename = _publish_encoded(root / 'originals', encoded, digest)
            end = min(cursor + batch_size, len(notes))
            files = []
            for browse, note in notes[cursor:end]:
                output = recommendation_outputs.encode(SURFACE, browse, note)
                files.append((browse, *recommendation_outputs.publish(root / 'outputs', output)))
            conn.execute('BEGIN IMMEDIATE')
            if self.progress(conn) != current:
                return self.progress(conn)
            if not head:
                conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?,?)',
                    (generation, int(source is not None), filename, digest,
                     len(encoded) if encoded is not None else None, source[1] if source else None, len(notes)))
            for position, (browse, filename, digest, size) in enumerate(files, cursor):
                reference, reason = album_note_requests._reference(conn, browse)
                conn.execute(f'INSERT INTO {FILES} VALUES (?,?,?,?,?,?,?,?)',
                             (generation, position, browse, reference, reason, filename, digest, size))
            changes = conn.execute(f'SELECT owner,source_key FROM {DEFINITION.changes} '
                                   'ORDER BY owner,source_key LIMIT ?', (batch_size,)).fetchall()
            conn.executemany(f'DELETE FROM {DEFINITION.changes} WHERE owner=? AND source_key=?', changes)
            complete = end == len(notes) and not conn.execute(
                f'SELECT 1 FROM {DEFINITION.changes} LIMIT 1').fetchone()
            conn.execute(f'UPDATE {self.state} SET status=?,owner=?,cursor=?,processed=processed+?,error=NULL,'
                         'prepared_revision=CASE WHEN ? THEN input_revision END,updated_at=? WHERE id=1',
                ('prepared' if complete else 'copying', None if complete else 'rec_proposals',
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


JOB = AlbumNotesJob()


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
