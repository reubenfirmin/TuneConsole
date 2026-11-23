"""Per-field metadata lineage on the recording or upload that uses it."""
from .metadata_values import ITEM_FIELDS, RECORDING_FIELDS

VIEW = 'current_metadata_selections'
COLUMN = 'metadata_selections'
OWNERS = {'entry': 'track_sources', 'recording': 'recordings'}
FIELDS = {'entry': ITEM_FIELDS, 'recording': (*RECORDING_FIELDS, 'energy')}
DETAILS = ('observation_id', 'attribution_status', 'legacy_source', 'selected_at', 'runtime_provider_id')


def fields_sql(scope):
    return ','.join(f"'{field}'" for field in dict.fromkeys(FIELDS[scope]))


def projection(scope):
    columns = ','.join(f"selection.value->>'$[{index}]' AS {column}" for index, column in enumerate(DETAILS))
    recording, entry = ('subject.id', 'NULL') if scope == 'recording' else ('NULL', 'subject.id')
    return (f'SELECT {recording} AS recording_id,{entry} AS entry_id,selection.key AS field,{columns} '
            f'FROM {OWNERS[scope]} subject CROSS JOIN json_each(subject.{COLUMN}) selection')


def read_view():
    return f'CREATE VIEW {VIEW} AS ' + ' UNION ALL '.join(map(projection, OWNERS))


def save_runtime(conn, scope, identity, field, provider_id):
    owner = OWNERS[scope]
    path = '$.' + field
    conn.execute(f'''UPDATE {owner} SET {COLUMN}=json_set({COLUMN},?,
        json_set(COALESCE({COLUMN}->?,json_array(NULL,NULL,NULL,NULL,NULL)),'$[4]',?)) WHERE id=?''',
        (path, path, provider_id, identity))


def save_evidence(conn, scope, identity, field, observation_id, source, selected_at):
    owner = OWNERS[scope]
    path = '$.' + field
    status = 'evidenced' if observation_id is not None else 'legacy_unresolved'
    # Encode the timestamp at SQLite's full precision before storing it in JSON.
    return conn.execute(f'''UPDATE {owner} SET {COLUMN}=json_set({COLUMN},?,json_array(?,?,?,
        json(CASE WHEN ? IS NOT NULL THEN printf('%!.26g',?) END),{COLUMN}->>?)) WHERE id=?
        AND (({COLUMN}->>?) IS NOT ? OR ({COLUMN}->>?) IS NOT ? OR ({COLUMN}->>?) IS NOT ?)''',
        (path, observation_id, status, source, selected_at, selected_at, path + '[4]', identity,
         path + '[0]', observation_id, path + '[1]', status, path + '[2]', source)).rowcount == 1
