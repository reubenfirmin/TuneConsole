"""Prepare exact Home display files, policy references and installed roots."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .. import home_outputs as outputs, recommendation_outputs as files
from ..migrations.runner import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


HEAD = 'prepared_home_output_generations'
PARTS = 'prepared_home_output_parts'
POLICIES = 'prepared_home_output_policies'
CURRENT = 'prepared_home_output_heads'
ROOTS = 'prepared_home_output_roots'
ORIGINALS = 'prepared_home_root_originals'
ROOT_BYTES = 256 * 1024
ROOT_KEYS = {
    'group_evidence_owners': ('kind', 'external_key'),
    'group_evidence_generations': ('kind', 'external_key', 'generation_id'),
    'group_evidence_versions': ('kind', 'external_key', 'external_version'),
    'unresolved_group_evidence': ('kind', 'external_key', 'reference_kind', 'raw_key'),
    'unresolved_group_contexts': ('kind', 'external_key'),
    'recommendation_generation_refs': ('kind', 'external_key', 'generation_id'),
}
ROOT_COLUMNS = ('reference_digest', 'created_at', 'generation_id', 'external_version', 'group_version_id',
                'reference_kind', 'raw_key', 'reason', 'source_context')
ROOT_FIELDS = {
    'group_evidence_owners': ('reference_digest', 'created_at'),
    'group_evidence_generations': ('generation_id',),
    'group_evidence_versions': ('external_version', 'group_version_id'),
    'unresolved_group_evidence': ('reference_kind', 'raw_key', 'reason'),
    'unresolved_group_contexts': ('source_context', 'reason'),
    'recommendation_generation_refs': ('generation_id',),
}
TEXT_FIELDS = ('kind', 'external_key', 'reference_digest', 'external_version', 'reference_kind', 'raw_key', 'reason', 'source_context')


def _unused(*args):
    raise MigrationError('Home output preparation requires its bounded file worker')


DEFINITION = Job('home_outputs', 79, 90, {'rec_proposals': ('surface',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        surface TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
        built_at REAL, schema_version INTEGER, edition_key TEXT, mix_regions INTEGER NOT NULL CHECK(mix_regions>=0),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {outputs.MAX_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 128),
        policy_count INTEGER NOT NULL CHECK(policy_count>=0), has_content INTEGER NOT NULL CHECK(has_content IN (0,1)),
        PRIMARY KEY(surface,generation)) WITHOUT ROWID'''),
    (PARTS, f'''CREATE TABLE {PARTS} (
        surface TEXT NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 127),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(surface,generation,position),
        FOREIGN KEY(surface,generation) REFERENCES {HEAD}(surface,generation)) WITHOUT ROWID'''),
    (POLICIES, f'''CREATE TABLE {POLICIES} (
        surface TEXT NOT NULL, generation INTEGER NOT NULL, raw_key TEXT NOT NULL, policy_version TEXT,
        unresolved_reason TEXT CHECK(unresolved_reason='missing_policy'),
        CHECK((policy_version IS NULL)=(unresolved_reason IS NOT NULL)),
        PRIMARY KEY(surface,generation,raw_key),
        FOREIGN KEY(surface,generation) REFERENCES {HEAD}(surface,generation)) WITHOUT ROWID'''),
    (CURRENT, f'''CREATE TABLE {CURRENT} (
        surface TEXT PRIMARY KEY, generation INTEGER NOT NULL,
        FOREIGN KEY(surface,generation) REFERENCES {HEAD}(surface,generation)) WITHOUT ROWID'''),
), _unused, _unused, dependencies=('recommendation_policies',), filters={'rec_proposals':
    '{prefix}surface IN (' + ','.join("'"+surface+"'" for surface in sorted(outputs.SURFACES)) + ')'},
    projections={'rec_proposals': ('surface', f"COALESCE(substr(CAST(payload AS BLOB),1,{outputs.MAX_BYTES + 1}),X'') AS payload", 'built_at')})


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Home output preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'home-outputs'


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {PARTS} '
        'WHERE surface=? AND generation=? ORDER BY position LIMIT 129', (row['surface'], row['generation'])).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared Home display parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['surface'], chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared Home display differs from its original digest')
    return raw


@dataclass(frozen=True)
class Plan:
    prepared: object
    units: tuple
    part_count: int
    policy_count: int


def prepare(conn, row):
    raw = bytes(row['payload'])
    if len(raw) > outputs.MAX_BYTES:
        raise MigrationError('Home display exceeds its preparation byte budget')
    prepared = outputs.prepare(row['surface'], raw.decode('utf-8'), row['built_at'])
    policies = sorted(outputs.policy_references(prepared.value))
    durable = [('file', files.publish(directory(conn), part)) for part in prepared.parts]
    return Plan(prepared, tuple(durable + [('policy', key) for key in policies]), len(durable), len(policies))


def begin(conn, row, generation, plan):
    prepared = plan.prepared
    conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (row['surface'], generation, prepared.built_at, prepared.schema, prepared.generation, prepared.mix_regions,
         prepared.digest, len(prepared.raw.encode()), plan.part_count, plan.policy_count, int(prepared.raw not in ('null', '{}'))))


def copy(conn, row, generation, position, unit):
    kind, value = unit
    if kind == 'file':
        conn.execute(f'INSERT INTO {PARTS} VALUES (?,?,?,?,?,?)', (row['surface'], generation, position, *value))
    else:
        known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (value,)).fetchone()
        conn.execute(f'INSERT INTO {POLICIES} VALUES (?,?,?,?,?)',
                     (row['surface'], generation, value, value if known else None, None if known else 'missing_policy'))


def complete(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {CURRENT} VALUES (?,?) '
                 'ON CONFLICT(surface) DO UPDATE SET generation=excluded.generation', (row['surface'], generation))


def delete(conn, owner, key):
    conn.execute(f'DELETE FROM {CURRENT} WHERE surface=?', (json.loads(key)[0],))


JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete)


def copy_root(conn, owner, key, source):
    row = dict(source)
    if sum(len(row[name]) for name in row if name in TEXT_FIELDS and row[name] is not None) > ROOT_BYTES:
        raise MigrationError('Home evidence root exceeds its preparation byte budget')
    for name in TEXT_FIELDS:
        if name in row and row[name] is not None:
            row[name] = bytes(row[name]).decode('utf-8')
    snapshot = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    conn.execute(f'INSERT INTO {ORIGINALS} VALUES (?,?,?,?) ON CONFLICT DO NOTHING', (owner, key, digest, snapshot))
    columns = ('owner', 'source_key', 'source_digest', 'kind', 'external_key', *ROOT_COLUMNS)
    conn.execute(f'INSERT INTO {ROOTS}({",".join(columns)}) VALUES ({",".join("?" for _ in columns)}) '
        'ON CONFLICT(owner,source_key) DO UPDATE SET ' + ','.join(name+'=excluded.'+name for name in columns[2:]),
        (owner, key, digest, row['kind'], row['external_key'], *(row.get(name) for name in ROOT_COLUMNS)))


def delete_root(conn, owner, key):
    conn.execute(f'DELETE FROM {ROOTS} WHERE owner=? AND source_key=?', (owner, key))


class RootJob(Job):
    def _batch(self, conn, current, size):
        progress = JOB.progress(conn)
        if progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision:
            raise MigrationError('Home root preparation requires reconciled output files')
        return super()._batch(conn, current, size)


def root_projection(owner):
    names = tuple(dict.fromkeys((*ROOT_KEYS[owner], *ROOT_FIELDS[owner])))
    return tuple(f"CASE WHEN {name} IS NULL THEN NULL ELSE COALESCE(substr(CAST({name} AS BLOB),1,{ROOT_BYTES + 1}),X'') END AS {name}"
                 if name in TEXT_FIELDS else name for name in names)


EXTERNAL_KEYS = tuple('proposal:' + surface + ':' + field for surface in sorted(outputs.SURFACES)
                      for field in (*outputs.CONTEXTS, 'recommendations'))
ROOT_JOB = RootJob('home_output_roots', 79, 90, ROOT_KEYS, (
    (ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        owner TEXT NOT NULL, source_key TEXT NOT NULL CHECK(json_valid(source_key)),
        digest TEXT NOT NULL CHECK(length(digest)=64), source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
        PRIMARY KEY(owner,source_key,digest)) WITHOUT ROWID'''),
    (ROOTS, f'''CREATE TABLE {ROOTS} (
        owner TEXT NOT NULL CHECK(owner IN ({','.join(repr(owner) for owner in ROOT_KEYS)})),
        source_key TEXT NOT NULL, source_digest TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind='prepared'), external_key TEXT NOT NULL,
        reference_digest TEXT, created_at REAL, generation_id INTEGER, external_version TEXT, group_version_id INTEGER,
        reference_kind TEXT, raw_key TEXT, reason TEXT, source_context TEXT,
        PRIMARY KEY(owner,source_key),
        FOREIGN KEY(owner,source_key,source_digest) REFERENCES {ORIGINALS}(owner,source_key,digest)) WITHOUT ROWID'''),
), copy_root, delete_root, dependencies=(DEFINITION.state,), filters={owner:
    "{prefix}kind='prepared' AND {prefix}external_key IN (" + ','.join(repr(key) for key in EXTERNAL_KEYS) + ')'
    for owner in ROOT_KEYS}, projections={owner: root_projection(owner) for owner in ROOT_KEYS}, batch_limit=8)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('files', 'roots'))
    args, remaining = parser.parse_known_args(argv)
    return run(JOB if args.job == 'files' else ROOT_JOB, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
