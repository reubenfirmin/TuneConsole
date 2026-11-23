"""Move comparison history and fitted estimates onto their existing owners."""
from itertools import groupby
import json

from .. import acousticbrainz_projection as estimate, acousticbrainz_vibe_schema as acousticbrainz, model_restore_artifact
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from . import acousticbrainz_storage


NAME = 'own_model_run_artifacts_and_acousticbrainz_estimates'
RUNS = 'rec_model_selection_runs'
RETIRED = ('rec_model_forecasts', 'rec_model_previous_vectors', acousticbrainz.ESTIMATES)
GUARDS = (acousticbrainz_storage.UPDATE, acousticbrainz_storage.DELETE)
OWNERS = (RUNS, 'recordings', acousticbrainz.SERVED, *GUARDS, *RETIRED, acousticbrainz_storage.RANK_INDEX)


def predecessor_objects(conn):
    return list(definitions(conn, OWNERS).values())


def install(conn, _context):
    conn.execute(f"ALTER TABLE {RUNS} ADD COLUMN forecast_series TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(forecast_series) IS 'array')")
    conn.execute(f'ALTER TABLE {RUNS} ADD COLUMN restore_artifact BLOB '
                 "CHECK(restore_artifact IS NULL OR (typeof(restore_artifact)='blob' AND length(restore_artifact)>0))")
    for run, in conn.execute(f'SELECT id FROM {RUNS}'):
        forecasts = list(map(tuple, conn.execute('SELECT made_at,forecast_json FROM rec_model_forecasts '
                                                'WHERE run_id=? ORDER BY made_at', (run,))))
        previous = conn.execute('SELECT identity_key,vec FROM rec_model_previous_vectors '
                                'WHERE run_id=? ORDER BY identity_key', (run,))
        conn.execute(f'UPDATE {RUNS} SET forecast_series=?,restore_artifact=? WHERE id=?',
                     (json.dumps(forecasts), model_restore_artifact.pack(previous), run))

    conn.execute('ALTER TABLE recordings ADD COLUMN ' + estimate.DECLARATION)
    rows = conn.execute('SELECT recording_id,vibe,score,inputs_version FROM acousticbrainz_vibe_estimates ORDER BY recording_id,vibe')
    for recording, fits in groupby(rows, key=lambda row: row[0]):
        payload = {vibe: [score, version] for _, vibe, score, version in fits}
        conn.execute(f'UPDATE recordings SET {estimate.COLUMN}=? WHERE id=?', (json.dumps(payload), recording))
    conn.execute(f'DROP VIEW {acousticbrainz.SERVED}')
    for name in GUARDS:
        conn.execute(f'DROP TRIGGER {name}')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    conn.execute(f'CREATE INDEX {estimate.INDEX} ON recordings(id) WHERE {estimate.PRESENT}')
    conn.execute(estimate.VIEW)
    for name in GUARDS:
        old = acousticbrainz_storage.SQL[name]
        conn.execute(old.replace(f'DELETE FROM {acousticbrainz.ESTIMATES} WHERE recording_id=OLD.recording_id',
                                 f"UPDATE recordings SET {estimate.COLUMN}='{{}}' WHERE id=OLD.recording_id AND {estimate.PRESENT}"))


def validate(conn):
    layout = definitions(conn, OWNERS + (estimate.INDEX,))
    if any(name in layout for name in RETIRED) or not set((RUNS, 'recordings', estimate.INDEX, *GUARDS)) <= layout.keys():
        raise MigrationError('Incomplete model run and AcousticBrainz consolidation')
    if estimate.COLUMN not in layout['recordings'] or 'restore_artifact' not in layout[RUNS] or 'forecast_series' not in layout[RUNS]:
        raise MigrationError('Missing model artifact or estimate owner')
    acousticbrainz._validate_relations(conn, {
        acousticbrainz.SERVED: (estimate.VIEW,),
        **{name: (sql,) for name, sql in acousticbrainz.IDENTIFIER_TRIGGERS.items()},
    })
