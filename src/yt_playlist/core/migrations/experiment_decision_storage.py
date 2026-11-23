"""Move the bounded experiment history and its evidence into SQLite."""
from .. import collection_recovery, experiment_decision_state
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'keep_experiment_decisions_and_evidence_in_settings'
RETIRED = 'experiment_decisions'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL '
                                         'ORDER BY type,name', (RETIRED,))]


def install(conn, _context):
    records = []
    for row in conn.execute(f'SELECT * FROM {RETIRED} ORDER BY id'):
        evidence = collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
                                            kind='experiment-decision', owner=row['id'])
        records.append(dict(decision=row['decision'], cohort=row['cohort'], reason=row['reason'], at=row['created_at'],
                            powered=bool(row['powered']), basis=row['basis'], live_evidence=evidence))
    experiment_decision_state.save(conn, records)
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute('UPDATE collection_file_writes SET ownership_revision=ownership_revision+1 WHERE id=0')


def validate(conn):
    if definitions(conn, (RETIRED,)):
        raise MigrationError('Experiment decisions still have a dedicated table')
