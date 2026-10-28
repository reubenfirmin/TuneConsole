"""Stage group generations without exposing partial history or replacing a live graph."""


def statements():
    yield ('CREATE TABLE rec_group_publications (generation TEXT PRIMARY KEY '
           'REFERENCES rec_group_generations(generation)) WITHOUT ROWID')
    yield ('CREATE TABLE rec_group_builds (generation TEXT PRIMARY KEY '
           'REFERENCES rec_group_generations(generation),owner TEXT NOT NULL,'
           'expires_at REAL NOT NULL,ready INTEGER NOT NULL DEFAULT 0 CHECK(ready IN (0,1))) WITHOUT ROWID')
    yield ('CREATE TABLE rec_group_graph (generation TEXT NOT NULL '
           'REFERENCES rec_group_generations(generation),left_key TEXT NOT NULL,right_key TEXT NOT NULL,'
           'weight REAL NOT NULL,support REAL NOT NULL,'
           'PRIMARY KEY(generation,left_key,right_key)) WITHOUT ROWID')
    yield ('CREATE VIEW rec_group_edges AS SELECT g.left_key,g.right_key,g.weight,g.support '
           'FROM rec_group_graph g JOIN rec_group_head h USING(generation) WHERE h.id=1')
    yield ('CREATE TABLE rec_group_assignments (identity_key TEXT NOT NULL,generation TEXT NOT NULL '
           'REFERENCES rec_group_generations(generation),version TEXT REFERENCES rec_group_definitions(version),'
           'similarity REAL NOT NULL,support REAL NOT NULL,profile_digest TEXT,prepared_at REAL NOT NULL,'
           'PRIMARY KEY(generation,identity_key)) WITHOUT ROWID')
    yield 'CREATE INDEX ix_group_lineage_target ON rec_group_lineage(target)'
    yield 'CREATE INDEX ix_group_definition_identity ON rec_group_definitions(group_id)'
    yield 'CREATE INDEX ix_group_definition_parent ON rec_group_definitions(parent_version)'

    generations = {
        'rec_group_generations': '{row}.generation',
        'rec_group_definitions': '{row}.generation',
        'rec_group_members': '(SELECT generation FROM rec_group_definitions WHERE version={row}.version)',
        'rec_group_unknown': '{row}.generation',
        'rec_group_lineage': '(SELECT generation FROM rec_group_definitions WHERE version={row}.target)',
    }
    for table, expression in generations.items():
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'immutable_{table}_{event.lower()}'
            sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
            condition = ' OR '.join('EXISTS (SELECT 1 FROM rec_group_publications WHERE generation='
                                    + expression.format(row=side) + ')' for side in sides)
            yield (f'CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {condition} '
                   "BEGIN SELECT RAISE(ABORT,'Musical group history is immutable'); END")
    for event in ('UPDATE', 'DELETE'):
        yield (f'CREATE TRIGGER immutable_group_publication_{event.lower()} BEFORE {event} '
               "ON rec_group_publications BEGIN SELECT RAISE(ABORT,'Musical group history is immutable'); END")
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER complete_group_head_{event.lower()} BEFORE {event} ON rec_group_head '
               'WHEN NOT EXISTS (SELECT 1 FROM rec_group_publications WHERE generation=NEW.generation) '
               "BEGIN SELECT RAISE(ABORT,'Musical group generation is unpublished'); END")
    for event in ('UPDATE', 'DELETE'):
        yield (f'CREATE TRIGGER immutable_group_identity_{event.lower()} BEFORE {event} '
               'ON rec_group_identities WHEN EXISTS (SELECT 1 FROM rec_group_definitions d '
               'JOIN rec_group_publications p USING(generation) WHERE d.group_id=OLD.group_id) '
               "BEGIN SELECT RAISE(ABORT,'Musical group history is immutable'); END")


def apply(conn, context):
    conn.execute('ALTER TABLE rec_group_edges RENAME TO old_group_edges')
    conn.execute('ALTER TABLE rec_group_assignments RENAME TO old_group_assignments')
    for table in ('rec_group_generations', 'rec_group_definitions', 'rec_group_members',
                  'rec_group_lineage', 'rec_group_unknown'):
        for event in ('update', 'delete'):
            conn.execute(f'DROP TRIGGER immutable_{table}_{event}')
    for sql in statements():
        conn.execute(sql)
    conn.execute('INSERT INTO rec_group_publications SELECT generation FROM rec_group_generations')
    conn.execute('INSERT INTO rec_group_graph SELECT h.generation,e.* FROM old_group_edges e '
                 'CROSS JOIN rec_group_head h WHERE h.id=1')
    conn.execute('INSERT INTO rec_group_assignments SELECT * FROM old_group_assignments')
    conn.execute('DROP TABLE old_group_edges')
    conn.execute('DROP TABLE old_group_assignments')


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    for sql in statements():
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible group publication structure: {name}')
