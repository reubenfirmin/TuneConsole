"""Current metadata facts in SQL; alternatives and explanations in keyed JSON.

Provider evidence keeps its current data owner until explicit preparation.
The SQL owners below are bounded by current subjects/fields, not lookup count.
"""
import json
import sqlite3
import time

from . import content_work, metadata_selections, metadata_runtime_files as files
from . import runtime_metadata_choices as choices
from .catalog_identity import RECORDING_FIELDS, ITEM_FIELDS
from .metadata_contract import ACTIVE, ALTERNATIVE_SECONDS, PROVIDER_FIELDS, Capture, clock, runtime_active as active

from .metadata_values import RUNTIME_CODES as CODES, RUNTIME_FIELDS as FIELDS
from .recommendation_invalidation import queue_refresh

CONFLICT_STATUS = ('unresolved', 'user_resolved', 'provider_converged')
CONFLICT_COLUMNS = ('runtime_id AS id,recording_id,entry_id,field,'
                    "CASE status WHEN 'user_resolved' THEN 1 WHEN 'provider_converged' THEN 2 ELSE 0 END AS status,"
                    'runtime_revision AS revision,updated_at')


def reserve(conn):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    else:
        conn.execute('UPDATE settings SET value=value WHERE 0')


def next_identity(conn, table):
    return conn.execute('SELECT max(COALESCE((SELECT max(id) FROM '+table+'),0),'
        'COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0))+1', (table,)).fetchone()[0]


def code(field):
    return CODES['mb_year' if field == 'year' else field]


def definitions(*, entry_owner='catalog_entries'):
    yield 'metadata_runtime_state', '''CREATE TABLE metadata_runtime_state (
        id INTEGER PRIMARY KEY CHECK(id=1),active INTEGER NOT NULL CHECK(active IN (0,1)),
        gc_cursor INTEGER NOT NULL DEFAULT 0,gc_after REAL NOT NULL DEFAULT 0,
        gc_shard INTEGER NOT NULL DEFAULT 0 CHECK(gc_shard BETWEEN 0 AND 99))'''
    yield 'metadata_runtime_providers', 'CREATE TABLE metadata_runtime_providers (id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE)'
    yield 'metadata_runtime_genres', 'CREATE TABLE metadata_runtime_genres (id INTEGER PRIMARY KEY,genre_key TEXT NOT NULL UNIQUE)'
    yield 'metadata_runtime_recording_genres', '''CREATE TABLE metadata_runtime_recording_genres (
        recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,genre_id INTEGER REFERENCES metadata_runtime_genres(id),
        unresolved TEXT,CHECK(genre_id IS NULL OR unresolved IS NULL))'''
    for scope, owner in (('recording', 'recordings'), ('entry', entry_owner)):
        name = 'metadata_runtime_' + scope + '_sources'
        yield name, f'''CREATE TABLE {name} ({scope}_id INTEGER NOT NULL REFERENCES {owner}(id) ON DELETE CASCADE,
            field INTEGER NOT NULL CHECK(field BETWEEN 1 AND {len(FIELDS)}),
            provider_id INTEGER NOT NULL REFERENCES metadata_runtime_providers(id),
            PRIMARY KEY({scope}_id,field)) WITHOUT ROWID'''
    yield 'metadata_runtime_choices', f'''CREATE TABLE metadata_runtime_choices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,entry_id INTEGER NOT NULL REFERENCES {entry_owner}(id) ON DELETE CASCADE,
        field INTEGER NOT NULL CHECK(field BETWEEN 1 AND {len(FIELDS)}),
        provider_id INTEGER NOT NULL REFERENCES metadata_runtime_providers(id),revision INTEGER NOT NULL CHECK(revision>0),
        kind INTEGER NOT NULL CHECK(kind BETWEEN 0 AND 6),text_value TEXT,number_value,
        genre_id INTEGER REFERENCES metadata_runtime_genres(id),UNIQUE(entry_id,field),
        CHECK((kind IN (0,1) AND text_value IS NULL AND number_value IS NULL AND genre_id IS NULL)
           OR (kind IN (2,3,6) AND text_value IS NOT NULL AND number_value IS NULL AND genre_id IS NULL)
           OR (kind=4 AND text_value IS NULL AND typeof(number_value) IN ('integer','real') AND genre_id IS NULL)
           OR (kind=5 AND text_value IS NULL AND number_value IS NULL AND genre_id IS NOT NULL)))'''
    yield 'metadata_runtime_conflicts', f'''CREATE TABLE metadata_runtime_conflicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,recording_id INTEGER REFERENCES recordings(id) ON DELETE CASCADE,
        entry_id INTEGER REFERENCES {entry_owner}(id) ON DELETE CASCADE,field INTEGER NOT NULL CHECK(field BETWEEN 1 AND {len(FIELDS)}),
        status INTEGER NOT NULL CHECK(status IN (0,1,2)),revision INTEGER NOT NULL CHECK(revision>0),updated_at REAL NOT NULL,
        CHECK((recording_id IS NULL)<>(entry_id IS NULL)))'''
    for scope in ('recording', 'entry'):
        name = 'ix_metadata_runtime_conflict_' + scope
        yield name, f'CREATE UNIQUE INDEX {name} ON metadata_runtime_conflicts({scope}_id,field) WHERE {scope}_id IS NOT NULL'


