"""Stage paired model outputs and clicked pools without selecting serving owners.

Decode and resolve one bounded source outside SQL transactions, then retain a
durable plan. Subsequent batches read only their selected units. Source and
dependency capture invalidate that plan before a completed head can change.
"""
from dataclasses import astuple, replace
import hashlib
import json
from pathlib import Path

from .. import genre_identity, library_pool_state, model_projections, recommendation_outputs as files, recommendation_pools
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run
from . import document_plan, home_outputs as home_preparation
from .document_plan import Units


MAX_SOURCE_BYTES = 128 * 1024 * 1024
PART_CHARACTERS = 64 * 1024
def prefix(surface):
    return 'prepared_model_pairs' if surface.startswith('model_pair:') else 'prepared_clicked_pools'


def table(surface, suffix):
    return prefix(surface) + '_' + suffix


def definitions(owner):
    head = owner + '_generations'
    yield head, f'''CREATE TABLE {head} (
        surface TEXT NOT NULL, preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0), built_at REAL,
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {MAX_SOURCE_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 2048),
        unit_count INTEGER NOT NULL CHECK(unit_count>=1),
        plan_filename TEXT NOT NULL, plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),
        plan_bytes INTEGER NOT NULL CHECK(plan_bytes BETWEEN 1 AND {files.MAX_BYTES}),
        ranked_digest TEXT, ranked_policy TEXT, display_digest TEXT, display_parts INTEGER NOT NULL CHECK(display_parts BETWEEN 0 AND 128),
        run_id INTEGER CHECK(run_id IS NULL OR (typeof(run_id)='integer' AND run_id>0)), run_ref INTEGER,
        run_resolution TEXT CHECK(run_resolution='missing_run'), arm TEXT CHECK(arm IN ('incumbent','challenger')),
        format TEXT CHECK(format IN ('mixes','albums','radio')),
        encoding_model TEXT CHECK(encoding_model IS NULL OR length(encoding_model) BETWEEN 1 AND 4096),
        lane TEXT CHECK(lane IN ('wheelhouse','explore','comfort','temporal')), epoch INTEGER CHECK(epoch>=0),
        CHECK((surface GLOB 'model_pair:*')=(run_id IS NOT NULL)),
        CHECK(run_id IS NULL OR (run_ref=run_id AND run_resolution IS NULL) OR (run_ref IS NULL AND run_resolution='missing_run')),
        PRIMARY KEY(surface,preparation_revision)) WITHOUT ROWID'''
    specs = {
        'parts': ('''kind TEXT NOT NULL CHECK(kind IN ('original','display')), position INTEGER NOT NULL CHECK(position>=0),
            filename TEXT NOT NULL, content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
            byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 1048576)''', 'kind,position'),
        'pools': ('''content_digest TEXT NOT NULL CHECK(length(content_digest)=64), pool_key TEXT NOT NULL,
            policy_version TEXT NOT NULL, group_version_id INTEGER, input_fingerprint TEXT NOT NULL,
            purpose TEXT NOT NULL, eligible_count INTEGER NOT NULL CHECK(eligible_count>=0)''', 'content_digest'),
        'candidates': ('''pool_digest TEXT NOT NULL, rank INTEGER NOT NULL CHECK(rank>=0), recording_id INTEGER, raw_key TEXT,
            score REAL, intensity_band INTEGER, genre_key TEXT,
            CHECK((recording_id IS NULL)<>(raw_key IS NULL)), CHECK(recording_id IS NULL OR recording_id>0),
            CHECK(intensity_band BETWEEN 0 AND 50), CHECK((intensity_band IS NULL)=(genre_key IS NULL))''', 'pool_digest,rank'),
        'slots': ('slot_surface TEXT NOT NULL, mode_key TEXT NOT NULL, purpose TEXT NOT NULL, pool_digest TEXT NOT NULL', 'slot_surface,mode_key,purpose'),
        'outputs': ('''slot_surface TEXT NOT NULL, chunk_key TEXT NOT NULL, filename TEXT NOT NULL,
            content_digest TEXT NOT NULL CHECK(length(content_digest)=64), byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 1048576),
            expires_at REAL, pool_digest TEXT, first_rank INTEGER, last_rank INTEGER''', 'slot_surface,chunk_key'),
        'framing': ('''presence INTEGER NOT NULL, comfort_pool INTEGER, year_low INTEGER, year_high INTEGER,
            full_pools INTEGER, unmodeled INTEGER, musical_generation TEXT, radio_generation TEXT, radio_space TEXT,
            intensity_version INTEGER, radio_profile_version TEXT, vibe_version TEXT, vibe_profile_version TEXT''', 'presence'),
        'policies': ('''raw_version TEXT NOT NULL, policy_version TEXT, resolution_reason TEXT CHECK(resolution_reason='missing_policy'),
            CHECK((policy_version IS NULL)=(resolution_reason IS NOT NULL))''', 'raw_version'),
        'modes': ('''position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 511), mode_id INTEGER NOT NULL CHECK(mode_id>0), mode_ref INTEGER,
            resolution_reason TEXT CHECK(resolution_reason='missing_mode'), space TEXT NOT NULL, centroid BLOB NOT NULL,
            CHECK(length(centroid) BETWEEN 8 AND 32768 AND length(centroid)%8=0),
            CHECK((mode_ref IS NULL)=(resolution_reason IS NOT NULL)), CHECK(mode_ref IS NULL OR mode_ref=mode_id)''', 'position'),
        'contexts': ("field TEXT NOT NULL CHECK(field IN ('context','choice_context','album_choice_context')), artifact TEXT NOT NULL CHECK(length(artifact) BETWEEN 1 AND 1024)", 'field'),
        'genres': ('''kind TEXT NOT NULL CHECK(kind IN ('include','avoid')), position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 99),
            raw_key TEXT NOT NULL, family_key TEXT, genre_key TEXT, resolution_reason TEXT,
            CHECK((family_key IS NOT NULL)+(genre_key IS NOT NULL)+(resolution_reason IS NOT NULL)=1)''', 'kind,position'),
    }
    for suffix, (columns, key) in specs.items():
        name = owner + '_' + suffix
        pool_fk = (f',FOREIGN KEY(surface,preparation_revision,pool_digest) REFERENCES {owner}_pools(surface,preparation_revision,content_digest)'
                   if suffix in ('candidates', 'slots', 'outputs') else '')
        yield name, f'''CREATE TABLE {name} (
            surface TEXT NOT NULL, preparation_revision INTEGER NOT NULL, {columns},
            PRIMARY KEY(surface,preparation_revision,{key}),
            FOREIGN KEY(surface,preparation_revision) REFERENCES {head}(surface,preparation_revision){pool_fk}) WITHOUT ROWID'''
    current = owner + '_heads'
    yield current, f'''CREATE TABLE {current} (
        surface TEXT PRIMARY KEY, preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(surface,preparation_revision) REFERENCES {head}(surface,preparation_revision)) WITHOUT ROWID'''


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Ranked state preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'ranked-state'


