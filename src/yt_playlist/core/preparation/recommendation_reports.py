"""Prepare bounded diagnostic files and queried report fields without cutover."""
import hashlib
import json
from pathlib import Path
import time

from .. import recommendation_outputs as files, recommendation_reports as reports
from ..migrations.runner import MigrationError
from .rows import Job, main as run


HEAD = 'prepared_report_generations'
PARTS = 'prepared_report_parts'
METADATA = ('built_at', 'observed_at', 'schema_version', 'watermark', 'report_key', 'evidence_generation',
            'inputs_key', 'next_at', 'vibe_episodes', 'half_life_days', 'content_digest', 'total_bytes', 'has_content')


def _unused(*args):
    raise MigrationError('Report preparation requires its file worker')


DEFINITION = Job('recommendation_reports', 79, 92, {'rec_proposals': ('surface',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        generation INTEGER NOT NULL CHECK(generation>=0), surface TEXT NOT NULL,
        built_at REAL, observed_at REAL, schema_version INTEGER, watermark TEXT, report_key TEXT,
        evidence_generation TEXT, inputs_key TEXT, next_at REAL,
        vibe_episodes INTEGER CHECK(vibe_episodes IS NULL OR (typeof(vibe_episodes)='integer' AND vibe_episodes>=0)),
        half_life_days REAL CHECK(half_life_days IS NULL OR half_life_days>0),
        content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
        total_bytes INTEGER NOT NULL CHECK(total_bytes BETWEEN 1 AND {reports.MAX_BYTES}),
        has_content INTEGER NOT NULL CHECK(has_content IN (0,1)),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 64),
        PRIMARY KEY(generation,surface)) WITHOUT ROWID'''),
    (PARTS, f'''CREATE TABLE {PARTS} (
        generation INTEGER NOT NULL, surface TEXT NOT NULL,
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 63),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(generation,surface,position),
        FOREIGN KEY(generation,surface) REFERENCES {HEAD}(generation,surface)) WITHOUT ROWID'''),
    ('prepared_report_usage', f'''CREATE TABLE prepared_report_usage (
        generation INTEGER NOT NULL, surface TEXT NOT NULL CHECK(surface='listening_choices'),
        format TEXT NOT NULL CHECK(format IN ('mixes','albums','radio')),
        field TEXT NOT NULL CHECK(field IN ('scores','episodes','days')), value NOT NULL,
        CHECK(typeof(value) IN ('integer','real') AND value>=0 AND value<=1.7976931348623157e308
              AND (field='scores' OR typeof(value)='integer')),
        PRIMARY KEY(generation,surface,format,field),
        FOREIGN KEY(generation,surface) REFERENCES {HEAD}(generation,surface)) WITHOUT ROWID'''),
    ('prepared_report_heads', f'''CREATE TABLE prepared_report_heads (
        surface TEXT PRIMARY KEY, generation INTEGER NOT NULL,
        FOREIGN KEY(generation,surface) REFERENCES {HEAD}(generation,surface)) WITHOUT ROWID'''),
), _unused, _unused, filters={'rec_proposals': '{prefix}surface IN (' +
    ','.join("'"+surface+"'" for surface in sorted(reports.SURFACES)) + ')'},
    projections={'rec_proposals': ('surface', f"COALESCE(substr(CAST(payload AS BLOB),1,{reports.MAX_BYTES + 1}),X'') AS payload", 'built_at')})


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Report preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'recommendation-reports'


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {PARTS} '
                         'WHERE generation=? AND surface=? ORDER BY position LIMIT 65',
                         (row['generation'], row['surface'])).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared report parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['surface'], chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['total_bytes'] or hashlib.sha256(raw.encode()).hexdigest() != row['content_digest']:
        raise MigrationError('Prepared report differs from its original digest')
    return raw


def _prepare(conn, row):
    raw = bytes(row['payload'])
    if len(raw) > reports.MAX_BYTES:
        raise MigrationError('Report exceeds its preparation byte budget')
    plan = reports.prepare(row['surface'], raw.decode('utf-8'), row['built_at'])
    durable = [files.publish(directory(conn), part) for part in plan.parts]
    return plan, durable


def _stage(conn, generation, prepared):
    plan, durable = prepared
    conn.execute(f'INSERT INTO {HEAD} VALUES ({",".join("?" for _ in range(16))}) ON CONFLICT DO NOTHING',
                 (generation, plan.surface, *plan.metadata, len(durable)))
    conn.executemany(f'INSERT INTO {PARTS} VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
        [(generation, plan.surface, position, *part) for position, part in enumerate(durable)])
    conn.executemany('INSERT INTO prepared_report_usage VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING',
                     [(generation, plan.surface, *usage) for usage in plan.usage])
    conn.execute('INSERT INTO prepared_report_heads VALUES (?,?) '
                 'ON CONFLICT(surface) DO UPDATE SET generation=excluded.generation', (plan.surface, generation))


class ReportJob:
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
            # Four fixed report surfaces, each at most 64 files and nine usage
            # fields. No report list or file operation runs under a writer.
            owner, cursor, work = DEFINITION.select(conn, current, min(batch_size, 4))
            conn.rollback()
            plans = [_prepare(conn, row) if row is not None else None for _, _, row in work]
            conn.execute('BEGIN IMMEDIATE')
            if self.progress(conn) != current:
                return self.progress(conn)
            for (source, key, row), plan in zip(work, plans):
                if plan is None:
                    conn.execute('DELETE FROM prepared_report_heads WHERE surface=?', (json.loads(key)[0],))
                else:
                    _stage(conn, current.input_revision, plan)
                conn.execute(f'DELETE FROM {DEFINITION.changes} WHERE owner=? AND source_key=?', (source, key))
            complete = owner is None and not conn.execute(f'SELECT 1 FROM {DEFINITION.changes} LIMIT 1').fetchone()
            conn.execute(f'UPDATE {self.state} SET status=?,owner=?,cursor=?,processed=processed+?,error=NULL,'
                         'prepared_revision=CASE WHEN ? THEN input_revision END,updated_at=? WHERE id=1',
                ('prepared' if complete else 'copying' if owner else 'reconciling', owner, cursor,
                 len(work), int(complete), clock()))
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


JOB = ReportJob()


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
