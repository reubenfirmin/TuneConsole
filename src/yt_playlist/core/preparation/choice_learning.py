"""Prepare saved choice-learning inputs in individually checkpointed relations."""
from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .. import choice_learning_state as learning, recommendation_outputs as files
from ..migrations.runner import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


PREFIX = 'prepared_'
PUBLICATIONS = PREFIX + 'choice_learning_publications'
CURRENT = PREFIX + 'choice_learning_heads'
PARTS = 'prepared_choice_learning_original_parts'
REFERENCES = 'prepared_choice_learning_references'
PART_CHARACTERS = 64 * 1024
MAX_PARTS = 512
TABLES = ('choice_learning_publications', 'choice_learning_parameters', 'choice_learning_exclusions',
          'choice_mode_estimates', 'choice_group_estimates', 'choice_group_opportunities',
          'choice_interest_contexts', 'choice_interest_features', 'choice_interest_tracks',
          'choice_album_interests', 'choice_album_track_days')


def _contract():
    # This SQL is also part of the finite current bootstrap. Admission may
    # import this module on a fresh database; never invoke an upgrade provider.
    definition = Path(__file__).parents[1] / 'migrations/schema_choice_learning.sql'
    pending = ''
    for line in definition.read_text().splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete choice learning preparation contract')


def definitions():
    # Reuse the finite numerical contracts in a separate namespace. These
    # are observations of mutable reference owners, not new retention pins.
    names = re.compile(r'\b(' + '|'.join(TABLES) + r')\b')
    for sql in _contract():
        match = re.match(r'\s*CREATE TABLE (\w+)\s*\(', sql)
        if match is None or match[1] not in TABLES:
            continue
        for target in ('taste_modes(mode_id)', 'recordings(id)', 'albums(id)'):
            sql = sql.replace(' REFERENCES ' + target, '')
        if match[1] == 'choice_learning_publications':
            sql = sql.replace('UNIQUE(id,surface)',
                f'''input_revision INTEGER NOT NULL CHECK(input_revision>=0),
    raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64),
    raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {learning.MAX_BYTES}),
    part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND {MAX_PARTS}),
    UNIQUE(surface,input_revision), UNIQUE(id,surface)''')
        sql = names.sub(lambda item: PREFIX + item[0], sql)
        yield PREFIX + match[1], sql.strip().removesuffix(';')
    yield CURRENT, f'''CREATE TABLE {CURRENT} (
        surface TEXT PRIMARY KEY, publication_id INTEGER NOT NULL UNIQUE,
        FOREIGN KEY(publication_id,surface) REFERENCES {PUBLICATIONS}(id,surface)) WITHOUT ROWID'''
    yield PARTS, f'''CREATE TABLE {PARTS} (
        publication_id INTEGER NOT NULL REFERENCES {PUBLICATIONS}(id),
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_PARTS - 1}),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(publication_id,position)) WITHOUT ROWID'''
    yield REFERENCES, f'''CREATE TABLE {REFERENCES} (
        publication_id INTEGER NOT NULL REFERENCES {PUBLICATIONS}(id), position INTEGER NOT NULL,
        field TEXT NOT NULL CHECK(field IN ('generation','version','shared_from')),
        estimate_position INTEGER, raw_key TEXT NOT NULL, expected_group TEXT,
        generation_id INTEGER, group_version_id INTEGER, unresolved_reason TEXT,
        CHECK((unresolved_reason IS NULL)=(generation_id IS NOT NULL OR group_version_id IS NOT NULL)),
        PRIMARY KEY(publication_id,position)) WITHOUT ROWID'''


def _unused(*args):
    raise MigrationError('Choice learning preparation requires its bounded document worker')


DEFINITION = Job('choice_learning', 79, 92, {'rec_proposals': ('surface',)}, tuple(definitions()), _unused, _unused,
    dependencies=('taste_modes', 'recordings', 'album_provider_ids', 'group_generations',
                  'group_publications', 'taste_groups', 'group_versions', 'group_version_aliases', 'generation_groups'),
    filters={'rec_proposals': '{prefix}surface IN (' + ','.join(repr(surface) for surface in sorted(learning.SURFACES)) + ')'},
    projections={'rec_proposals': ('surface', f"COALESCE(substr(CAST(payload AS BLOB),1,{learning.MAX_BYTES + 1}),X'') AS payload", 'built_at')})


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Choice learning preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'choice-learning'


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {PARTS} '
        'WHERE publication_id=? ORDER BY position LIMIT ?', (row['id'], MAX_PARTS + 1)).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared choice learning original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['surface'], chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared choice learning differs from its original digest')
    return raw


