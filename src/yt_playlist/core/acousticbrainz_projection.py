"""Current fitted estimates on recordings, independent of their selected Vibe fits."""
import json

from . import acousticbrainz_vibe_schema as schema
from .audio_vibe_contract import ACOUSTICBRAINZ_ESTIMATE


COLUMN = 'acousticbrainz_estimates'
INDEX = 'ix_recording_acousticbrainz_estimates'
VIBES = ('cruise', 'release', 'relax', 'uplift', 'energize', 'concentrate')
PRESENT = f"{COLUMN}<>'{{}}'"
DECLARATION = (
    f"{COLUMN} TEXT NOT NULL DEFAULT '{{}}' CHECK(json_type({COLUMN}) IS 'object' "
    f"AND json_remove({COLUMN},{','.join(repr('$.' + vibe) for vibe in VIBES)})='{{}}')"
)
VERIFIED_MATCH = schema._verified_match(f' INDEXED BY {schema.MATCH_INDEX}')
VIEW = f"""CREATE VIEW {schema.SERVED} AS
    SELECT {schema.FIT_COLUMNS} FROM vibe_recording_fits WHERE source NOT IN ('provider_measurement','heuristic')
    UNION ALL
    SELECT r.id AS recording_id,e.key,e.value->>0,1.0,'{ACOUSTICBRAINZ_ESTIMATE}',NULL,NULL,
           e.value->>1,NULL,NULL,NULL,NULL
    FROM ({schema.LINKED_CLASSIFIERS}) i JOIN recordings r ON r.id=i.recording_id
    JOIN json_each(r.{COLUMN}) e
    WHERE r.{PRESENT} AND {VERIFIED_MATCH}"""


def put(conn, recording, vibe, score, version, *, existing=False):
    if vibe not in VIBES or not 0 <= score <= 1 or not isinstance(version, str):
        raise ValueError('Invalid AcousticBrainz estimate')
    path = '$.' + vibe
    clause = f' AND json_type({COLUMN},?) IS NOT NULL' if existing else ''
    parameters = (path, json.dumps((score, version)), recording, *((path,) if existing else ()))
    result = conn.execute(f'UPDATE recordings SET {COLUMN}=json_set({COLUMN},?,json(?)) WHERE id=?' + clause,
                          parameters)
    if not existing and result.rowcount != 1:
        raise ValueError('AcousticBrainz estimate has no recording')


def remove(conn, rows):
    conn.executemany(f'UPDATE recordings SET {COLUMN}=json_remove({COLUMN},?) WHERE id=? AND {PRESENT}',
                     (('$.' + vibe, recording) for recording, vibe in rows))
