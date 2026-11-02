"""Prepare ordered recipe members in bounded batches, retaining all source lists."""
import hashlib
import json
from pathlib import Path
from .. import fao
import time

from .. import artist_keys, genre_identity
from ..schema_contract import MigrationError
from ..policy_documents import _publish_encoded
from .rows import Job, main as run


MAX_BYTES = 8 * 1024 * 1024
HEAD = 'prepared_recipe_generations'
ITEMS = 'prepared_recipe_members'
FIELDS = ('artists', 'genres', 'blacklist_genres')
ROLES = ('artist', 'include', 'exclude')


def _unused(*args):
    raise MigrationError('Recipe preparation requires its bounded member worker')


DEFINITION = Job('recipe_memberships', 79, 88, {'road_trip_recipes': ('id',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        recipe_id INTEGER NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {2 * MAX_BYTES + 4096}),
        members_count INTEGER NOT NULL CHECK(members_count>=0),
        PRIMARY KEY(recipe_id,generation)) WITHOUT ROWID'''),
    (ITEMS, f'''CREATE TABLE {ITEMS} (
        recipe_id INTEGER NOT NULL, generation INTEGER NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('artist','include','exclude')),
        position INTEGER NOT NULL CHECK(position>=0), raw_label TEXT NOT NULL,
        artist_id INTEGER, genre_key TEXT,
        unresolved_reason TEXT CHECK(unresolved_reason IN ('unknown_or_ambiguous_artist','unknown_genre','ambiguous_genre')),
        duplicate_position INTEGER CHECK(duplicate_position>=0 AND duplicate_position<position),
        PRIMARY KEY(recipe_id,generation,role,position),
        FOREIGN KEY(recipe_id,generation) REFERENCES {HEAD}(recipe_id,generation),
        CHECK((role='artist' AND genre_key IS NULL AND ((artist_id IS NULL)=(unresolved_reason IS NOT NULL)))
           OR (role<>'artist' AND artist_id IS NULL AND ((genre_key IS NULL)=(unresolved_reason IS NOT NULL))))) WITHOUT ROWID'''),
    ('ix_prepared_recipe_artist', f'CREATE INDEX ix_prepared_recipe_artist ON {ITEMS}(recipe_id,generation,role,artist_id,position)'),
    ('ix_prepared_recipe_genre', f'CREATE INDEX ix_prepared_recipe_genre ON {ITEMS}(recipe_id,generation,role,genre_key,position)'),
    ('prepared_recipe_heads', f'''CREATE TABLE prepared_recipe_heads (
        recipe_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL,
        FOREIGN KEY(recipe_id,generation) REFERENCES {HEAD}(recipe_id,generation))'''),
), _unused, _unused, dependencies=('artists', 'artist_aliases', 'artist_redirects', 'genre_catalog', 'genre_alias'))


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Recipe preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'recipe-memberships'


def read_original(row):
    with fao.open_reader(row['filename']) as stream:
        encoded = stream.read(row['byte_size'] + 1)
    if (len(encoded) != row['byte_size'] or hashlib.sha256(encoded).hexdigest() != row['digest']
            or Path(row['filename']).stem != row['digest']):
        raise MigrationError('Prepared recipe original does not match its digest')
    return json.loads(encoded)


def _source(conn, recipe_id):
    columns = ','.join('substr(CAST('+name+' AS BLOB),1,?) AS '+name for name in FIELDS)
    return conn.execute('SELECT '+columns+' FROM main.road_trip_recipes WHERE id=?',
                        (*([MAX_BYTES + 1] * len(FIELDS)), recipe_id)).fetchone()


def _select(conn, current):
    if current.owner is None:
        row = conn.execute(f'SELECT source_key FROM {DEFINITION.changes} ORDER BY source_key LIMIT 1').fetchone()
        return json.loads(row[0])[0] if row else None
    cursor = json.loads(current.cursor) if current.cursor else None
    if cursor and len(cursor) == 3:
        return cursor[0]
    highwater = conn.execute(f'SELECT highwater FROM {DEFINITION.sources}').fetchone()[0]
    if highwater is None:
        return None
    query = 'SELECT id FROM main.road_trip_recipes WHERE id<=?'
    values = [json.loads(highwater)[0]]
    if cursor:
        query += ' AND id>?'
        values.append(cursor[0])
    row = conn.execute(query+' ORDER BY id LIMIT 1', values).fetchone()
    return row[0] if row else None


def _documents(row):
    if any(value is None for value in row):
        raise MigrationError('Recipe selections require lists of nonblank labels')
    if sum(len(value) for value in row) > MAX_BYTES:
        raise MigrationError('Recipe selections exceed their preparation byte budget')
    original = {name: bytes(row[name]).decode('utf-8') for name in FIELDS}
    lists = [json.loads(original[name]) for name in FIELDS]
    if any(not isinstance(values, list) or any(not isinstance(label, str) or not label.strip()
           for label in values) for values in lists):
        raise MigrationError('Recipe selections require lists of nonblank labels')
    encoded = json.dumps(original, ensure_ascii=False, separators=(',', ':')).encode()
    return encoded, lists


def describe(conn, role, position, label):
    artist = artist_keys.lookup(conn, label, exact=True) if role == 'artist' else None
    genre, reason = genre_identity.resolve(conn, label) if role != 'artist' else (None, None)
    if role == 'artist' and artist is None:
        reason = 'unknown_or_ambiguous_artist'
    return role, position, label, artist, genre, reason


def _copy(conn, recipe_id, generation, role, position, label, resolved=None):
    _, _, _, artist, genre, reason = resolved or describe(conn, role, position, label)
    column, identity = ('artist_id', artist) if role == 'artist' else ('genre_key', genre)
    prior = conn.execute(f'SELECT position FROM {ITEMS} WHERE recipe_id=? AND generation=? AND role=? '
                         f'AND {column}=? ORDER BY position LIMIT 1', (recipe_id, generation, role, identity)).fetchone()
    conn.execute(f'INSERT INTO {ITEMS} VALUES (?,?,?,?,?,?,?,?,?)',
                 (recipe_id, generation, role, position, label, artist, genre, reason, prior[0] if prior else None))


class RecipeJob:
    definition = DEFINITION
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
            recipe_id = _select(conn, current)
            source = _source(conn, recipe_id) if recipe_id is not None else None
            generation = current.input_revision
            cursor = json.loads(current.cursor) if current.cursor else None
            start = cursor[2] if cursor and len(cursor) == 3 and cursor[:2] == [recipe_id, generation] else 0
            existing = conn.execute(f'SELECT 1 FROM {HEAD} WHERE recipe_id=? AND generation=?',
                                    (recipe_id, generation)).fetchone()
            conn.rollback()
            encoded, lists = _documents(source) if source is not None else (None, [])
            total = sum(map(len, lists))
            end = min(start + batch_size, total)
            if encoded is not None:
                digest = hashlib.sha256(encoded).hexdigest()
                filename = _publish_encoded(directory(conn), encoded, digest)
            planned, offset = [], 0
            for role, values in zip(ROLES, lists):
                for position in range(max(0, start-offset), min(len(values), end-offset)):
                    planned.append(describe(conn, role, position, values[position]))
                offset += len(values)
            conn.execute('BEGIN IMMEDIATE')
            if self.progress(conn) != current:
                return self.progress(conn)
            if source is not None:
                if not existing:
                    conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?)',
                                 (recipe_id, generation, filename, digest, len(encoded), total))
                for member in planned:
                    _copy(conn, recipe_id, generation, *member[:3], member)
                if end == total:
                    conn.execute('INSERT INTO prepared_recipe_heads VALUES (?,?) '
                                 'ON CONFLICT(recipe_id) DO UPDATE SET generation=excluded.generation', (recipe_id, generation))
            elif recipe_id is not None:
                conn.execute('DELETE FROM prepared_recipe_heads WHERE recipe_id=?', (recipe_id,))
            owner, checkpoint = current.owner, None
            if recipe_id is not None:
                if end == total:
                    conn.execute(f'DELETE FROM {DEFINITION.changes} WHERE source_key=json_array(?)', (recipe_id,))
                    checkpoint = json.dumps([recipe_id]) if owner is not None else None
                else:
                    checkpoint = json.dumps([recipe_id, generation, end])
            else:
                owner = None
            complete = owner is None and not conn.execute(f'SELECT 1 FROM {DEFINITION.changes} LIMIT 1').fetchone()
            conn.execute(f'UPDATE {self.state} SET status=?,owner=?,cursor=?,processed=processed+?,error=NULL,'
                         'prepared_revision=CASE WHEN ? THEN input_revision END,updated_at=? WHERE id=1',
                ('prepared' if complete else 'copying' if owner else 'reconciling', owner, checkpoint,
                 end-start, int(complete), clock()))
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


JOB = RecipeJob()


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
