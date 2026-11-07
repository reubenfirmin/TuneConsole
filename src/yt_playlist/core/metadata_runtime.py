"""Current metadata facts in SQL; alternatives and explanations in keyed JSON.

Installed histories keep their old readers/writers until explicit preparation.
The SQL owners below are bounded by current subjects/fields, not lookup count.
"""
import json
import os
import sqlite3
import time

from . import fao, metadata_runtime_files as files
from .catalog_identity import RECORDING_FIELDS, ITEM_FIELDS
from .metadata_contract import ALTERNATIVE_SECONDS, PROVIDER_FIELDS, Capture, clock, runtime_active as active

# Append new names; these persisted numeric field codes must never be reordered.
FIELDS = ('genre', 'bpm', 'energy', 'danceability', 'mb_recording_id', 'music_key', 'music_scale',
          'mood_happy', 'mood_sad', 'mood_relaxed', 'mood_acoustic', 'instrumental', 'title', 'artist',
          'orig_title', 'orig_artist', 'album', 'duration_s', 'available', 'video_type', 'artist_browse_id',
          'album_browse_id', 'thumbnail', 'mb_year', 'loudness', 'dynamic_complexity', 'popularity', 'gain', 'label')
CODES = {name: index + 1 for index, name in enumerate(FIELDS)}


def reserve(conn):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    else:
        conn.execute('UPDATE metadata_runtime_state SET active=active WHERE id=1')


def next_identity(conn, table):
    return conn.execute('SELECT max(COALESCE((SELECT max(id) FROM '+table+'),0),'
        'COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0))+1', (table,)).fetchone()[0]


def code(field):
    return CODES['mb_year' if field == 'year' else field]


def definitions():
    yield 'metadata_runtime_state', '''CREATE TABLE metadata_runtime_state (
        id INTEGER PRIMARY KEY CHECK(id=1),active INTEGER NOT NULL CHECK(active IN (0,1)),
        gc_cursor INTEGER NOT NULL DEFAULT 0,gc_after REAL NOT NULL DEFAULT 0,
        gc_shard INTEGER NOT NULL DEFAULT 0 CHECK(gc_shard BETWEEN 0 AND 99))'''
    yield 'metadata_runtime_providers', 'CREATE TABLE metadata_runtime_providers (id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE)'
    yield 'metadata_runtime_genres', 'CREATE TABLE metadata_runtime_genres (id INTEGER PRIMARY KEY,genre_key TEXT NOT NULL UNIQUE)'
    yield 'metadata_runtime_recording_genres', '''CREATE TABLE metadata_runtime_recording_genres (
        recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,genre_id INTEGER REFERENCES metadata_runtime_genres(id),
        unresolved TEXT,CHECK(genre_id IS NULL OR unresolved IS NULL))'''
    for scope, owner in (('recording', 'recordings'), ('entry', 'catalog_entries')):
        name = 'metadata_runtime_' + scope + '_sources'
        yield name, f'''CREATE TABLE {name} ({scope}_id INTEGER NOT NULL REFERENCES {owner}(id) ON DELETE CASCADE,
            field INTEGER NOT NULL CHECK(field BETWEEN 1 AND {len(FIELDS)}),
            provider_id INTEGER NOT NULL REFERENCES metadata_runtime_providers(id),
            PRIMARY KEY({scope}_id,field)) WITHOUT ROWID'''
    yield 'metadata_runtime_choices', f'''CREATE TABLE metadata_runtime_choices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,entry_id INTEGER NOT NULL REFERENCES catalog_entries(id) ON DELETE CASCADE,
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
        entry_id INTEGER REFERENCES catalog_entries(id) ON DELETE CASCADE,field INTEGER NOT NULL CHECK(field BETWEEN 1 AND {len(FIELDS)}),
        status INTEGER NOT NULL CHECK(status IN (0,1,2)),revision INTEGER NOT NULL CHECK(revision>0),updated_at REAL NOT NULL,
        CHECK((recording_id IS NULL)<>(entry_id IS NULL)))'''
    for scope in ('recording', 'entry'):
        name = 'ix_metadata_runtime_conflict_' + scope
        yield name, f'CREATE UNIQUE INDEX {name} ON metadata_runtime_conflicts({scope}_id,field) WHERE {scope}_id IS NOT NULL'


