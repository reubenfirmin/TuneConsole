"""Install the library lookup omitted by an earlier installed v52 definition."""

SQL = ("CREATE INDEX ix_group_library_inputs ON rec_prepared_content(identity_key) "
       "WHERE json_type(payload,'$.library')='object'")


def apply(conn, context):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ix_group_library_inputs'").fetchone() is None:
        conn.execute(SQL)


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='ix_group_library_inputs'").fetchone()
    if row is None or _tokens(row[0]) != _tokens(SQL):
        raise MigrationError('Incompatible group library lookup: ix_group_library_inputs')
