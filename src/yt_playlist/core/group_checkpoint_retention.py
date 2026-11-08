"""Keep the grouping worker's generation in the bounded retirement closure."""


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='group_preparation_state'").fetchone() is not None


def definitions(conn):
    if not available(conn):
        return
    for event, condition in (
        ('INSERT', 'NEW.generation_id IS NOT NULL'),
        ('DELETE', 'OLD.generation_id IS NOT NULL'),
        ('UPDATE OF generation_id', 'OLD.generation_id IS NOT NEW.generation_id'),
    ):
        name = 'group_retention_checkpoint_' + event.split()[0].lower()
        yield name, (f'CREATE TRIGGER {name} AFTER {event} ON group_preparation_state WHEN {condition} BEGIN '
                     'UPDATE group_retention_epoch SET revision=revision+1 WHERE id=1; END')
    name = 'retained_group_checkpoint_publication_delete'
    yield name, (f'CREATE TRIGGER {name} BEFORE DELETE ON group_publications WHEN EXISTS('
                 'SELECT 1 FROM group_preparation_state WHERE generation_id=OLD.generation_id) '
                 "BEGIN SELECT RAISE(ABORT,'Grouping worker checkpoint publication is immutable'); END")


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    from .schema_inspection import definitions as read_definitions
    expected = dict(definitions(conn))
    actual = read_definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or _tokens(actual[name]) != _tokens(sql):
            raise MigrationError('Incompatible grouping checkpoint retention: ' + name)


def install(conn, context=None):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    added = False
    for name, sql in definitions(conn):
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None:
            conn.execute(sql)
            added = True
        elif _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible grouping checkpoint retention: ' + name)
    if added:
        # A previously completed walk did not include this owner. Rebuild its
        # closure before allowing any further retirement, without scanning here.
        conn.execute('UPDATE group_retention_epoch SET revision=revision+1 WHERE id=1')