def install(conn, context=None):
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='metadata_runtime_state'").fetchone():
        validate(conn)
        return
    for _, sql in definitions():
        conn.execute(sql)
    conn.execute('INSERT INTO metadata_runtime_state(id,active) VALUES (1,0)')


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible runtime metadata object: ' + name)
    if not conn.execute('SELECT 1 FROM metadata_runtime_state WHERE id=1').fetchone():
        raise MigrationError('Missing runtime metadata state')
    if active(conn):
        for name, sql in views().items():
            row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
            if row is None or _tokens(row[0]) != _tokens(sql):
                raise MigrationError('Incompatible runtime metadata view: ' + name)


def provider_id(conn, name, *, create=True):
    row = conn.execute('SELECT id FROM metadata_runtime_providers WHERE name=?', (name,)).fetchone()
    if row:
        return row[0]
    if not create:
        return None
    if not isinstance(name, str) or not 0 < len(name.encode()) <= 256:
        raise ValueError('Invalid metadata provider')
    return conn.execute('INSERT INTO metadata_runtime_providers(name) VALUES (?)', (name,)).lastrowid


def genre_id(conn, key):
    if key is None:
        return None
    conn.execute('INSERT INTO metadata_runtime_genres(genre_key) VALUES (?) ON CONFLICT DO NOTHING', (key,))
    return conn.execute('SELECT id FROM metadata_runtime_genres WHERE genre_key=?', (key,)).fetchone()[0]


def field_expression(alias):
    return 'CASE '+alias+'.field '+ ' '.join(f"WHEN {index+1} THEN '{name}'" for index, name in enumerate(FIELDS))+' END'


def value_expression(alias):
    return (f'CASE {alias}.kind WHEN 4 THEN {alias}.number_value WHEN 5 THEN '
        f'(SELECT g.name FROM metadata_runtime_genres m JOIN genre_catalog g ON g.key=COALESCE('
        f'(SELECT genre_key FROM genre_redirects WHERE retired_key=m.genre_key),m.genre_key) WHERE m.id={alias}.genre_id) '
        f'ELSE {alias}.text_value END')


def views():
    result = {'entry_field_choices': 'CREATE VIEW entry_field_choices AS SELECT c.entry_id,'+
        field_expression('c')+' AS field,json_quote('+value_expression('c')+') AS value_json,p.name AS source '
        'FROM metadata_runtime_choices c JOIN metadata_runtime_providers p ON p.id=c.provider_id WHERE c.kind<>0'}
    for scope, view in (('recording', 'recording_field_sources'), ('entry', 'item_field_sources')):
        result[view] = (f'CREATE VIEW {view} AS SELECT s.{scope}_id,'+field_expression('s')+
            ' AS field,p.name AS source,NULL AS selected_at FROM metadata_runtime_'+scope+'_sources s '
            'JOIN metadata_runtime_providers p ON p.id=s.provider_id')
    result['enrichment_conflict'] = ('CREATE VIEW enrichment_conflict AS SELECT c.id,e.id AS track_id,'
        "CASE WHEN c.field="+str(code('mb_year'))+" THEN 'year' ELSE "+field_expression('c')+
        " END AS field,'[]' AS candidates,c.status AS resolved,NULL AS resolved_value,c.updated_at,e.recording_id,"
        "e.video_id AS input_video_id,CASE WHEN c.recording_id IS NOT NULL THEN 'recording' ELSE 'item' END AS field_scope "
        'FROM metadata_runtime_conflicts c JOIN catalog_entries e ON e.id=c.entry_id OR e.recording_id=c.recording_id')
    return result


def activate(conn):
    for name, sql in views().items():
        conn.execute('DROP VIEW '+name)
        conn.execute(sql)
    conn.execute('UPDATE metadata_runtime_state SET active=1 WHERE id=1')


def subject(conn, entry):
    row = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()
    if row is None:
        raise ValueError('Unknown metadata catalog input')
    return tuple(row)


def selected_source(conn, entry, field):
    recording, _ = subject(conn, entry)
    scope, identity = ('recording', recording) if field in RECORDING_FIELDS else ('entry', entry)
    row = conn.execute('SELECT p.name FROM metadata_runtime_'+scope+'_sources s '
        'JOIN metadata_runtime_providers p ON p.id=s.provider_id WHERE s.'+scope+'_id=? AND s.field=?',
        (identity, code(field))).fetchone()
    return row[0] if row else None


