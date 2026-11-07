"""Checkpoint retained road-trip choices without replacing their live draft."""
from dataclasses import dataclass, replace
import hashlib
import json
import math
from itertools import groupby
from pathlib import Path

from .. import recommendation_outputs as files
from ..activity import check_cancellable_work
from ..schema_contract import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 32768
MAX_PARTS = 256
PART_CHARACTERS = 64 * 1024
HEAD = 'prepared_road_trip_draft_generations'
PARTS = 'prepared_road_trip_draft_original_parts'
ITEMS = 'prepared_road_trip_draft_entries'
CURRENT = 'prepared_road_trip_draft_heads'


def _unused(*args):
    raise MigrationError('Road-trip draft preparation requires its bounded file worker')


DEFINITION = Job('road_trip_drafts', 79, 93, {'road_trip_drafts': ('recipe_id',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        recipe_id INTEGER NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {MAX_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND {MAX_PARTS}),
        item_count INTEGER NOT NULL CHECK(item_count BETWEEN 0 AND {MAX_ITEMS}),
        updated_at REAL NOT NULL CHECK(abs(updated_at)<=1.7976931348623157e308),
        source_revision TEXT, revision_kind TEXT NOT NULL CHECK(revision_kind IN ('recorded','missing','null','blank','invalid')),
        building INTEGER NOT NULL CHECK(building IN (0,1)),
        account_id INTEGER CHECK(account_id IS NULL), legacy_author_unknown INTEGER NOT NULL CHECK(legacy_author_unknown=1),
        CHECK((revision_kind='recorded')=(source_revision IS NOT NULL)),
        PRIMARY KEY(recipe_id,generation)) WITHOUT ROWID'''),
    (PARTS, f'''CREATE TABLE {PARTS} (
        recipe_id INTEGER NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_PARTS - 1}),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(recipe_id,generation,position), FOREIGN KEY(recipe_id,generation) REFERENCES {HEAD}(recipe_id,generation)) WITHOUT ROWID'''),
    (ITEMS, f'''CREATE TABLE {ITEMS} (
        recipe_id INTEGER NOT NULL, generation INTEGER NOT NULL,
        selection_state TEXT NOT NULL CHECK(selection_state IN ('picked','excluded')),
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_ITEMS - 1}), video_id TEXT NOT NULL CHECK(length(video_id)>0),
        entry_id INTEGER, recording_id INTEGER, unresolved_reason TEXT,
        CHECK((entry_id IS NULL AND recording_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='unadmitted_video')
           OR (entry_id IS NOT NULL AND recording_id IS NOT NULL AND unresolved_reason IS NULL)),
        PRIMARY KEY(recipe_id,generation,selection_state,position),
        FOREIGN KEY(recipe_id,generation) REFERENCES {HEAD}(recipe_id,generation)) WITHOUT ROWID'''),
    (CURRENT, f'''CREATE TABLE {CURRENT} (
        recipe_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL,
        FOREIGN KEY(recipe_id,generation) REFERENCES {HEAD}(recipe_id,generation)) WITHOUT ROWID'''),
), _unused, _unused, dependencies=('catalog_entries',), projections={'road_trip_drafts':
    ('recipe_id', f"COALESCE(substr(CAST(state AS BLOB),1,{MAX_BYTES + 1}),X'') AS state", 'updated_at')})


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Road-trip draft preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'road-trip-drafts'


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {PARTS} '
        'WHERE recipe_id=? AND generation=? ORDER BY position LIMIT ?',
        (row['recipe_id'], row['generation'], MAX_PARTS + 1)).fetchall()
    return read_original_parts(row, parts)


def read_original_parts(row, parts):
    """Read a detached original manifest without accessing SQLite."""
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared road-trip draft original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface='road_trip_draft', chunk_key=f'{row["recipe_id"]}:{part[0]}') for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared road-trip draft differs from its original digest')
    return raw


@dataclass(frozen=True)
class Plan:
    digest: str
    size: int
    part_count: int
    item_count: int
    source_revision: str | None
    revision_kind: str
    building: bool
    units: tuple


def describe(text, updated_at):
    raw = text.encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise MigrationError('Road-trip draft exceeds its preparation byte budget')
    value = files.decode_json(text)
    if not isinstance(value, dict):
        raise ValueError('Legacy road-trip draft must be an object')
    if type(updated_at) not in (int, float) or not math.isfinite(updated_at):
        raise ValueError('Road-trip draft requires a finite source update time')
    items = []
    for field, selection in (('picks', 'picked'), ('banned', 'excluded')):
        values = value.get(field, [])
        if not isinstance(values, list) or len(values) + len(items) > MAX_ITEMS:
            raise ValueError('Road-trip draft exceeds its ordered selection budget')
        for position, video in enumerate(values):
            if not isinstance(video, str) or not video:
                raise ValueError('A draft selection requires an exact upload ID')
            items.append(('item', selection, position, video))
    revision = value.get('revision')
    kind = ('missing' if 'revision' not in value else 'null' if revision is None else
            'blank' if revision == '' else 'recorded' if isinstance(revision, str) else 'invalid')
    return Plan(hashlib.sha256(raw).hexdigest(), len(raw), (len(text)+PART_CHARACTERS-1)//PART_CHARACTERS,
                len(items), revision if kind == 'recorded' else None, kind, bool(value.get('building')), tuple(items))


def prepare(conn, row):
    text = bytes(row['state']).decode('utf-8')
    plan = describe(text, row['updated_at'])
    durable = [('file', position, files.publish(directory(conn), files.encode('road_trip_draft',
        f'{row["recipe_id"]}:{position}', text[start:start + PART_CHARACTERS])))
        for position, start in enumerate(range(0, len(text), PART_CHARACTERS))]
    if len(durable) > MAX_PARTS:
        raise MigrationError('Road-trip draft exceeds its original part budget')
    return replace(plan, units=tuple(durable)+plan.units)


def begin(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?,?,?,?,?,NULL,1)',
        (row['recipe_id'], generation, plan.digest, plan.size, plan.part_count, plan.item_count,
         row['updated_at'], plan.source_revision, plan.revision_kind, int(plan.building)))


def copy(conn, row, generation, position, unit):
    if unit[0] == 'file':
        conn.execute(f'INSERT INTO {PARTS} VALUES (?,?,?,?,?,?)', (row['recipe_id'], generation, unit[1], *unit[2]))
    else:
        _, selection, rank, video = unit
        entry = conn.execute('SELECT id,recording_id FROM catalog_entries WHERE video_id=?', (video,)).fetchone()
        conn.execute(f'INSERT INTO {ITEMS} VALUES (?,?,?,?,?,?,?,?)',
            (row['recipe_id'], generation, selection, rank, video, entry[0] if entry else None,
             entry[1] if entry else None, None if entry else 'unadmitted_video'))


def copy_batch(conn, row, generation, selected):
    """Keep all ordered occurrences while resolving repeated uploads once per slice."""
    entries = {}
    for kind, group in groupby(selected, key=lambda pair: pair[1][0]):
        values = []
        for _, unit in group:
            check_cancellable_work()
            if kind == 'file':
                values.append((row['recipe_id'], generation, unit[1], *unit[2]))
            else:
                _, selection, rank, video = unit
                if video not in entries:
                    entries[video] = conn.execute(
                        'SELECT id,recording_id FROM catalog_entries WHERE video_id=?', (video,)).fetchone()
                entry = entries[video]
                values.append((row['recipe_id'], generation, selection, rank, video,
                    entry[0] if entry else None, entry[1] if entry else None,
                    None if entry else 'unadmitted_video'))
        table, width = (PARTS, 6) if kind == 'file' else (ITEMS, 8)
        conn.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in range(width))})', values)


def complete(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {CURRENT} VALUES (?,?) ON CONFLICT(recipe_id) DO UPDATE SET generation=excluded.generation',
                 (row['recipe_id'], generation))


def delete(conn, owner, key):
    conn.execute(f'DELETE FROM {CURRENT} WHERE recipe_id=?', (json.loads(key)[0],))


JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete, copy_batch=copy_batch)


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
