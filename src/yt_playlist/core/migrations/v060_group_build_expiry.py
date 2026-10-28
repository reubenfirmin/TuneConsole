"""Add the expiry lookup to databases that already applied group publication."""

SQL = 'CREATE INDEX ix_group_build_expiry ON rec_group_builds(expires_at)'


def apply(conn, context):
    # A checkout may already have the additive index from the initial v59 work.
    # Keep that index, while still validating its exact definition below.
    if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', ('ix_group_build_expiry',)).fetchone() is None:
        conn.execute(SQL)
    validate(conn)


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', ('ix_group_build_expiry',)).fetchone()
    if row is None or _tokens(row[0]) != _tokens(SQL):
        raise MigrationError('Incompatible group publication structure: ix_group_build_expiry')