def select(conn, entry, field, value, source):
    from . import genre_classifications, metadata_values
    if field not in CODES:
        raise ValueError('Unknown metadata field')
    metadata_values.prepare(conn, field, value,
        unit=metadata_values.DEFAULT_UNIT.get(field) if value not in (None, '') else None)
    recording, video = subject(conn, entry)
    if field in RECORDING_FIELDS:
        scope, identity = 'recording', recording
        if field == 'genre':
            genre_classifications.write(conn, 'recordings', recording, value)
            from .genre_identity import resolve
            key, _ = resolve(conn, value)
            conn.execute('INSERT INTO metadata_runtime_recording_genres VALUES (?,?,?) '
                'ON CONFLICT(recording_id) DO UPDATE SET genre_id=excluded.genre_id,unresolved=excluded.unresolved',
                (recording, genre_id(conn, key), value if key is None else None))
        else:
            conn.execute(f'UPDATE recordings SET {field}=? WHERE id=? AND {field} IS NOT ?', (value, recording, value))
    else:
        scope, identity = 'entry', entry
        table, key, target = ('youtube_items','video_id',video) if video is not None else ('unresolved_items','entry_id',entry)
        conn.execute(f'UPDATE {table} SET {field}=? WHERE {key}=? AND {field} IS NOT ?', (value, target, value))
    conn.execute('INSERT INTO metadata_runtime_'+scope+'_sources VALUES (?,?,?) ON CONFLICT('+scope+
        '_id,field) DO UPDATE SET provider_id=excluded.provider_id', (identity, code(field), provider_id(conn, source)))


def choose(conn, entry, field, value=None, *, withdraw=False, source='user', now=None, expected_recording_id=None,
           _prepared=None, _invalidate=True, _identity=None, _revision=None):
    from . import metadata_values
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
        text, number, genre = prepared.text, prepared.number, genre_id(conn, prepared.genre_key)
        if kind == 5:
            text = None
    previous = conn.execute('SELECT id,revision FROM metadata_runtime_choices WHERE entry_id=? AND field=?',
                            (entry, code(field))).fetchone()
    if (_identity is not None or _revision is not None) and active(conn):
        raise ValueError('Explicit metadata revisions are only valid during preparation')
    revision = _revision if _revision is not None else previous['revision'] + 1 if previous else 1
    identity = (_identity if _identity is not None else previous['id'] if previous else
                next_identity(conn, 'metadata_runtime_choices'))
    files.publish(conn, 'choices', (identity,), revision, {'entry_id':entry, 'recording_id':recording,
        'input_video_id':video, 'field':field, 'value':value, 'withdrawn':withdraw, 'source':source, 'at':at})
    conn.execute('INSERT INTO metadata_runtime_choices VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(entry_id,field) DO UPDATE SET '
        'provider_id=excluded.provider_id,revision=excluded.revision,kind=excluded.kind,text_value=excluded.text_value,'
        'number_value=excluded.number_value,genre_id=excluded.genre_id',
        (identity, entry, code(field), provider_id(conn, source), revision, kind, text, number, genre))
    if _invalidate:
        conn.execute("INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1")
        conn.execute('INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings WHERE id=? '
                     'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1', (recording,))
    return identity