class Units:
    """Index existing lists without allocating a tuple for every coordinate."""
    def __init__(self):
        self.ends = []
        self.ranges = []

    def add(self, kind, parent, values):
        if values:
            self.ends.append(len(self) + len(values))
            self.ranges.append((kind, parent, values))

    def __len__(self):
        return self.ends[-1] if self.ends else 0

    def __getitem__(self, position):
        if not 0 <= position < len(self):
            raise IndexError(position)
        index = bisect_right(self.ends, position)
        offset = position - (self.ends[index - 1] if index else 0)
        kind, parent, values = self.ranges[index]
        return kind, parent, offset, values[offset]


@dataclass(frozen=True)
class Plan:
    value: dict
    raw_digest: str
    raw_size: int
    part_count: int
    units: Units


def prepare(conn, row):
    encoded = bytes(row['payload'])
    if len(encoded) > learning.MAX_BYTES:
        raise MigrationError('Choice learning exceeds its preparation byte budget')
    raw = encoded.decode('utf-8')
    value = learning.decode(row['surface'], raw)
    learning._number(row['built_at'])
    parts = [files.publish(directory(conn), files.encode(row['surface'], str(position), raw[start:start + PART_CHARACTERS]))
        for position, start in enumerate(range(0, len(raw), PART_CHARACTERS))]
    if len(parts) > MAX_PARTS:
        raise MigrationError('Choice learning exceeds its original part budget')
    units = Units()
    units.add('file', None, parts)
    for field in ('parameters', 'excluded'):
        units.add(field, None, list(value.get(field, {}).items()))
    if row['surface'] == 'choice_bandit':
        units.add('mode', None, value.get('rows', []))
    elif row['surface'] == 'group_choice_bandit':
        if value.get('generation') is not None:
            units.add('reference', None, [('generation', value['generation'], None, None)])
        for position, estimate in enumerate(value.get('rows', [])):
            units.add('group', position, [estimate])
            units.add('opportunity', position, estimate.get('menus', []))
            for field in ('version', 'shared_from'):
                if estimate.get(field):
                    units.add('reference', position, [(field, estimate[field], estimate.get('group_id') if field == 'version' else None,
                                                       value.get('generation'))])
    for position, context in enumerate(value.get('contexts', [])):
        units.add('context', position, [context])
        if context.get('descriptor_id') is not None:
            units.add('descriptor', position, [context['descriptor_id']])
        units.add('feature', position, context.get('feature') or [])
        units.add('track', position, context.get('keys', []))
    for position, (browse, tracks) in enumerate(value.get('album_track_days', {}).items()):
        units.add('album', position, [browse])
        units.add('day', position, list(tracks.items()))
    return Plan(value, hashlib.sha256(encoded).hexdigest(), len(encoded), len(parts), units)


def begin(conn, row, generation, plan):
    columns = ('surface', 'field_mask', 'built_at', *learning.SCALARS, 'input_revision', 'raw_digest', 'raw_size', 'part_count')
    columns = tuple('schema_version' if name == 'schema' else name for name in columns)
    learning._insert(conn, PUBLICATIONS, columns, (row['surface'], learning._mask(plan.value, learning.HEADER), row['built_at'],
        *(plan.value.get(field) for field in learning.SCALARS), generation, plan.raw_digest, plan.raw_size, plan.part_count))


def reference(conn, identity, position, parent, value):
    field, raw, expected_group, generation = value
    resolved_generation = resolved_version = reason = None
    if field == 'generation':
        row = conn.execute('SELECT g.id FROM group_generations g JOIN group_publications p ON p.generation_id=g.id '
                           'WHERE g.external_key=?', (raw,)).fetchone()
        resolved_generation = row[0] if row else None
        reason = None if row else 'missing_generation'
    else:
        row = conn.execute('SELECT a.group_version_id,g.external_key FROM group_version_aliases a '
            'JOIN group_versions v ON v.id=a.group_version_id JOIN taste_groups g ON g.id=v.group_id '
            'JOIN group_publications p ON p.generation_id=a.first_generation_id WHERE a.external_version=?', (raw,)).fetchone()
        reason = 'missing_version' if row is None else 'wrong_group' if expected_group is not None and row[1] != expected_group else None
        if reason is None and expected_group is not None and generation is not None and not conn.execute(
            'SELECT 1 FROM generation_groups x JOIN group_generations g ON g.id=x.generation_id '
            'JOIN group_publications p ON p.generation_id=g.id WHERE g.external_key=? AND x.external_version=? '
            'AND x.group_version_id=?', (generation, raw, row[0])).fetchone():
            reason = 'wrong_generation'
        resolved_version = row[0] if reason is None else None
    conn.execute(f'INSERT INTO {REFERENCES} VALUES (?,?,?,?,?,?,?,?,?)',
                 (identity, position, field, parent, raw, expected_group, resolved_generation, resolved_version, reason))


