"""Explicit retirement of empty installed metadata locator storage (#682).

Run only on a verified compact destination. Installed and partially prepared
databases retain their original schema and writers. No startup calls this path.
The source database and historical file custody stay with the copy/rebuild job.
"""
import argparse
from pathlib import Path

from .. import metadata_retention, metadata_runtime, metadata_runtime_files, policy_documents
from ..activity import check_cancellable_work
from ..metadata_locator_contract import GUARDS, REMOVED, TABLE, retired
from ..schema_contract import tokens

COLUMNS = frozenset(('contract_digest', 'raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset'))
DEPENDENCIES = REMOVED | {'metadata_deliveries', 'immutable_metadata_delivery', 'owned_metadata_legacy_record'}


def _eligible(conn):
    from ..db_rebuild.installed_format import validate as validate_installed
    validate_installed(conn)
    metadata_runtime.validate(conn)
    if not metadata_runtime.active(conn):
        raise ValueError('Activate verified runtime metadata before locator retirement')
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='_database_rebuild_checkpoint'").fetchone():
        raise ValueError('Finish the database rebuild before locator retirement')
    metadata_retention.plan(conn)  # Includes external FK and credit replay audits.
    for table in metadata_retention.RETIRED:
        check_cancellable_work()
        if conn.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():
            raise ValueError('Retain populated installed metadata until verified compaction: '+table)
    for name, kind, owner, sql in conn.execute(
            "SELECT name,type,tbl_name,sql FROM main.sqlite_schema WHERE sql IS NOT NULL"):
        unknown_trigger = (kind == 'trigger' and owner == 'metadata_deliveries'
                           and name != 'immutable_metadata_delivery')
        if unknown_trigger or COLUMNS.intersection(tokens(sql)) and name not in DEPENDENCIES:
            raise ValueError('Locator retirement requires a new dependency audit: '+name)


def retire(conn, manifest):
    """Verify recovery custody on a snapshot, then atomically remove empty DDL.

    No data conversion or deletion occurs here. The copy/rebuild must already
    have reconciled continuing writes, preserved histories and retired rows.
    Verification does not hold a writer reservation; a data-version fence
    rejects any intervening commit. A failed DDL transaction retains the entire
    installed contract, and a retry after commit recognizes the guarded format.
    """
    if conn.in_transaction:
        raise ValueError('Locator retirement requires an idle connection')
    if Path(manifest).resolve().is_relative_to(metadata_runtime_files.directory(conn).resolve()):
        raise ValueError('Locator retirement requires independent recovery custody')
    if retired(conn):
        from ..db_rebuild.installed_format import validate as validate_installed
        validate_installed(conn)
        return {'retired': True, 'already_retired': True}
    changes = conn.total_changes
    version = conn.execute('PRAGMA data_version').fetchone()[0]
    with policy_documents.retained_reader(metadata_runtime_files.directory(conn)):
        with conn:
            conn.execute('BEGIN')
            _eligible(conn)
            members = metadata_runtime_files.verify_backup(conn, manifest)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.total_changes != changes or conn.execute('PRAGMA data_version').fetchone()[0] != version:
                raise ValueError('Metadata changed during retirement verification; retry')
            indexes = [sql for name, sql in conn.execute("SELECT name,sql FROM main.sqlite_schema "
                "WHERE tbl_name='metadata_deliveries' AND type='index' AND sql IS NOT NULL") if name not in REMOVED]
            sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='metadata_deliveries'").fetchone()
            conn.execute('DROP TRIGGER owned_metadata_legacy_record')
            conn.execute('DROP TABLE metadata_deliveries')
            conn.execute(TABLE)
            for sql in indexes:
                conn.execute(sql)
            for sql in GUARDS.values():
                conn.execute(sql)
            if sequence:
                conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('metadata_deliveries',?)", (sequence[0],))
            check_cancellable_work()
            from ..db_rebuild.installed_format import validate as validate_installed
            validate_installed(conn)
    return {'retired': True, 'verified_recovery_members': members, 'removed_columns': sorted(COLUMNS),
            'removed_indexes': sorted(REMOVED)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path, help='Isolated, verified compact destination')
    parser.add_argument('--recovery-manifest', type=Path, required=True)
    args = parser.parse_args()
    from ..store import Store
    import json
    store = Store(args.database)
    try:
        print(json.dumps(retire(store.conn, args.recovery_manifest), sort_keys=True))
    finally:
        store.conn.close()


if __name__ == '__main__':
    main()
