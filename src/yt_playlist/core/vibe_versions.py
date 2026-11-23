"""Exact Vibe version identities and application-facing text projections."""
import re

COLUMNS = {'measurements': 'model_version', 'predictions': 'policy_version',
           'recording_vibes': 'inputs_version'}


def definitions():
    yield 'vibe_versions', 'CREATE TABLE vibe_versions (id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE)'
    yield 'vibe_versions_immutable', ("CREATE TRIGGER vibe_versions_immutable BEFORE UPDATE ON vibe_versions "
        "BEGIN SELECT RAISE(ABORT,'Vibe version identities are immutable'); END")


def install(conn, context=None):
    for _, sql in definitions():
        conn.execute(sql)


def validate(conn):
    from .schema_contract import tokens as _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise ValueError('Incompatible vibe version owner: ' + name)


def normalized(conn):
    return any(row[1] == 'model_version_id' for row in conn.execute('PRAGMA main.table_info(measurements)'))


def column(conn, table):
    return COLUMNS[table] + ('_id' if normalized(conn) else '')


def intern(conn, version):
    if not isinstance(version, str):
        raise ValueError('Vibe version must retain its exact text identity')
    conn.execute('INSERT INTO vibe_versions(value) VALUES (?) ON CONFLICT(value) DO NOTHING', (version,))
    return conn.execute('SELECT id FROM vibe_versions WHERE value=?', (version,)).fetchone()[0]


def stored(conn, version):
    return intern(conn, version) if normalized(conn) else version


_APPLICATION_RELATIONS = (
    ('measurements', 'vibe_measurements',
     'id recording_id vibe score support source model_version input_digest measured_at'),
    ('predictions', 'vibe_predictions',
     'id recording_id vibe score support subgenre_key kind lineage_complete policy_version predicted_at'),
    ('recording_vibes', 'vibe_recording_fits',
     'recording_id vibe score support source measurement_id prediction_id inputs_version artist_key subgenre_key recordings variance'),
)


def application_definitions(conn):
    normalized_storage = normalized(conn)
    for table, view, fields in _APPLICATION_RELATIONS:
        fields = fields.split()
        version = COLUMNS[table]
        stored_column = version + '_id' if normalized_storage else version
        projection = [f't.{field}' for field in fields]
        version_index = fields.index(version)
        if normalized_storage:
            projection[version_index] = f'(SELECT value FROM vibe_versions WHERE id=t.{stored_column}) AS {version}'
        yield view, f'CREATE VIEW {view} AS SELECT {",".join(projection)} FROM {table} t'

        columns = list(fields)
        columns[version_index] = stored_column
        values = [f'NEW.{field}' for field in fields]
        prepare = ''
        if normalized_storage:
            prepare = f'INSERT INTO vibe_versions(value) VALUES (NEW.{version}) ON CONFLICT(value) DO NOTHING; '
            values[version_index] = f'(SELECT id FROM vibe_versions WHERE value=NEW.{version})'
        if table == 'predictions':
            values[fields.index('lineage_complete')] = 'COALESCE(NEW.lineage_complete,1)'
        conflict = ''
        if table == 'recording_vibes':
            assignments = ','.join(f'{col}=excluded.{col}' for col in columns[2:])
            conflict = f' ON CONFLICT(recording_id,vibe) DO UPDATE SET {assignments}'
        trigger = view + '_insert'
        yield trigger, (f'CREATE TRIGGER {trigger} INSTEAD OF INSERT ON {view} BEGIN {prepare}'
                        f'INSERT INTO {table}({",".join(columns)}) VALUES ({",".join(values)}){conflict}; END')


def install_application_relations(conn, context=None):
    for _, sql in application_definitions(conn):
        conn.execute(sql)


def validate_application_relations(conn):
    from .schema_contract import tokens
    for name, sql in application_definitions(conn):
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise ValueError('Incompatible vibe application relation: ' + name)


def drop_application_relations(conn):
    for _, view, _ in _APPLICATION_RELATIONS:
        conn.execute(f'DROP VIEW {view}')


def transform(sql, table):
    name = COLUMNS[table]
    sql = re.sub(r'\b'+name+r'\b', name+'_id', sql)
    return sql.replace(name+'_id TEXT NOT NULL', name+'_id INTEGER NOT NULL REFERENCES vibe_versions(id)')
