"""Exact file custody for retained installed Radio offers and model reports."""
from . import recommendation_outputs as files
from .activity import check_cancellable_work
from .preparation import saved_outputs as preparation, saved_output_publication as publication
from .preparation.saved_outputs import directory


OWNERS = {preparation.tables(source)[0]: source for source in preparation.PREFIXES}


def available(conn):
    return any(conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (owner,)).fetchone()
               for owner in OWNERS)


def references(conn):
    for owner, source in OWNERS.items():
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (owner,)).fetchone():
            # Each table has one fixed source. Revision zero is a valid owner;
            # the backup codec uses positive identities, without changing SQL IDs.
            for revision, filename, digest, size in conn.execute(
                    f'SELECT preparation_revision,filename,content_digest,byte_size FROM {owner} '
                    'ORDER BY preparation_revision'):
                yield owner, revision + 1, source, '', filename, digest, size


def export(conn, root):
    return files._export(conn, root, directory=directory, references=references)


def restore(conn, manifest):
    return files._restore(conn, manifest, directory=directory, references=references,
                          owners=OWNERS, relocate=_relocate)


def _relocate(conn, restored):
    # Recovery owns this disposable connection. Suspend only the validated file
    # owner's update guards inside the restore transaction, retaining selected
    # heads, custody epochs, membership order and unresolved references exactly.
    publication.validate_installed(conn)
    guards = []
    for owner, source in OWNERS.items():
        for name in (owner + '_published_update', owner + '_custody_update'):
            row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
            if row is not None:
                guards.append(row[0])
                conn.execute('DROP TRIGGER ' + name)
    for (owner, identity), filename in restored.items():
        check_cancellable_work()
        conn.execute(f'UPDATE {owner} SET filename=? WHERE source=? AND preparation_revision=?',
                     (filename, OWNERS[owner], identity - 1))
    for definition in guards:
        conn.execute(definition)
    publication.validate_installed(conn)