def alternatives(conn, recording, provider, *, now=None):
    now = time.time() if now is None else now
    state = conn.execute('SELECT at FROM enrichment_provider_state WHERE recording_id=? AND provider=?',
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
    previous = conn.execute('SELECT * FROM enrichment_provider_state WHERE recording_id=? AND provider=?',
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
    return conn.execute('SELECT * FROM metadata_runtime_conflicts WHERE field=? AND '
                        '(entry_id=? OR recording_id=?) ORDER BY entry_id DESC LIMIT 1', (code(field),entry,recording)).fetchone()


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
    identity = old['id'] if old else next_identity(conn, 'metadata_runtime_conflicts')
    files.publish(conn,'conflicts',(identity,),revision,{'candidates':current})
    conn.execute('INSERT INTO metadata_runtime_conflicts VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
        'status=excluded.status,revision=excluded.revision,updated_at=excluded.updated_at',
        (identity,recording if field in RECORDING_FIELDS else None,None if field in RECORDING_FIELDS else entry,
         code(field),status,revision,now))


def converge(conn, entry, agreeing, *, now=None):
    recording, _ = subject(conn,entry)
    for row in conn.execute('SELECT * FROM metadata_runtime_conflicts WHERE status=0 AND (entry_id=? OR recording_id=?)',
                            (entry,recording)).fetchall():
        field=FIELDS[row['field']-1]
        previous={candidate['provider'] for candidate in conflict_details(conn,row,now=now)['candidates']}
        providers=set(agreeing.get('year' if field=='mb_year' else field,{}))
        if len(providers)>=2 and previous<=providers:
            conn.execute('UPDATE metadata_runtime_conflicts SET status=2,updated_at=? WHERE id=?',
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


def expire(conn, *, now=None, limit=16):
    from . import policy_documents
    root = files.directory(conn)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.readers.lock').open('a+b') as stream:
        unlock = policy_documents._try_file_lock(stream, exclusive=True)
        if unlock is None:
            return 0
        try:
            return _expire(conn, now=now, limit=limit)
        finally:
            unlock()


def _expire(conn, *, now=None, limit=16):
    """Bounded round-robin file collection; completion/retry SQL is unchanged."""
    now=time.time() if now is None else now
    state=conn.execute('SELECT gc_cursor,gc_after,gc_shard FROM metadata_runtime_state WHERE id=1').fetchone()
    removed = _expire_alternatives(conn, state['gc_shard'], now)
    if state['gc_after']>now:
        return removed
    rows=conn.execute('SELECT id FROM recordings WHERE id>? ORDER BY id LIMIT ?',
                      (state['gc_cursor'],limit)).fetchall()
    for (recording,) in rows:
        # Candidate bodies follow the same expiry; the compact conflict/status
        # remains useful even when all alternatives have expired.
        conflicts = conn.execute('SELECT * FROM metadata_runtime_conflicts WHERE recording_id=? OR entry_id IN '
            '(SELECT id FROM catalog_entries WHERE recording_id=?)', (recording, recording)).fetchall()
        for row in conflicts:
            data = files.read(conn, 'conflicts', (row['id'],), row['revision'])
            current = conflict_details(conn, row, now=now)
            if current != data:
                files.publish(conn, 'conflicts', (row['id'],), row['revision']+1, current)
                conn.execute('UPDATE metadata_runtime_conflicts SET revision=revision+1 WHERE id=?', (row['id'],))
            parent = (files.directory(conn)/files.relative('conflicts', (row['id'],), row['revision'])).parent
            for index, path in enumerate(parent.iterdir()):
                if index == 128:
                    break
                keep = (row['revision'], row['revision']+1) if current != data else (row['revision'],)
                if path.suffix == '.json' and path.stem.isdecimal() and int(path.stem) not in keep:
                    removed += fao.delete(path, missing_ok=True)
        for key, revision in conn.execute('SELECT s.source_key,s.revision FROM rec_musical_sources s '
            'JOIN rec_musical_links l ON l.source_key=s.source_key WHERE l.recording_id=?', (recording,)).fetchall():
            parent = (files.directory(conn)/files.relative('native', (key,), revision)).parent
            for index, path in enumerate(parent.iterdir()):
                if index == 128:
                    break
                if path.suffix == '.json' and path.stem.isdecimal() and int(path.stem) != revision:
                    removed += fao.delete(path, missing_ok=True)
    conn.execute('UPDATE metadata_runtime_state SET gc_cursor=?,gc_after=? WHERE id=1',
                 (rows[-1][0] if rows else 0,0 if rows else now+3600))
    return removed


def _walk_alternatives(root, depth=0):
    """Yield directory entries too, so empty/orphan trees consume the budget."""
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                yield entry, depth
                if depth < 2 and entry.is_dir(follow_symlinks=False):
                    yield from _walk_alternatives(entry.path, depth+1)
    except FileNotFoundError:
        return


def _expire_alternatives(conn, shard, now, *, budget=128):
    # Filesystem traversal includes recordings/providers already deleted from
    # SQL. Persist the completed shard; keep only a bounded-depth iterator in
    # memory while a shard is being visited. A restart safely repeats that shard.
    current = getattr(conn, '_metadata_alternatives_gc', None)
    if current is None or current[0] != shard:
        if current is not None:
            current[1].close()
            conn._close_callbacks.remove(current[1].close)
        root = files.directory(conn)/'alternatives'/str(shard).zfill(2)
        current = shard, _walk_alternatives(root)
        conn._metadata_alternatives_gc = current
        conn._close_callbacks.append(current[1].close)
    removed = 0
    for position in range(budget):
        try:
            entry, depth = next(current[1])
        except StopIteration:
            conn.execute('UPDATE metadata_runtime_state SET gc_shard=? WHERE id=1', ((shard+1)%100,))
            conn._metadata_alternatives_gc = None
            conn._close_callbacks.remove(current[1].close)
            # Empty shards cost one step, allowing small libraries to collect
            # files on this tick rather than needing a hundred worker ticks.
            remaining = budget-position-1
            if remaining:
                removed += _expire_alternatives(conn, (shard+1)%100, now, budget=remaining)
            break
        if depth != 2 or not entry.name.endswith('.json') or not entry.is_file(follow_symlinks=False):
            continue
        try:
            at = float.fromhex(entry.name[:-5])
        except ValueError:
            continue
        if at <= now-ALTERNATIVE_SECONDS:
            removed += fao.delete(entry.path, missing_ok=True)
    return removed
