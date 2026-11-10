"""Explicit database checks; opening the application does not run this audit."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from .audit import audit
from .runner import MigrationError
from . import regular


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('audit', 'upgrade-schema'))
    parser.add_argument('database', type=Path)
    args = parser.parse_args(argv)
    # Neither operation creates a missing database. Auditing additionally opens
    # read-only; regular changes never invoke the historical conversion runner.
    mode = 'ro' if args.operation == 'audit' else 'rw'
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri() + '?mode=' + mode,
                                     uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.operation == 'audit':
                result = audit(conn)
                print(f'Integrity audit passed at schema {result.version} ({result.elapsed:.3f}s)')
            else:
                regular.migrate(conn)
                version = conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
                print(f'Regular schema changes applied; legacy layout remains {version}')
    except (MigrationError, sqlite3.Error) as exc:
        label = 'Integrity audit' if args.operation == 'audit' else 'Regular schema upgrade'
        parser.exit(1, f'{label} failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