def installed(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_runtime_choices'").fetchone() is not None


def install(conn, *, enrichment_clock):
    if installed(conn):
        validate(conn, enrichment_clock=enrichment_clock)
        return
    for _, sql in definitions():
        conn.execute(sql)
    conn.execute('INSERT INTO metadata_runtime_state(id,active) VALUES (1,0)')


def validate(conn, *, enrichment_clock):
    from . import track_sources
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    from .schema_inspection import definitions as read_definitions
    entry_owner = 'track_sources' if track_sources.folded(conn) else 'catalog_entries'
    expected = dict(definitions(entry_owner=entry_owner))
    actual = read_definitions(conn, expected)
    for name, sql in expected.items():
        valid = {_tokens(sql)}
        if name == 'metadata_runtime_state':
            valid.add(_tokens(sql[:-1] + ', ' + enrichment_clock + ')'))
        if name not in actual or _tokens(actual[name]) not in valid:
            raise MigrationError('Incompatible runtime metadata object: ' + name)
    state = conn.execute('SELECT active FROM metadata_runtime_state WHERE id=1').fetchone()
    if state is None:
        raise MigrationError('Missing runtime metadata state')
    if state[0]:
        from . import catalog_metadata
        expected = views(include_choices=not catalog_metadata.installed(conn) or not catalog_metadata.ready(conn),
                         entry_owner=entry_owner)
        actual = read_definitions(conn, expected)
        for name, sql in expected.items():
            if name not in actual or _tokens(actual[name]) != _tokens(sql):
                raise MigrationError('Incompatible runtime metadata view: ' + name)


def provider_id(conn, name, *, create=True):
    row = conn.execute('SELECT metadata_provider_id FROM enrichment_provider_config WHERE provider=?', (name,)).fetchone()
    if row and row[0] is not None:
        return row[0]
    if not create:
        return None
    if not isinstance(name, str) or not 0 < len(name.encode()) <= 256:
        raise ValueError('Invalid metadata provider')
    return conn.execute('INSERT INTO enrichment_provider_config(provider,metadata_provider_id) '
        'VALUES (?,(SELECT COALESCE(max(metadata_provider_id),0)+1 FROM enrichment_provider_config)) '
        'ON CONFLICT(provider) DO UPDATE SET metadata_provider_id=COALESCE(enrichment_provider_config.metadata_provider_id,'
        'excluded.metadata_provider_id) RETURNING metadata_provider_id', (name,)).fetchone()[0]


def field_expression(alias):
    return 'CASE '+alias+'.field '+ ' '.join(f"WHEN {index+1} THEN '{name}'" for index, name in enumerate(FIELDS))+' END'


def value_expression(alias, *, historical=False):
    if historical:
        genre = ('SELECT g.name FROM metadata_runtime_genres m JOIN genre_catalog g ON g.key=COALESCE('
                 '(SELECT genre_key FROM genre_redirects WHERE retired_key=m.genre_key),m.genre_key) '
                 f'WHERE m.id={alias}.genre_id')
    else:
        genre = ('SELECT g.name FROM genre_catalog g WHERE g.key=COALESCE('
                 f'(SELECT redirect_key FROM genre_catalog WHERE key={alias}.genre_key),{alias}.genre_key)')
    return (f'CASE {alias}.kind WHEN 4 THEN {alias}.number_value WHEN 5 THEN ({genre}) '
            f'ELSE {alias}.text_value END')


def selection_view(scope, *, runtime, relation='recording_metadata_selections'):
    name = 'recording_field_sources' if scope == 'recording' else 'item_field_sources'
    subject = scope + '_id'
    if relation == metadata_selections.VIEW:
        relation = '(' + metadata_selections.projection(scope) + ')'
    if runtime:
        return (f'CREATE VIEW {name} AS SELECT s.{subject},s.field,p.provider AS source,NULL AS selected_at '
                f'FROM {relation} s JOIN enrichment_provider_config p '
                f'ON p.metadata_provider_id=s.runtime_provider_id WHERE s.{subject} IS NOT NULL')
    return (f'CREATE VIEW {name} AS SELECT s.{subject},s.field,COALESCE(d.provider,o.legacy_source,s.legacy_source) '
            f'AS source,s.selected_at FROM {relation} s '
            'LEFT JOIN metadata_observations o ON o.id=s.observation_id LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id '
            f'WHERE s.{subject} IS NOT NULL AND s.attribution_status IS NOT NULL')


def views(*, include_choices=True, entry_owner='catalog_entries', compact=False, selection_relation='recording_metadata_selections'):
    result = {}
    if include_choices:
        result['entry_field_choices'] = ('CREATE VIEW entry_field_choices AS SELECT c.entry_id,'+
            field_expression('c')+' AS field,json_quote('+value_expression('c', historical=True)+
            ') AS value_json,p.name AS source '
            'FROM metadata_runtime_choices c JOIN metadata_runtime_providers p ON p.id=c.provider_id WHERE c.kind<>0')
    for scope, view in (('recording', 'recording_field_sources'), ('entry', 'item_field_sources')):
        result[view] = selection_view(scope, runtime=True, relation=selection_relation) if compact else (
            f'CREATE VIEW {view} AS SELECT s.{scope}_id,'+field_expression('s')+
            ' AS field,p.name AS source,NULL AS selected_at FROM metadata_runtime_'+scope+'_sources s '
            'JOIN metadata_runtime_providers p ON p.id=s.provider_id')
    result['enrichment_conflict'] = ('CREATE VIEW enrichment_conflict AS SELECT c.id,e.id AS track_id,'
        "CASE WHEN c.field="+str(code('mb_year'))+" THEN 'year' ELSE "+field_expression('c')+
        " END AS field,'[]' AS candidates,c.status AS resolved,NULL AS resolved_value,c.updated_at,e.recording_id,"
        "e.video_id AS input_video_id,CASE WHEN c.recording_id IS NOT NULL THEN 'recording' ELSE 'item' END AS field_scope "
        f'FROM metadata_runtime_conflicts c JOIN {entry_owner} e ON e.id=c.entry_id OR e.recording_id=c.recording_id')
    if compact:
        result['enrichment_conflict'] = ('CREATE VIEW enrichment_conflict AS SELECT c.runtime_id AS id,e.id AS track_id,'
            "CASE c.field WHEN 'mb_year' THEN 'year' ELSE c.field END AS field,'[]' AS candidates,"
            "CASE c.status WHEN 'user_resolved' THEN 1 WHEN 'provider_converged' THEN 2 ELSE 0 END AS resolved,"
            'NULL AS resolved_value,c.updated_at,e.recording_id,e.video_id AS input_video_id,'
            "CASE WHEN c.recording_id IS NOT NULL THEN 'recording' ELSE 'item' END AS field_scope "
            f'FROM metadata_conflicts c JOIN {entry_owner} e ON e.id=c.entry_id OR e.recording_id=c.recording_id '
            'WHERE c.runtime_id IS NOT NULL')
    return result


def replace_views(conn, **layout):
    for name, sql in views(**layout).items():
        conn.execute('DROP VIEW '+name)
        conn.execute(sql)


def activate(conn):
    from . import source_metadata

    changed = not active(conn)
    replace_views(conn, include_choices=False, entry_owner='track_sources', compact=True,
                  selection_relation=metadata_selections.VIEW)
    conn.execute("UPDATE settings SET value='1' WHERE key=?", (ACTIVE,))
    if changed:
        source_metadata.refresh_range(conn)


def subject(conn, entry):
    row = conn.execute('SELECT recording_id,video_id FROM track_sources WHERE id=?', (entry,)).fetchone()
    if row is None:
        raise ValueError('Unknown metadata catalog input')
    return tuple(row)


def selected_source(conn, entry, field):
    recording, _ = subject(conn, entry)
    scope, identity = ('recording', recording) if field in RECORDING_FIELDS else ('entry', entry)
    view = 'recording_field_sources' if scope == 'recording' else 'item_field_sources'
    row = conn.execute(f'SELECT source FROM {view} WHERE {scope}_id=? AND field=?', (identity, field)).fetchone()
    return row[0] if row else None


def select(conn, entry, field, value, source, *, now=None, prepared=None):
    from . import genre_classifications, metadata_values
    from . import catalog_metadata
    if field not in CODES:
        raise ValueError('Unknown metadata field')
    if prepared is None:
        prepared = metadata_values.prepare(conn, field, value,
            unit=metadata_values.DEFAULT_UNIT.get(field) if value not in (None, '') else None)
    recording, _ = subject(conn, entry)
    if field in RECORDING_FIELDS:
        scope, identity = 'recording', recording
        if field == 'genre':
            genre_classifications.write_evidence(conn, 'recordings', recording, prepared)
        else:
            conn.execute(f'UPDATE recordings SET {field}=? WHERE id=? AND {field} IS NOT ?', (value, recording, value))
    else:
        scope, identity = 'entry', entry
        from . import track_sources
        track_sources.write_field(conn, entry, field, value)
    metadata_selections.save_runtime(conn, scope, identity, field, provider_id(conn, source))
    catalog_metadata.audit(conn, entry, recording, field, source, 'select', value,
                           time.time() if now is None else now, genre_key=prepared.genre_key)


def choose(conn, entry, field, value=None, *, withdraw=False, source='user', now=None, expected_recording_id=None,
           _prepared=None, _invalidate=True, _identity=None, _revision=None):
    from . import metadata_values, recording_metadata
    reserve(conn)
    recording, video = subject(conn, entry)
    if expected_recording_id is not None and recording != expected_recording_id:
        raise ValueError('Metadata decision input changed')
    at = time.time() if now is None else now
    prepared = (None if withdraw else _prepared if _prepared is not None else metadata_values.prepare(conn, field, value,
        unit=metadata_values.DEFAULT_UNIT.get(field) if value not in (None, '') else None))
    if withdraw:
        kind, text, number, genre = 0, None, None, None
    else:
        kind = {'null': 1, 'blank': 2, 'value': 4 if prepared.number is not None else 5 if prepared.genre_key else 3,
                'unresolved': 6}[prepared.kind]
        text, number, genre = prepared.text, prepared.number, prepared.genre_key
        if kind == 5:
            text = None
    previous = choices.head(conn, entry, code(field))
    if (_identity is not None or _revision is not None) and active(conn):
        raise ValueError('Explicit metadata revisions are only valid during preparation')
    revision = _revision if _revision is not None else previous['revision'] + 1 if previous else 1
    identity = (_identity if _identity is not None else previous['id'] if previous else
                choices.next_identity(conn))
    files.publish(conn, 'choices', (identity,), revision, {'entry_id':entry, 'recording_id':recording,
        'input_video_id':video, 'field':field, 'value':value, 'withdrawn':withdraw, 'source':source, 'at':at})
    choices.save(conn, entry, code(field), identity, provider_id(conn, source), revision, kind, text, number, genre)
    recording_metadata.audit_choice(conn, entry, recording, field, source,
                                  metadata_values.Value('withdrawn') if withdraw else prepared, at)
    if _invalidate:
        queue_refresh(conn)
        content_work.for_recording(conn, recording)
    return identity


def alternatives(conn, recording, provider, *, now=None):
    now = time.time() if now is None else now
    state = conn.execute("SELECT at FROM enrichment_provider_state WHERE recording_id=? AND provider=? AND status<>'queued'",
                         (recording, provider)).fetchone()
    identity = provider_id(conn, provider, create=False)
    if state is None or identity is None or state[0] <= now - ALTERNATIVE_SECONDS:
        return {}
    data = files.read(conn, 'alternatives', (recording, identity), float(state[0]), missing=True)
    return data or {}


def scoped_fields(conn, entry, data):
    recording, video = subject(conn, entry)
    if data.get('recording_id') != recording:
        return {}
    inputs = data.get('inputs', {})
    return {field: value for field, value in data.get('fields', {}).items()
            if field in RECORDING_FIELDS or inputs.get(field,
                [data.get('entry_id'), data.get('input_video_id')]) == [entry, video]}


def capture(conn, entry, provider, fields, *, run_id=None, now=None):
    from . import metadata_values
    if not isinstance(fields, dict) or fields.keys() - set(PROVIDER_FIELDS):
        raise ValueError('Unsupported provider result fields')
    if not isinstance(provider, str) or not 0 < len(provider.encode()) <= 4096:
        raise ValueError('A provider result needs a bounded provider identity')
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip() or len(run_id.encode()) > 4096):
        raise ValueError('A provider result needs a bounded run identity')
    selected = {}
    for field, value in fields.items():
        name = 'mb_year' if field == 'year' else field
        if name not in CODES:
            continue
        if name in selected and json.dumps(selected[name], allow_nan=False) != json.dumps(value, allow_nan=False):
            raise ValueError('Provider result has contradictory field aliases')
        selected[name] = value
        metadata_values.prepare(conn, name, value,
            unit=metadata_values.DEFAULT_UNIT.get(name) if value not in (None, '') else None)
    recording, video = subject(conn, entry)
    from .evidence_ledger import canonical
    encoded = canonical(fields)
    if len(encoded) > files.MAX_BYTES-4096:
        raise ValueError('Provider result exceeds its byte budget')
    return Capture(entry, recording, video, provider, encoded, run_id, clock(now))


