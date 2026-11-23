"""Installed schema coordinates for test-library captures and reports."""

LEDGERS = ('schema_migrations', 'schema_changes')
SCHEMA_FIELDS = ('schema', *LEDGERS)


def read_schema(connection):
    present = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name IN (?,?)", LEDGERS)}
    versions = {table: connection.execute(f'SELECT max(version) FROM {table}').fetchone()[0]
                if table in present else None for table in LEDGERS}
    regular = versions['schema_changes']
    return dict(schema=regular if regular is not None else versions['schema_migrations'], **versions)