def durable_plan(conn, surface, fields, units):
    return document_plan.build(directory(conn), surface, fields, units)


def resumed_plan(conn, surface, revision):
    row = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table(surface,"generations")} '
                       'WHERE surface=? AND preparation_revision=?', (surface, revision)).fetchone()
    if row is None:
        raise MigrationError('Prepared ranked checkpoint has no durable plan')
    return document_plan.load(surface, row)


def ranked_units(conn, surface, plan, *, retain=files.publish):
    if plan is None:
        return
    if plan.framing is not None:
        yield 'framing', astuple(plan.framing)
    distinct = set()
    for slot in plan.slots:
        pool = slot.pool
        if pool.content_digest not in distinct:
            distinct.add(pool.content_digest)
            yield 'pool', (pool.content_digest, pool.pool_key, pool.policy_version, pool.group_version_id,
                pool.input_fingerprint, pool.purpose, len(pool.candidates))
            for rank, candidate in enumerate(pool.candidates):
                kind, identity = recommendation_pools.owner(candidate)
                yield 'candidate', (pool.content_digest, rank, identity if kind == 'recording' else None,
                    identity if kind == 'unresolved' else None, candidate.score, candidate.intensity_band, candidate.genre_key)
        yield 'slot', (slot.surface, slot.mode_key, slot.purpose, pool.content_digest)
    for output in plan.outputs:
        yield 'output', (output.surface, output.chunk_key, *retain(directory(conn), output.encoded),
                         output.expires_at, output.pool_digest, output.first_rank, output.last_rank)