def validate_capture(conn, entry, provider, fields, captured, *, run_id=None):
    if captured is None:
        return
    recording, video = subject(conn, entry)
    if (not isinstance(captured, Capture)
            or (captured.entry, captured.recording, captured.video, captured.provider) != (entry, recording, video, provider)
            or any(name not in captured.fields or json.dumps(captured.fields[name], sort_keys=True, allow_nan=False)
                   != json.dumps(value, sort_keys=True, allow_nan=False) for name, value in fields.items())
            or run_id is not None and captured.run != run_id):
        raise ValueError('Provider result has a different captured input or fields')


def record_attempt(conn, entry, provider, fields, *, cycle, retry=False, now=None, retry_after=0,
                   error='', run_id=None, delivery_id=None):
    now = float(clock(now))
    retry_after = clock(retry_after)
    if type(retry) is not bool:
        raise ValueError('Provider retry flag must be a boolean')
    if delivery_id is None:
        delivery_id = capture(conn, entry, provider, fields, run_id=run_id, now=now)
    recording, video = subject(conn, entry)
    validate_capture(conn, entry, provider, fields, delivery_id, run_id=run_id)
    if delivery_id is not None and delivery_id.fields != fields:
        raise ValueError('Provider completion must account for its whole captured result')
    previous = conn.execute("SELECT * FROM enrichment_provider_state WHERE recording_id=? AND provider=? AND status<>'queued'",
                            (recording, provider)).fetchone()
    prior = alternatives(conn, recording, provider, now=now)
    if previous and (now <= previous['at'] or delivery_id is not None and delivery_id.at <= previous['at']
                     or run_id and prior.get('run_id') == run_id):
        return False
    failures = ((previous['failures'] if previous and previous['cycle'] == cycle else 0) + 1) if retry else 0
    deadline = max(now + min(3600,30*2**min(failures-1,7)), retry_after) if retry else 0
    retained = {**(scoped_fields(conn, entry, prior) if previous and previous['cycle'] == cycle else {}), **fields}
    if 'year' in fields and 'mb_year' not in fields:
        retained.pop('mb_year', None)
    elif 'mb_year' in fields and 'year' not in fields:
        retained.pop('year', None)
    status = 'retry' if retry else 'found' if fields else 'no_match'
    files.publish(conn, 'alternatives', (recording,provider_id(conn,provider)), now,
        {'entry_id':entry,'recording_id':recording,'input_video_id':video,'provider':provider,
         'fields':retained,'run_id':run_id,'at':now,'expires_at':now+ALTERNATIVE_SECONDS,'error':error})
    conn.execute('INSERT INTO enrichment_provider_state(recording_id,provider,cycle,at,status,failures,retry_at,error) '
        "VALUES (?,?,?,?,?,?,?,'') ON CONFLICT(recording_id,provider) DO UPDATE SET cycle=excluded.cycle,at=excluded.at,"
        'status=excluded.status,failures=excluded.failures,retry_at=excluded.retry_at,error=excluded.error',
        (recording,provider,cycle,now,status,failures,deadline))
    return True


