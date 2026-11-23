"""Keep preparation state and optional radio fields with their existing owners."""
import re

from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'consolidate_recommendation_candidate_storage'
RETIRED = ('candidate_refresh_checkpoint', 'rec_preparation', 'recommendation_radio_bands')
MARKER = 'recommendation_candidate_radio_insert'
INDEXES = {'ix_recommendation_radio_band': 'intensity_band,genre_key,rank',
           'ix_recommendation_radio_genre': 'genre_key,intensity_band,rank'}
SETTINGS_GUARDS = ('collection_settings_insert', 'collection_settings_update')


def predecessor_objects(conn):
    owners = (*RETIRED, 'content_full_checkpoint', 'recommendation_candidates')
    marks = ','.join('?' for _ in owners)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM main.sqlite_schema WHERE sql IS NOT NULL AND (tbl_name IN ({marks}) '
        "OR name IN (?,?) OR (type='trigger' AND instr(sql,'UPDATE rec_preparation SET ')>0)) ORDER BY name",
        (*owners, *SETTINGS_GUARDS))]


def install(conn, _context):
    _refresh_settings(conn)
    _content_counters(conn)
    _radio_fields(conn)


def _refresh_settings(conn):
    keys = ('rec_candidate_preferences', 'rec_candidates_expire_at')
    guards = definitions(conn, SETTINGS_GUARDS)
    for name, sql in guards.items():
        conn.execute(f'DROP TRIGGER {name}')
        for key in keys:
            sql = sql.replace(f"'{key}',", '')
        conn.execute(sql)
    row = conn.execute('SELECT preferences_fingerprint,expires_at FROM candidate_refresh_checkpoint WHERE singleton=1').fetchone()
    if row is not None:
        conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         ((key, str(value)) for key, value in zip(keys, row) if value is not None))
    conn.execute('DROP TABLE candidate_refresh_checkpoint')


def _content_counters(conn):
    conn.execute('ALTER TABLE content_full_checkpoint ADD COLUMN has_checkpoint INTEGER NOT NULL DEFAULT 1 '
                 'CHECK(has_checkpoint IN (0,1))')
    for column in ('content_changes', 'evidence_changes'):
        conn.execute(f'ALTER TABLE content_full_checkpoint ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
    conn.execute('INSERT INTO content_full_checkpoint(singleton,has_config,has_checkpoint) VALUES (1,0,0) '
                 'ON CONFLICT(singleton) DO NOTHING')
    conn.execute('UPDATE content_full_checkpoint SET content_changes=p.content_changes,evidence_changes=p.evidence_changes '
                 'FROM rec_preparation p WHERE singleton=p.id')
    triggers = conn.execute("SELECT name,sql FROM main.sqlite_schema WHERE type='trigger' "
                            "AND instr(sql,'UPDATE rec_preparation SET ')>0").fetchall()
    for name, sql in triggers:
        conn.execute(f'DROP TRIGGER {name}')
        sql = re.sub(r'UPDATE rec_preparation SET ([^;]+) WHERE id=1;',
                     r'UPDATE content_full_checkpoint SET \1 WHERE singleton=1;', sql)
        conn.execute(sql)
    conn.execute('DROP TABLE rec_preparation')


def _radio_fields(conn):
    guard = 'frozen_recommendation_candidates_update'
    sql = definitions(conn, (guard,))[guard]
    conn.execute(f'DROP TRIGGER {guard}')
    conn.execute('ALTER TABLE recommendation_candidates ADD COLUMN intensity_band INTEGER '
                 "CHECK(intensity_band IS NULL OR (typeof(intensity_band)='integer' AND intensity_band BETWEEN 0 AND 50))")
    conn.execute('ALTER TABLE recommendation_candidates ADD COLUMN genre_key TEXT '
                 'CHECK((intensity_band IS NULL)=(genre_key IS NULL))')
    conn.execute('UPDATE recommendation_candidates AS c SET intensity_band=b.intensity_band,genre_key=b.genre_key '
                 'FROM recommendation_radio_bands b WHERE c.pool_id=b.pool_id AND c.recording_id=b.recording_id')
    conn.execute('DROP TABLE recommendation_radio_bands')
    conn.execute(sql)
    for name, columns in INDEXES.items():
        conn.execute(f'CREATE INDEX {name} ON recommendation_candidates(pool_id,{columns}) WHERE intensity_band IS NOT NULL')
    for operation in ('INSERT', 'UPDATE OF pool_id,intensity_band'):
        name = 'recommendation_candidate_radio_' + operation.split()[0].lower()
        conn.execute(f'CREATE TRIGGER {name} BEFORE {operation} ON recommendation_candidates '
                     'WHEN NEW.intensity_band IS NOT NULL AND NOT EXISTS('
                     "SELECT 1 FROM recommendation_pools WHERE id=NEW.pool_id AND purpose='radio') "
                     "BEGIN SELECT RAISE(ABORT,'Radio bands require a radio pool'); END")


def validate(conn):
    actual = definitions(conn, (*RETIRED, *INDEXES))
    if set(actual) != INDEXES.keys() or any('ON recommendation_candidates' not in sql for sql in actual.values()):
        raise MigrationError('Recommendation candidate storage was not consolidated')