def prepare(conn, row, *, revision, offset, rebuild=False):
    surface = row['surface']
    if offset and not rebuild:
        return resumed_plan(conn, surface, revision)
    fields, units = describe(conn, row)
    result = durable_plan(conn, surface, fields, units)
    if offset:
        captured = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table(surface,"generations")} '
                               'WHERE surface=? AND preparation_revision=?', (surface, revision)).fetchone()
        if captured is None or tuple(captured) != result.manifest:
            raise MigrationError('Rebuilt ranked plan differs from its captured checkpoint')
    return result


def describe(conn, row, *, retain=files.publish):
    """Reconstruct members; verification can supply a read-only file checker."""
    from yt_playlist.repos import recommendation_publication
    surface = row['surface']
    raw = bytes(row['payload'])
    if not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise MigrationError('Ranked source exceeds its preparation byte budget')
    text = raw.decode('utf-8')
    if row['built_at'] is not None:
        from ..recommendation_work import finite
        finite(row['built_at'])
    fields = {'built_at': row['built_at'], 'raw_digest': hashlib.sha256(raw).hexdigest(), 'raw_size': len(raw),
              'part_count': (len(text) + PART_CHARACTERS - 1) // PART_CHARACTERS, 'display_parts': 0}
    units, plan = [], None
    for position, start in enumerate(range(0, len(text), PART_CHARACTERS)):
        units.append(('part', ('original', position, *retain(directory(conn),
            files.encode(surface, 'original:' + str(position), text[start:start + PART_CHARACTERS])))))
    if surface.startswith('model_pair:'):
        run_id, arm, format = model_projections.address(surface)
        if run_id >= 2 ** 63:
            raise ValueError('Comparison run exceeds its integer identity budget')
        projection = model_projections.prepare(conn, surface, files.decode_json(text), row['built_at'], capture_only=True)
        known = conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (run_id,)).fetchone()
        fields.update(run_id=run_id, run_ref=run_id if known else None, run_resolution=None if known else 'missing_run',
            arm=arm, format=format, encoding_model=projection.value.get('encoding_model'),
            display_digest=projection.digest, display_parts=len(projection.parts))
        for position, encoded in enumerate(projection.parts):
            units.append(('part', ('display', position, *retain(directory(conn), encoded))))
        for position, (mode, space, centroid) in enumerate(projection.modes):
            known = conn.execute('SELECT 1 FROM taste_modes WHERE mode_id=?', (mode,)).fetchone()
            units.append(('mode', (position, mode, mode if known else None, None if known else 'missing_mode', space, centroid.hex())))
        for field in model_projections.CONTEXTS:
            context = projection.value.get(field)
            if isinstance(context, dict) and context.get('artifact'):
                units.append(('context', (field, context['artifact'])))
        policies, plan = projection.policies, projection.plan
    else:
        value = library_pool_state.decode(surface, text)
        plan = library_pool_state.prepare(conn, surface, value, row['built_at'], capture_only=True)
        parameters = value['parameters']
        fields.update(lane=parameters['lane'], epoch=parameters['epoch'])
        for field, kind in (('include_genres', 'include'), ('avoid_genres', 'avoid')):
            for position, raw_key in enumerate(parameters[field]):
                family = conn.execute('SELECT key FROM genre_family WHERE key=?', (raw_key,)).fetchone()
                genre, reason = (None, None) if family else genre_identity.resolve(conn, raw_key.removeprefix('other:'))
                units.append(('genre', (kind, position, raw_key, family[0] if family else None, genre, reason)))
        policies = {plan.policy_version} if plan is not None else set()
    for policy in sorted(policies):
        known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (policy,)).fetchone()
        units.append(('policy', (policy, policy if known else None, None if known else 'missing_policy')))
    if plan is not None:
        plan = replace(plan, external_key='capture:' + surface)
        fields.update(ranked_digest=recommendation_publication.validate(plan, allow_unobserved=True), ranked_policy=plan.policy_version)
    return fields, _chain(units, ranked_units(conn, surface, plan, retain=retain))