def conflict(conn, entry, field):
    recording, _ = subject(conn,entry)
    return conn.execute(f'SELECT {CONFLICT_COLUMNS} FROM metadata_conflicts WHERE runtime_id IS NOT NULL AND field=? '
        'AND (entry_id=? OR recording_id=?) ORDER BY entry_id DESC LIMIT 1',
        ('mb_year' if field == 'year' else field, entry, recording)).fetchone()


def store_conflict(conn, entry, recording, field, scope, identity, status, revision, at):
    conn.execute('INSERT INTO metadata_conflicts(entry_id,recording_id,field_scope,field,status,updated_at,runtime_id,'
                 'runtime_revision) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(runtime_id) DO UPDATE SET status=excluded.status,'
                 'runtime_revision=excluded.runtime_revision,updated_at=excluded.updated_at',
                 (None if scope == 'recording' else entry, recording if scope == 'recording' else None,
                  scope, field, CONFLICT_STATUS[status], at, identity, revision))


def conflict_details(conn, row, *, now=None):
    if row is None:
        return {'candidates': []}
    now = time.time() if now is None else now
    data = files.read(conn,'conflicts',(row['id'],),row['revision'])
    return {**data,'candidates':[candidate for candidate in data.get('candidates', [])
                                if candidate.get('at',row['updated_at']) > now-ALTERNATIVE_SECONDS]}


