"""Prepare enabled labels and disabled-default meaning without retiring choices."""
import argparse
import hashlib
import json
from pathlib import Path
from .. import fao
import time

from .. import genre_identity
from ..schema_contract import MigrationError
from ..policy_documents import _publish_encoded
from ...util import genre_catalog
from .rows import Job, main as run


MAX_BYTES = 8 * 1024 * 1024
MAX_SEEDED_BYTES = 16 * 1024
HEAD = 'prepared_genre_default_generations'
ITEMS = 'prepared_genre_default_items'


def resolve(conn, label):
    genre, reason = genre_identity.resolve(conn, label)
    return genre, 'blank' if label == '' else 'resolved' if genre is not None else reason


def describe_choice(conn, key, row):
    label = row['name']
    if not isinstance(label, str) or not label.strip() or len(label.encode()) > 1024:
        raise ValueError('An enabled genre requires 1..1024 bytes of nonblank text')
    genre, disposition = resolve(conn, label)
    proposed = None
    if disposition == 'unknown_genre':
        proposed = genre_catalog.normalize(label)
        if not proposed:
            raise ValueError('An enabled custom genre requires a nonempty identity')
        disposition = 'requires_custom'
    snapshot = json.dumps([label, genre, proposed, disposition], ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    return (key, label, genre, proposed, disposition, digest), snapshot


def copy_choice(conn, owner, key, row):
    values, snapshot = describe_choice(conn, key, row)
    digest = values[-1]
    conn.execute('INSERT INTO prepared_genre_choice_originals VALUES (?,?,?) '
                 'ON CONFLICT(source_key,digest) DO NOTHING', (key, digest, snapshot))
    conn.execute('INSERT INTO prepared_genre_choices VALUES (?,?,?,?,?,?) '
                 'ON CONFLICT(source_key) DO UPDATE SET raw_label=excluded.raw_label,genre_key=excluded.genre_key,'
                 'proposed_key=excluded.proposed_key,disposition=excluded.disposition,source_digest=excluded.source_digest',
                 values)


def delete_choice(conn, owner, key):
    conn.execute('DELETE FROM prepared_genre_choices WHERE source_key=?', (key,))


CHOICES = Job('genre_choices', 79, 84, {'genre_whitelist': ('name',)}, (
    ('prepared_genre_choice_originals', '''CREATE TABLE prepared_genre_choice_originals (
        source_key TEXT NOT NULL CHECK(json_valid(source_key)), digest TEXT NOT NULL CHECK(length(digest)=64),
        source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)), PRIMARY KEY(source_key,digest)) WITHOUT ROWID'''),
    ('prepared_genre_choices', '''CREATE TABLE prepared_genre_choices (
        source_key TEXT PRIMARY KEY, raw_label TEXT NOT NULL, genre_key TEXT, proposed_key TEXT,
        disposition TEXT NOT NULL CHECK(disposition IN ('resolved','requires_custom','ambiguous_genre')),
        source_digest TEXT NOT NULL,
        FOREIGN KEY(source_key,source_digest) REFERENCES prepared_genre_choice_originals(source_key,digest),
        CHECK((genre_key IS NOT NULL)=(disposition='resolved')),
        CHECK((proposed_key IS NOT NULL)=(disposition='requires_custom'))) WITHOUT ROWID'''),
), copy_choice, delete_choice, dependencies=('genre_catalog', 'genre_alias'))


def _unused(*args):
    raise MigrationError('Genre-default preparation requires its bounded list worker')


DEFAULT_DEFINITION = Job('genre_defaults', 79, 84, {'settings': ('key',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        generation INTEGER PRIMARY KEY CHECK(generation>=0),
        baseline_origin TEXT NOT NULL CHECK(baseline_origin IN ('saved','legacy_defaults')),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {MAX_BYTES}),
        seeded_present INTEGER NOT NULL CHECK(seeded_present IN (0,1)), seeded_value TEXT,
        apply_defaults INTEGER NOT NULL CHECK(apply_defaults IN (0,1)),
        labels_count INTEGER NOT NULL CHECK(labels_count>=0),
        CHECK(seeded_present=1 OR seeded_value IS NULL))'''),
    (ITEMS, f'''CREATE TABLE {ITEMS} (
        generation INTEGER NOT NULL REFERENCES {HEAD}(generation), position INTEGER NOT NULL CHECK(position>=0),
        raw_label TEXT NOT NULL, genre_key TEXT,
        disposition TEXT NOT NULL CHECK(disposition IN ('resolved','blank','unknown_genre','ambiguous_genre')),
        proposed_enabled INTEGER CHECK(proposed_enabled=0), PRIMARY KEY(generation,position),
        CHECK((genre_key IS NOT NULL)=(disposition='resolved'))) WITHOUT ROWID'''),
), _unused, _unused, dependencies=('genre_catalog', 'genre_alias', 'genre_whitelist'),
    filters={'settings': "{prefix}key IN ('genre_defaults','genres_seeded')"})


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Genre-default preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'genre-defaults'


def read_baseline(row):
    with fao.read(row['filename']) as stream:
        encoded = stream.read(row['byte_size'] + 1)
    if (len(encoded) != row['byte_size'] or hashlib.sha256(encoded).hexdigest() != row['digest']
            or Path(row['filename']).stem != row['digest']):
        raise MigrationError('Prepared genre baseline does not match its digest')
    return encoded.decode('utf-8')


class DefaultsJob:
    name = DEFAULT_DEFINITION.name
    state = DEFAULT_DEFINITION.state
    install = staticmethod(DEFAULT_DEFINITION.install)
    progress = staticmethod(DEFAULT_DEFINITION.progress)

    def step(self, conn, *, batch_size=128, retry=False, clock=time.time):
        if type(batch_size) is not int or not 1 <= batch_size <= 512:
            raise ValueError('Preparation batch size must be from 1 through 512')
        if conn.in_transaction:
            raise MigrationError('Preparation requires an idle connection')
        current = None
        try:
            conn.execute('BEGIN')
            DEFAULT_DEFINITION._version(conn)
            DEFAULT_DEFINITION.validate(conn)
            current = self.progress(conn)
            if current is None:
                raise MigrationError('Preparation is not initialized')
            if current.status == 'failed' and not retry:
                raise MigrationError('Repair the input and retry explicitly: ' + current.error)
            if current.status == 'prepared':
                return current
            source = conn.execute("SELECT substr(CAST(value AS BLOB),1,?) FROM main.settings WHERE key='genre_defaults'",
                                  (MAX_BYTES + 1,)).fetchone()
            seeded = conn.execute("SELECT COALESCE(substr(CAST(value AS BLOB),1,?),X''),value IS NULL "
                                  "FROM main.settings WHERE key='genres_seeded'",
                                  (MAX_SEEDED_BYTES + 1,)).fetchone()
            enabled = conn.execute('SELECT 1 FROM main.genre_whitelist LIMIT 1').fetchone() is not None
            generation = current.input_revision
            head = conn.execute(f'SELECT 1 FROM {HEAD} WHERE generation=?', (generation,)).fetchone()
            cursor = json.loads(current.cursor)[1] if head and current.cursor else 0
            conn.rollback()
            if source is not None and source[0] is None:
                raise MigrationError('Saved genre defaults require a list of labels')
            encoded = bytes(source[0]) if source else json.dumps(genre_catalog.legacy_names()).encode()
            if len(encoded) > MAX_BYTES:
                raise MigrationError('Genre baseline exceeds its preparation byte budget')
            baseline = json.loads(encoded)
            if not isinstance(baseline, list) or any(not isinstance(label, str) for label in baseline):
                raise MigrationError('Saved genre defaults require a list of labels')
            seeded_raw = bytes(seeded[0]) if seeded and not seeded[1] else None
            if seeded_raw is not None and len(seeded_raw) > MAX_SEEDED_BYTES:
                raise MigrationError('Genre seed marker exceeds its preparation byte budget')
            seeded_value = seeded_raw.decode('utf-8') if seeded_raw is not None else None
            apply_defaults = bool(seeded_value) or enabled
            digest = hashlib.sha256(encoded).hexdigest()
            filename = _publish_encoded(directory(conn), encoded, digest)
            end = min(cursor + batch_size, len(baseline))
            planned = [(generation, position, baseline[position], *resolve(conn, baseline[position]),
                        0 if apply_defaults else None) for position in range(cursor, end)]
            conn.execute('BEGIN IMMEDIATE')
            if self.progress(conn) != current:
                return self.progress(conn)
            if not head:
                conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?,?,?,?)',
                             (generation, 'saved' if source else 'legacy_defaults', filename, digest, len(encoded),
                              int(seeded is not None), seeded_value, int(apply_defaults), len(baseline)))
            conn.executemany(f'INSERT INTO {ITEMS} VALUES (?,?,?,?,?,?)', planned)
            changes = conn.execute(f'SELECT owner,source_key FROM {DEFAULT_DEFINITION.changes} '
                                   'ORDER BY owner,source_key LIMIT ?', (batch_size,)).fetchall()
            conn.executemany(f'DELETE FROM {DEFAULT_DEFINITION.changes} WHERE owner=? AND source_key=?', changes)
            complete = end == len(baseline) and not conn.execute(
                f'SELECT 1 FROM {DEFAULT_DEFINITION.changes} LIMIT 1').fetchone()
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


DEFAULTS = DefaultsJob()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('enabled', 'defaults'))
    args, remaining = parser.parse_known_args(argv)
    return run(CHOICES if args.job == 'enabled' else DEFAULTS, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
