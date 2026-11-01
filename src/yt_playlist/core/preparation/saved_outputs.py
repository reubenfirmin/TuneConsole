"""Prepare saved Radio offers and historical reports without selecting new owners.

Exact originals live in a separate preparation file root. Parsing and durable
file publication happen outside SQLite transactions; ordered references are
checkpointed in bounded batches. Installed settings remain authoritative.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from .. import home_outputs, recommendation_outputs as files, recommendation_setting_outputs as outputs
from ..migrations.runner import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


SOURCES = {'radio-seed': 'radio_seed_offer', 'autotune-report': 'rec_autotune_result'}
PREFIXES = {'radio_seed_offer': 'prepared_radio_seed', 'rec_autotune_result': 'prepared_autotune_report'}
MAX_POLICIES = 32768
SUBJECTS = 'prepared_radio_seed_subjects'
POLICIES = 'prepared_radio_seed_policies'


def tables(source):
    prefix = PREFIXES[source]
    return prefix + '_generations', prefix + '_heads'


def definitions(source):
    head, current = tables(source)
    fields = '''menu_id TEXT CHECK(menu_id IS NULL OR length(CAST(menu_id AS BLOB)) BETWEEN 1 AND 4096),
        comparison_run_id INTEGER CHECK(comparison_run_id IS NULL OR (typeof(comparison_run_id)='integer' AND comparison_run_id>0)),
        run_ref INTEGER, run_resolution TEXT,
        subject_count INTEGER NOT NULL CHECK(subject_count BETWEEN 0 AND 1000),
        policy_count INTEGER NOT NULL CHECK(policy_count BETWEEN 0 AND 32768),
        CHECK((comparison_run_id IS NULL AND run_ref IS NULL AND run_resolution IS NULL) OR
            (comparison_run_id IS NOT NULL AND ((run_ref=comparison_run_id AND run_ref IS NOT NULL AND run_resolution IS NULL) OR
             (run_ref IS NULL AND run_resolution='missing_run' AND run_resolution IS NOT NULL)))),
        CHECK(source_kind='recorded' OR (menu_id IS NULL AND comparison_run_id IS NULL AND subject_count=0 AND policy_count=0))'''
    if source == 'rec_autotune_result':
        fields = '''observed_at REAL CHECK(observed_at IS NULL OR (typeof(observed_at) IN ('integer','real')
            AND abs(observed_at)<=1.7976931348623157e308))'''
    yield head, f'''CREATE TABLE {head} (
        source TEXT NOT NULL CHECK(source={source!r}), preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
        raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 0 AND {outputs.MAX_RAW_BYTES}),
        filename TEXT NOT NULL, content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        {fields}, PRIMARY KEY(source,preparation_revision)) WITHOUT ROWID'''
    yield current, f'''CREATE TABLE {current} (
        source TEXT PRIMARY KEY CHECK(source={source!r}), preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    if source == 'radio_seed_offer':
        # Reference custody is observational until publication. Eager source
        # FKs would block continuing catalog/run/policy retirement during staging.
        yield SUBJECTS, f'''CREATE TABLE {SUBJECTS} (
            source TEXT NOT NULL, preparation_revision INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 999), video_id TEXT NOT NULL CHECK(length(video_id)>0),
            entry_id INTEGER, resolution_reason TEXT,
            CHECK((entry_id IS NOT NULL AND resolution_reason IS NULL) OR
                (entry_id IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_entry')),
            PRIMARY KEY(source,preparation_revision,position), FOREIGN KEY(source,preparation_revision)
                REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
        yield POLICIES, f'''CREATE TABLE {POLICIES} (
            source TEXT NOT NULL, preparation_revision INTEGER NOT NULL, raw_version TEXT NOT NULL,
            policy_version TEXT, resolution_reason TEXT,
            CHECK((policy_version IS NOT NULL AND policy_version=raw_version AND resolution_reason IS NULL) OR
                (policy_version IS NULL AND resolution_reason IS NOT NULL AND resolution_reason='missing_policy')),
            PRIMARY KEY(source,preparation_revision,raw_version), FOREIGN KEY(source,preparation_revision)
                REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Saved output preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'saved-outputs'


@dataclass(frozen=True)
class Plan:
    kind: str
    size: int
    file: tuple
    fields: dict
    units: tuple


def describe(source, original):
    """Validate one bounded original without file publication or SQL writes."""
    raw = original.encode('utf-8') if original is not None else b''
    if len(raw) > outputs.MAX_RAW_BYTES:
        raise MigrationError('Saved output exceeds its preparation byte budget')
    text = original
    kind = 'null' if text is None else 'blank' if not text else 'recorded'
    value, observed, encoded = outputs.prepare(source, text) if text else (None, None, files.encode(source, '', text))
    fields, units = {'observed_at': observed}, ()
    if source == 'radio_seed_offer':
        value = value or {}
        menu = value.get('menu_id')
        if menu is not None and (not isinstance(menu, str) or not 0 < len(menu.encode()) <= 4096):
            raise ValueError('Invalid saved seed menu identity')
        comparison = value.get('comparison') or {}
        if not isinstance(comparison, dict):
            raise ValueError('Invalid saved seed comparison')
        run_id = comparison.get('run_id')
        if run_id is not None and (type(run_id) is not int or not 0 < run_id < 2 ** 63):
            raise ValueError('Invalid saved seed comparison run')
        policies = sorted(home_outputs.policy_references(value))
        if len(policies) > MAX_POLICIES:
            raise ValueError('Saved seed exceeds its policy reference budget')
        tracks = value.get('tracks', [])
        fields = {'menu_id': menu, 'comparison_run_id': run_id, 'subject_count': len(tracks), 'policy_count': len(policies)}
        units = tuple([('subject', (position, track['video_id'])) for position, track in enumerate(tracks)] +
                      [('policy', policy) for policy in policies])
    return kind, len(raw), fields, units, encoded


def prepare(conn, row):
    text = bytes(row['value']).decode('utf-8') if row['value'] is not None else None
    kind, size, fields, units, encoded = describe(row['key'], text)
    return Plan(kind, size, files.publish(directory(conn), encoded), fields, units)


def begin(conn, row, generation, plan):
    fields = dict(plan.fields)
    if row['key'] == 'radio_seed_offer':
        run_id = fields['comparison_run_id']
        known = run_id is not None and conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (run_id,)).fetchone()
        fields.update(run_ref=run_id if known else None, run_resolution=None if run_id is None or known else 'missing_run')
    columns = ','.join(fields)
    conn.execute(f'INSERT INTO {tables(row["key"])[0]}(source,preparation_revision,source_kind,raw_size,filename,content_digest,byte_size,{columns}) '
                 f'VALUES (?,?,?,?,?,?,?,{",".join("?" for _ in fields)})',
                 (row['key'], generation, plan.kind, plan.size, *plan.file, *fields.values()))


def copy(conn, row, generation, position, unit):
    kind, value = unit
    if kind == 'subject':
        order, video = value
        entry = conn.execute('SELECT id FROM catalog_entries WHERE video_id=? ORDER BY id LIMIT 1', (video,)).fetchone()
        conn.execute(f'INSERT INTO {SUBJECTS} VALUES (?,?,?,?,?,?)',
                     (row['key'], generation, order, video, entry[0] if entry else None, None if entry else 'missing_entry'))
    else:
        known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (value,)).fetchone()
        conn.execute(f'INSERT INTO {POLICIES} VALUES (?,?,?,?,?)',
                     (row['key'], generation, value, value if known else None, None if known else 'missing_policy'))


def complete(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {tables(row["key"])[1]} VALUES (?,?) '
                 'ON CONFLICT(source) DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['key'], generation))


def delete(conn, owner, key):
    source, = json.loads(key)
    if source in PREFIXES:
        conn.execute(f'DELETE FROM {tables(source)[1]} WHERE source=?', (source,))


def read_original(row):
    raw = files.read(row['filename'], row['content_digest'], row['byte_size'], surface=row['source'], chunk_key='')
    kind = 'null' if raw is None else 'blank' if raw == '' else 'recorded'
    if kind != row['source_kind'] or raw is not None and (not isinstance(raw, str) or len(raw.encode()) != row['raw_size']):
        raise MigrationError('Prepared saved output differs from its captured kind or byte size')
    return raw


def repair_files(source):
    def repair(conn,progress):
        # Only a completed generation for this exact input can need repair.
        # Interrupted/newer source work follows the ordinary bounded worker.
        try:
            conn.execute('BEGIN')
            if definition(source).progress(conn) != progress:
                return
            head,current = tables(source)
            captured = conn.execute(f'SELECT g.* FROM {current} h JOIN {head} g USING(source,preparation_revision) '
                'WHERE h.source=? AND h.preparation_revision=?',(source,progress.input_revision)).fetchone()
            if captured is None:
                return
            row = conn.execute("SELECT key,CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,?),X'') END AS value "
                'FROM main.settings WHERE key=?',(outputs.MAX_RAW_BYTES+1,source)).fetchone()
            if row is None:
                raise MigrationError('Saved output recovery requires its installed original')
        finally:
            conn.rollback()
        plan = prepare(conn,row)
        if plan.file != tuple(captured[column] for column in ('filename','content_digest','byte_size')):
            raise MigrationError('Saved output recovery differs from its retained file reference')
    return repair


def definition(source):
    return Job(PREFIXES[source].removeprefix('prepared_'), 79, 94, {'settings': ('key',)},
        tuple(definitions(source)), None, None,
        dependencies=('catalog_entries', 'rec_model_selection_runs', 'recommendation_policies') if source == 'radio_seed_offer' else (),
        filters={'settings': '{prefix}key=' + repr(source)}, projections={'settings': ('key',
            f'CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,{outputs.MAX_RAW_BYTES + 1}),X\'\') END AS value')})


JOBS = {name: OrderedJob(definition(source), prepare, begin, copy, complete, delete,
                       repair_files=repair_files(source)) for name, source in SOURCES.items()}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