def upsert_conflict(conn, entry, field, candidates, *, now=None, refreshed_providers=None):
    from . import metadata_values
    now = time.time() if now is None else now
    field = 'mb_year' if field == 'year' else field
    metadata_values.scope(field)
    if not isinstance(candidates, (list, tuple)) or len(candidates) > 64:
        raise ValueError('Metadata conflict exceeds its candidate budget')
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get('provider') or 'value' not in candidate:
            raise ValueError('A metadata candidate needs its provider and offered value')
        metadata_values.prepare(conn, field, candidate['value'],
            unit=metadata_values.DEFAULT_UNIT.get(field) if candidate['value'] not in (None, '') else None)
    recording, _ = subject(conn,entry)
    old = conflict(conn,entry,field)
    previous = conflict_details(conn,old,now=now)
    offered = {candidate['provider'] for candidate in candidates}
    retained = [candidate for candidate in previous.get('candidates',[]) if refreshed_providers is not None
                and candidate['provider'] not in refreshed_providers and candidate['provider'] not in offered]
    current = []
    for candidate in candidates:
        # Reusing a cached answer does not grant it another seven days.
        supplied = alternatives(conn, recording, candidate['provider'], now=now)
        at = candidate.get('at', supplied.get('at', now))
        if at > now-ALTERNATIVE_SECONDS:
            current.append({'provider':candidate['provider'],'value':candidate['value'],'at':at})
    current += retained
    status = old['status'] if old else 0
    if status==2 or status==1 and {str(c['value']) for c in current}-{str(c['value']) for c in previous.get('candidates',[])}:
        status=0
    revision = old['revision']+1 if old else 1
    identity = old['id'] if old else next_identity(conn, 'metadata_conflicts')
    files.publish(conn,'conflicts',(identity,),revision,{'candidates':current})
    store_conflict(conn, entry, recording, field, metadata_values.scope(field), identity, status, revision, now)