def _chain(first, second):
    yield from first
    yield from second


def begin(conn, row, revision, plan):
    values = {'surface': row['surface'], 'preparation_revision': revision, **plan.fields,
              'plan_filename': plan.manifest[0], 'plan_digest': plan.manifest[1], 'plan_bytes': plan.manifest[2]}
    conn.execute(f'INSERT INTO {table(row["surface"],"generations")}({",".join(values)}) '
                 f'VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


RELATIONS = {'part': 'parts', 'pool': 'pools', 'candidate': 'candidates', 'slot': 'slots', 'output': 'outputs',
             'framing': 'framing', 'policy': 'policies', 'mode': 'modes', 'context': 'contexts', 'genre': 'genres'}


def copy(conn, row, revision, position, unit):
    kind, values = unit
    if kind == 'mode':
        values = [*values[:-1], bytes.fromhex(values[-1])]
    conn.execute(f'INSERT INTO {table(row["surface"],RELATIONS[kind])} VALUES ({",".join("?" for _ in range(2+len(values)))})',
                 (row['surface'], revision, *values))


def complete(conn, row, revision, plan):
    conn.execute(f'INSERT INTO {table(row["surface"],"heads")} VALUES (?,?) ON CONFLICT(surface) '
                 'DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['surface'], revision))


def delete(conn, owner, key):
    surface, = json.loads(key)
    conn.execute(f'DELETE FROM {table(surface,"heads")} WHERE surface=?', (surface,))


def head(conn, surface):
    return conn.execute(f'SELECT g.* FROM {table(surface,"heads")} h JOIN {table(surface,"generations")} g '
                        'USING(surface,preparation_revision) WHERE surface=?', (surface,)).fetchone()


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,content_digest,byte_size FROM {table(row["surface"],"parts")} '
        "WHERE surface=? AND preparation_revision=? AND kind='original' ORDER BY position LIMIT 2049",
        (row['surface'], row['preparation_revision'])).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared ranked originals are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['surface'], chunk_key='original:' + str(part[0])) for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared ranked original differs from its captured digest')
    return raw


def definition(name, owner, predicate, *, clicked=False):
    dependencies = ('recordings', 'recommendation_policies') + (
        ('genre_catalog', 'genre_alias', 'genre_family') if clicked else ('rec_model_selection_runs', 'taste_modes'))
    return Job(name, 79, 94, {'rec_proposals': ('surface',)}, tuple(definitions(owner)), None, None,
        dependencies=dependencies, optional_dependencies=('genre_redirects',) if clicked else (),
        filters={'rec_proposals': predicate}, projections={'rec_proposals': ('surface',
            f"COALESCE(substr(CAST(payload AS BLOB),1,{MAX_SOURCE_BYTES + 1}),X'') AS payload", 'built_at')})


def resume_read(conn, owner, key):
    surface, = json.loads(key)
    return conn.execute('SELECT surface,built_at FROM main.rec_proposals WHERE surface=? AND json_array(surface)=?',
                        (surface, key)).fetchone()


