"""Keep reporting facts on impressions and mode choices on playlists."""
from .. import served_facts
from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'fold_served_impression_storage'
NEXT_ID_KEY = 'next_served_impression_id'
MARKER = 'served_impression_facts_immutable'
RETIRED = ('rec_lane_impressions', 'served_fact_state', 'served_facts',
           'impression_file_writes', 'served_diagnostic_retirements', 'rec_mode_picks')
OWNERS = ('rec_served_impressions', 'playlists', *RETIRED)
FLAGS = ('has_last_played', 'has_coverage', 'genre_covered', 'year_covered', 'audio_covered',
         'controlled', 'legacy_policy', 'has_experiment')
FACT_COLUMNS = {
    name: (f'INTEGER CHECK({name} IN (0,1))' if name in FLAGS else
           'REAL' if name in ('last_played', 'propensity') else 'TEXT')
    for name in served_facts.FIELDS
}
FACT_COLUMNS['diagnostic_expired'] = 'INTEGER NOT NULL DEFAULT 0 CHECK(diagnostic_expired IN (0,1))'
PLAYLIST_COLUMNS = {
    'picked_mode_id': 'INTEGER REFERENCES taste_modes(mode_id)',
    'picked_legacy_mode_id': 'INTEGER',
    'picked_ranker': 'TEXT',
    'mode_picked_at': '''REAL CHECK(
        (mode_picked_at IS NULL AND picked_mode_id IS NULL AND picked_legacy_mode_id IS NULL AND picked_ranker IS NULL)
        OR (mode_picked_at IS NOT NULL AND ((picked_mode_id IS NULL)<>(picked_legacy_mode_id IS NULL))))''',
}


def predecessor_objects(conn):
    marks = ','.join('?' for _ in OWNERS)
    return [sql for sql, in conn.execute(f'SELECT sql FROM sqlite_schema WHERE tbl_name IN ({marks}) '
                                        'AND sql IS NOT NULL ORDER BY type,name', OWNERS)]


def install(conn, _context):
    playlist_fields = [row[1] for row in conn.execute('PRAGMA table_info(playlists)')]
    revision = conn.execute("SELECT sql FROM sqlite_schema WHERE name='playlist_revision_playlists_update'").fetchone()[0]
    conn.execute('DROP TRIGGER playlist_revision_playlists_update')
    conn.execute(revision.replace('AFTER UPDATE ON', 'AFTER UPDATE OF ' + ','.join(playlist_fields) + ' ON', 1))
    for table, columns in (('rec_served_impressions', FACT_COLUMNS), ('playlists', PLAYLIST_COLUMNS)):
        for name, declaration in columns.items():
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    fields = ','.join(FACT_COLUMNS)
    conn.execute(f'UPDATE rec_served_impressions AS i SET ({fields})='
                 f'(SELECT {fields} FROM served_facts WHERE impression_id=i.id) '
                 'WHERE id IN (SELECT impression_id FROM served_facts)')
    conn.execute('UPDATE playlists AS p SET (picked_mode_id,picked_legacy_mode_id,picked_ranker,mode_picked_at)='
                 '(SELECT mode_id,legacy_mode_id,ranker,created_at FROM rec_mode_picks WHERE membership_ref_id=p.id) '
                 'WHERE id IN (SELECT membership_ref_id FROM rec_mode_picks)')
    conn.execute('INSERT INTO settings(key,value) SELECT ?,CAST(next_id AS TEXT) FROM (SELECT max(next_id,'
                 '(SELECT COALESCE(max(id),0)+1 FROM rec_served_impressions),'
                 '(SELECT COALESCE(max(impression_id),0)+1 FROM impression_file_writes),'
                 '(SELECT COALESCE(max(impression_id),0)+1 FROM served_diagnostic_retirements)) AS next_id '
                 'FROM served_fact_state WHERE id=1) WHERE next_id>1', (NEXT_ID_KEY,))
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    conn.execute(f'CREATE TRIGGER {MARKER} BEFORE UPDATE ON rec_served_impressions WHEN '
                 + ' OR '.join('NEW.' + name + ' IS NOT OLD.' + name for name in ('id', *served_facts.FIELDS))
                 + " OR NEW.diagnostic_expired<OLD.diagnostic_expired "
                   "BEGIN SELECT RAISE(ABORT,'Captured served facts are immutable'); END")
    conn.execute('DROP INDEX ix_rec_served_video_at')
    video = served_facts.expression('video_id', "json_extract(provenance,'$.candidate.video_id')")
    kind = served_facts.expression('policy_kind', "COALESCE(json_extract(provenance,'$.policy_kind'),'live')")
    conn.execute(f'CREATE INDEX ix_rec_served_video_at ON rec_served_impressions(({kind}),({video}),served_at,id)')
    conn.execute('CREATE INDEX ix_mode_pick_mode_time ON playlists(picked_mode_id,mode_picked_at) '
                 'WHERE picked_mode_id IS NOT NULL')


def validate(conn):
    installed = objects(conn, (MARKER, *RETIRED))
    if MARKER not in installed or set(installed).intersection(RETIRED):
        raise MigrationError('Served impression storage was not folded')
