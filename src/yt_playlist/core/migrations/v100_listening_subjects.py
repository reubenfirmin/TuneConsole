"""Keep captured listening evidence separate from current canonical subjects."""
from pathlib import Path
import re
import sqlite3

from .runner import MigrationError


REPLACED = {'play_events','listening_observations','trend_first_play',
            'play_events_subject','play_events_listening_insert',
            'ix_play_events_key','ix_play_events_key_time','ix_play_event_account_key_time',
            'ix_listening_observation_subject_day','ix_listening_observation_source_scope'}


def transform(sql):
    match = re.match(r'CREATE (?:TABLE|VIEW|(?:UNIQUE )?INDEX|TRIGGER)\s+"?(\w+)"?',sql.lstrip(),re.I)
    if match and match[1] in REPLACED:
        return ''
    if re.match(r'CREATE VIEW\b',sql.lstrip(),re.I):
        sql = re.sub(r'\b(FROM|JOIN) play_events\b',r'\1 play_event_subjects',sql)
        sql = re.sub(r'\b(FROM|JOIN) listening_observations\b',r'\1 listening_observation_subjects',sql)
    if match and re.match(r'CREATE TRIGGER\b',sql.lstrip(),re.I) and re.search(
            r' ON (play_events|listening_observations)\b',sql):
        sql = sql.replace('OLD.identity_key','OLD.unresolved_subject_key').replace('NEW.identity_key','NEW.unresolved_subject_key')
    return sql


def definitions():
    pending = ''
    for line in Path(__file__).with_name('schema_listening_subjects.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete listening subject schema')


def fences():
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER play_event_membership_{event.lower()} BEFORE {event} ON play_events '
            'WHEN NEW.membership_ref_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM playlist_membership_refs m '
            'JOIN playlist_scopes s ON s.ref=m.scope_ref WHERE m.id=NEW.membership_ref_id AND m.identity_id=NEW.identity_id '
            "AND (NEW.playlist_ytm_id=s.provider_id OR NEW.playlist_ytm_id='RDAMPL'||s.provider_id)) "
            "BEGIN SELECT RAISE(ABORT,'Listening membership disagrees with captured account or playlist'); END")
    for table in ('play_events','play_event_sources','play_event_calendar','listening_observations',
                  'listening_calendars','catalog_entries','entry_artists','artist_redirects','recording_redirects'):
        for event in ('INSERT','UPDATE','DELETE'):
            yield (f'CREATE TRIGGER listening_summary_{table}_{event.lower()} AFTER {event} ON {table} BEGIN '
                'INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
                'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1; END')
    for table in ('artists','recordings'):
        yield (f'CREATE TRIGGER listening_summary_{table}_update AFTER UPDATE ON {table} BEGIN '
            'INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
            'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1; END')


def apply(conn,context):
    from . import current
    originals = {t:[dict(r) for r in conn.execute('SELECT * FROM '+t)]
                 for t in ('play_events','listening_observations')}
    for table,rows in originals.items():
        for row in rows:
            if row['recording_id'] is not None and not conn.execute(
                    'SELECT 1 FROM recordings WHERE id=? AND identity_key=?',
                    (row['recording_id'],row['identity_key'])).fetchone():
                raise MigrationError(f'Listening subject disagrees with recording: {table} {row["id"]}')
    highwater = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='listening_observations'").fetchone()
    # Drop dependent projections before replacing their owners. Only their SQL
    # changes; unrelated tables, evidence and files remain untouched.
    objects = list(conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND type<>'table'"))
    dropped = set()
    for row in objects:
        if (row['type']=='view' or row['tbl_name'] in originals or row['name'] in REPLACED):
            conn.execute(f'DROP {row["type"]} "{row["name"]}"')
            dropped.add(row['name'])
    for table in (*originals,'trend_first_play'):
        conn.execute('DROP TABLE '+table)
    for sql in definitions():
        if re.match(r'CREATE TABLE\b',sql.lstrip(),re.I):
            conn.execute(sql)
    for table,rows in originals.items():
        columns = [r[1] for r in conn.execute('PRAGMA table_info('+table+')')]
        values = []
        for row in rows:
            row['unresolved_subject_key'] = row['identity_key'] if row['recording_id'] is None else None
            if table=='play_events':
                memberships = conn.execute('SELECT m.id FROM playlist_membership_refs m JOIN playlist_scopes s ON s.ref=m.scope_ref '
                    "WHERE m.identity_id=? AND (s.provider_id=? OR 'RDAMPL'||s.provider_id=?)",
                    (row['identity_id'],row['playlist_ytm_id'],row['playlist_ytm_id'])).fetchall()
                row['membership_ref_id'] = memberships[0][0] if len(memberships)==1 else None
            values.append(tuple(row.get(c) for c in columns))
        conn.executemany('INSERT INTO '+table+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',values)
    if highwater:
        if conn.execute("SELECT 1 FROM sqlite_sequence WHERE name='listening_observations'").fetchone():
            conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='listening_observations'",(highwater[0],))
        else:
            conn.execute("INSERT INTO sqlite_sequence VALUES ('listening_observations',?)",(highwater[0],))
    for sql in current.statements(version=100):
        match = re.match(r'CREATE (VIEW|(?:UNIQUE )?INDEX|TRIGGER)\s+"?(\w+)"?',sql.lstrip(),re.I)
        if match and (match[2] in dropped or not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(match[2],)).fetchone()):
            conn.execute(sql)
    # Listening replacement drops all views while rebuilding its owners. A
    # separately selected discovery publication still owns its retained inputs
    # on v100, so restore its finite projections until native v101 takes over.
    from ..preparation import discovery_publication, discovery_views
    from .v086_genre_classifications import transform as classified
    for publication in discovery_publication.OWNERS.values():
        for name, query in discovery_views.definitions(publication.table, classified=publication.name == 'classified'):
            if publication.table(name) in dropped:
                sql = 'CREATE VIEW '+publication.table(name)+' AS '+query
                conn.execute(classified(sql) if publication.name == 'legacy' else sql)
    # The additive learning map also owns a finite head projection. Preserve
    # its retained evidence even though native v93 already owns runtime reads.
    from ..preparation import choice_publication
    if choice_publication.CURRENT in dropped:
        conn.execute(next(sql for name,sql in choice_publication.OWNER.definitions()
            if name==choice_publication.CURRENT))
    from ..preparation import conflict_publication
    if conflict_publication.VIEW in dropped:
        conn.execute(next(sql for name,sql in conflict_publication.OWNER.definitions()
            if name==conflict_publication.VIEW))
    from ..preparation import metadata_publication
    for name, sql in metadata_publication.views():
        if name in dropped:
            conn.execute(sql)
    from ..preparation import genre_preference_publication
    for name, sql in genre_preference_publication.definitions():
        if name == genre_preference_publication.table('preferences') and name in dropped:
            conn.execute(sql)
    conn.execute("DELETE FROM settings WHERE key IN ('trend_first_play_watermark','trend_first_play_source')")