def converge(conn, entry, agreeing, *, now=None):
    recording, _ = subject(conn,entry)
    for row in conn.execute(f'SELECT {CONFLICT_COLUMNS} FROM metadata_conflicts WHERE runtime_id IS NOT NULL '
                            "AND status='unresolved' AND field<>'energy' "
                            'AND (entry_id=? OR recording_id=?)',
                            (entry,recording)).fetchall():
        field = row['field']
        previous={candidate['provider'] for candidate in conflict_details(conn,row,now=now)['candidates']}
        providers=set(agreeing.get('year' if field=='mb_year' else field,{}))
        if len(providers)>=2 and previous<=providers:
            conn.execute("UPDATE metadata_conflicts SET status='provider_converged',updated_at=? WHERE runtime_id=?",
                         (time.time() if now is None else now,row['id']))


def native_page(conn, identity_key, *, after_id=0, limit=100):
    from .activity import WorkBudgetExceeded
    from .evidence_ledger import canonical
    rows = conn.execute('SELECT s.rowid AS cursor,s.source_key,s.revision,s.provider FROM rec_musical_sources s '
        'JOIN rec_musical_links l ON l.source_key=s.source_key JOIN recordings r ON r.id=l.recording_id '
        'WHERE r.identity_key=? AND s.rowid>=? ORDER BY s.rowid LIMIT ?', (identity_key, after_id//64, limit)).fetchall()
    result, used = [], 0
    for row in rows:
        data = files.read(conn, 'native', (row['source_key'],), row['revision'])
        for position, (field, value) in enumerate(sorted(data['observations'].items()), 1):
            cursor = row['cursor']*64+position
            if cursor <= after_id:
                continue
            used += len(canonical(value))
            if used > files.MAX_BYTES:
                raise WorkBudgetExceeded('Native metadata page exceeds byte budget')
            result.append({'id': cursor, 'delivery_id': data.get('run_id'), 'provider': row['provider'],
                'entry_id': data.get('entry_id'), 'recording_id': data.get('recording_id'), 'observed_at': data.get('at'),
                'field': field, 'value': value['value'], 'provenance': value['provenance']})
            if len(result) == limit:
                return result
    return result