def copy(conn, row, generation, position, unit):
    identity = conn.execute(f'SELECT id FROM {PUBLICATIONS} WHERE surface=? AND input_revision=?',
                            (row['surface'], generation)).fetchone()[0]
    kind, parent, offset, value = unit
    if kind == 'file':
        conn.execute(f'INSERT INTO {PARTS} VALUES (?,?,?,?,?)', (identity, offset, *value))
    elif kind in ('parameters', 'excluded'):
        owner = 'choice_learning_parameters' if kind == 'parameters' else 'choice_learning_exclusions'
        conn.execute(f'INSERT INTO {PREFIX + owner} VALUES (?,?,?)', (identity, *value))
    elif kind in ('mode', 'group'):
        fields = learning.MODE if kind == 'mode' else learning.GROUP
        scalar = fields if kind == 'mode' else fields[:-1]
        columns = ('publication_id', 'position', 'field_mask', *scalar)
        values = (identity, offset if kind == 'mode' else parent, learning._mask(value, fields), *(value.get(field) for field in scalar))
        if kind == 'mode':
            mode = conn.execute('SELECT mode_id FROM taste_modes WHERE mode_id=?', (value.get('mode_id'),)).fetchone()
            columns += ('resolved_mode_id', 'resolution_reason')
            values += (mode[0] if mode else None, None if mode else 'missing_mode')
        learning._insert(conn, PREFIX + ('choice_mode_estimates' if kind == 'mode' else 'choice_group_estimates'), columns, values)
    elif kind == 'opportunity':
        conn.execute(f'INSERT INTO {PREFIX}choice_group_opportunities VALUES (?,?,?,?)', (identity, parent, offset, value))
    elif kind == 'reference':
        reference(conn, identity, position, parent, value)
    elif kind == 'context':
        scalar = learning.CONTEXT[:-2]
        album = learning._album(conn, value.get('option_id')) if value.get('format') == 'albums' else None
        learning._insert(conn, PREFIX + 'choice_interest_contexts',
            ('publication_id', 'position', 'field_mask', *scalar, 'feature_null', 'album_id', 'resolution_reason'),
            (identity, parent, learning._mask({k:v for k,v in value.items() if k != 'descriptor_id'}, learning.CONTEXT), *(value.get(field) for field in scalar),
             int(value.get('feature') is None), album, 'missing_album' if value.get('format') == 'albums' and album is None else None))
    elif kind == 'descriptor':
        conn.execute('INSERT INTO prepared_choice_interest_mix_descriptors VALUES (?,?,?)', (identity, parent, value))
    elif kind == 'feature':
        conn.execute(f'INSERT INTO {PREFIX}choice_interest_features VALUES (?,?,?,?)', (identity, parent, offset, value))
    elif kind in ('track', 'day'):
        raw = value if kind == 'track' else value[0]
        recording = learning._recording(conn, raw)
        values = (identity, parent, offset, raw, recording)
        if kind == 'day':
            values += (value[1],)
        values += (None if recording else 'missing_recording',)
        owner = 'choice_interest_tracks' if kind == 'track' else 'choice_album_track_days'
        conn.execute(f'INSERT INTO {PREFIX + owner} VALUES ({",".join("?" for _ in values)})', values)
    elif kind == 'album':
        album = learning._album(conn, value)
        conn.execute(f'INSERT INTO {PREFIX}choice_album_interests VALUES (?,?,?,?,?)',
                     (identity, parent, value, album, None if album else 'missing_album'))
    else:
        raise MigrationError('Unknown choice learning preparation unit: ' + kind)


def complete(conn, row, generation, plan):
    identity = conn.execute(f'SELECT id FROM {PUBLICATIONS} WHERE surface=? AND input_revision=?',
                            (row['surface'], generation)).fetchone()[0]
    conn.execute(f'INSERT INTO {CURRENT} VALUES (?,?) ON CONFLICT(surface) DO UPDATE '
                 'SET publication_id=excluded.publication_id', (row['surface'], identity))


def delete(conn, owner, key):
    conn.execute(f'DELETE FROM {CURRENT} WHERE surface=?', (json.loads(key)[0],))


JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete)


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
