"""Backup custody for installed metadata preparation, including unfinished stages.

The delivery owner retains every captured range, even after another stage or
the native owner takes precedence. Recovery relocates only file coordinates on
a disposable SQL copy; source identities, evidence and selected heads stay exact.
"""
from . import metadata_delivery_files as files
from .activity import check_cancellable_work
from .preparation import metadata_destinations as destinations
from .preparation.metadata_destinations import directory


def references(conn):
    return conn.execute('SELECT id,filename,digest,byte_size,byte_offset FROM '
                        + destinations.table('deliveries') + ' ORDER BY id')


def export(conn, root):
    """Caller pins the SQL snapshot and holds this owner's file lifetime fence."""
    return files._export(conn, root, directory=directory, references=references, contents=_contents)


def _contents(conn):
    return conn.execute('SELECT DISTINCT filename,digest,byte_size,byte_offset FROM '
                        + destinations.table('deliveries'))


def restore(conn, manifest):
    return files._restore(conn, manifest, directory=directory,
                          references=references, relocate=_relocate)


def _relocate(conn, staged):
    # This runs inside the generic restore transaction, after every range and
    # owner has been verified and every destination file is durable. SQLite DDL
    # rollback restores the guard if relocation or cancellation fails.
    destinations.validate_installed(conn)
    guard = destinations.table('deliveries_update')
    definition = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (guard,)).fetchone()[0]
    conn.execute('DROP TRIGGER ' + guard)
    for identity, filename, offset in staged.execute('SELECT o.id,c.target,c.target_offset '
            'FROM owners o JOIN contents c ON c.digest=o.digest ORDER BY o.id'):
        check_cancellable_work()
        conn.execute('UPDATE ' + destinations.table('deliveries')
                     + ' SET filename=?,byte_offset=? WHERE id=?', (filename, offset, identity))
    conn.execute(definition)
    destinations.validate_installed(conn)
