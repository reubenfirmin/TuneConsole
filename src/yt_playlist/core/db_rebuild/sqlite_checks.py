"""SQLite integrity checks shared by candidate construction and installation."""


def verify(conn):
    result = [row[0] for row in conn.execute('PRAGMA integrity_check')]
    if result != ['ok']:
        raise ValueError(f'Database integrity check failed: {result[:5]}')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('Database foreign-key check failed')


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
