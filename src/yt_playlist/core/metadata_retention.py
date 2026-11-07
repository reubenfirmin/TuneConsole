"""Retire installed metadata history after compact runtime activation.

Current facts and explanation documents have already been verified at cutover.
Keep compatibility DDL and allocation high-water marks; only the obsolete SQL
owners and their explicitly audited nullable custody links are omitted.
"""
from . import metadata_runtime

RETIRED = frozenset(('metadata_deliveries', 'metadata_observations', 'metadata_legacy_records',
    'metadata_capture_heads', 'recording_metadata_selections', 'entry_metadata_selections', 'metadata_choice_heads',
    'metadata_conflicts', 'metadata_conflict_candidates', 'metadata_hints', 'provider_attempts', 'provider_attempt_fields',
    'provider_state_fields', 'native_metadata_observations', 'musical_source_observations'))
NULLS = {'artist_credit_observations': frozenset(('metadata_delivery_id',)),
         'recording_identifiers': frozenset(('delivery_id', 'observation_id'))}


def plan(conn):
    """Return copy predicates and projections, refusing unaudited FK owners."""
    if not metadata_runtime.active(conn):
        return {}, {}
    tables = {name for (name,) in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table'")}
    for table in sorted(tables - RETIRED):
        quoted = '"' + table.replace('"', '""') + '"'
        columns = {row[1]: row for row in conn.execute('PRAGMA main.table_xinfo(' + quoted + ')')}
        for row in conn.execute('PRAGMA main.foreign_key_list(' + quoted + ')'):
            if row[2] in RETIRED:
                if row[3] not in NULLS.get(table, ()) or columns[row[3]][3]:
                    raise ValueError('Metadata retirement needs a new reference audit: ' + table + '.' + row[3])
    # Credits need durable replay outcomes before their delivery custody is
    # removed. A receipt preserves the distinction between bound and unbound
    # delivery keys even after the original delivery row is gone.
    if conn.execute('SELECT 1 FROM artist_credit_observations o WHERE o.delivery_key IS NOT NULL '
        'AND o.metadata_delivery_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM artist_credit_replays r '
        'WHERE r.entry_id=o.entry_id AND r.source=o.source AND r.delivery_key=o.delivery_key '
        'AND r.observation_id=o.id AND r.credit_id=o.credit_id AND r.metadata_bound=1) LIMIT 1').fetchone():
        raise ValueError('Metadata retirement requires prepared artist replay receipts')
    return ({table: '0' for table in RETIRED & tables},
            {table: {column: 'NULL' for column in columns} for table, columns in NULLS.items() if table in tables})