JOBS = {name: OrderedJob(definition(name, owner, predicate, clicked=name == 'clicked'),
    prepare, begin, copy, complete, delete, revision_aware=True, resume_read=resume_read) for name, owner, predicate in (
        ('pairs', 'prepared_model_pairs', "{prefix}surface GLOB 'model_pair:*'"),
        ('clicked', 'prepared_clicked_pools', '{prefix}surface IN (' + ','.join(repr(surface) for surface in library_pool_state.SURFACES) + ')'))}


ROOTS = 'prepared_model_pair_roots'
ORIGINALS = 'prepared_model_pair_root_originals'


def copy_root(conn, owner, key, source):
    row = dict(source)
    if sum(len(row[name]) for name in row if name in home_preparation.TEXT_FIELDS and row[name] is not None) > home_preparation.ROOT_BYTES:
        raise MigrationError('Comparison root exceeds its preparation byte budget')
    for name in home_preparation.TEXT_FIELDS:
        if name in row and row[name] is not None:
            row[name] = bytes(row[name]).decode('utf-8')
    snapshot = canonical(row).decode()
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    conn.execute(f'INSERT INTO {ORIGINALS} VALUES (?,?,?,?) ON CONFLICT DO NOTHING', (owner, key, digest, snapshot))
    columns = ('owner', 'source_key', 'source_digest', 'kind', 'external_key', *home_preparation.ROOT_COLUMNS)
    conn.execute(f'INSERT INTO {ROOTS}({",".join(columns)}) VALUES ({",".join("?" for _ in columns)}) '
        'ON CONFLICT(owner,source_key) DO UPDATE SET ' + ','.join(name + '=excluded.' + name for name in columns[2:]),
        (owner, key, digest, row['kind'], row['external_key'], *(row.get(name) for name in home_preparation.ROOT_COLUMNS)))


def delete_root(conn, owner, key):
    conn.execute(f'DELETE FROM {ROOTS} WHERE owner=? AND source_key=?', (owner, key))


class RootJob(Job):
    def _batch(self, conn, current, size):
        progress = JOBS['pairs'].progress(conn)
        if progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision:
            raise MigrationError('Comparison roots require reconciled output files')
        return super()._batch(conn, current, size)


ROOT_JOB = RootJob('model_pair_roots', 79, 94, home_preparation.ROOT_KEYS, (
    (ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        owner TEXT NOT NULL, source_key TEXT NOT NULL CHECK(json_valid(source_key)),
        digest TEXT NOT NULL CHECK(length(digest)=64), source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
        PRIMARY KEY(owner,source_key,digest)) WITHOUT ROWID'''),
    (ROOTS, f'''CREATE TABLE {ROOTS} (
        owner TEXT NOT NULL, source_key TEXT NOT NULL, source_digest TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind='comparison'), external_key TEXT NOT NULL,
        reference_digest TEXT, created_at REAL, generation_id INTEGER, external_version TEXT, group_version_id INTEGER,
        reference_kind TEXT, raw_key TEXT, reason TEXT, source_context TEXT,
        PRIMARY KEY(owner,source_key),
        FOREIGN KEY(owner,source_key,source_digest) REFERENCES {ORIGINALS}(owner,source_key,digest)) WITHOUT ROWID'''),
), copy_root, delete_root, dependencies=(JOBS['pairs'].state,), filters={owner:
    "{prefix}kind='comparison' AND {prefix}external_key GLOB 'proposal:model_pair:*'" for owner in home_preparation.ROOT_KEYS},
    projections={owner: home_preparation.root_projection(owner) for owner in home_preparation.ROOT_KEYS}, batch_limit=8)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=(*JOBS, 'roots'))
    args, remaining = parser.parse_known_args(argv)
    return run(ROOT_JOB if args.job == 'roots' else JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
