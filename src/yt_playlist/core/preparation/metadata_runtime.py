"""Explicit, resumable preparation of compact metadata owners and keyed JSON.

No work in this module runs during ordinary startup. The source keeps serving
and writing its installed format; a changed source refuses final activation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from .. import fao, metadata_runtime as runtime, metadata_runtime_files as files
from .. import metadata_evidence, metadata_attempts, metadata_native, metadata_conflicts
from .. import provider_frontiers
from ..activity import check_cancellable_work
from ..evidence_ledger import canonical

STAGES = ('recordings', 'entries', 'choices', 'providers', 'native', 'conflicts')


def signature(conn):
    """Hash serving inputs and current references, without reading lookup history."""
    digest = hashlib.sha256()
    queries = [
        'SELECT * FROM recordings ORDER BY id',
        'SELECT * FROM catalog_entries ORDER BY id',
        'SELECT * FROM youtube_items ORDER BY video_id',
        'SELECT * FROM unresolved_items ORDER BY entry_id',
        'SELECT * FROM recording_field_sources ORDER BY recording_id,field',
        'SELECT * FROM item_field_sources ORDER BY entry_id,field',
        'SELECT * FROM genre_catalog ORDER BY key',
        'SELECT * FROM genre_alias ORDER BY 1',
        'SELECT * FROM genre_redirects ORDER BY retired_key',
        'SELECT * FROM metadata_choice_heads ORDER BY entry_id,field',
        "SELECT * FROM metadata_observations WHERE origin IN ('user','legacy_choice') ORDER BY id",
        'SELECT * FROM enrichment_provider_state ORDER BY recording_id,provider',
        'SELECT * FROM provider_state_fields ORDER BY recording_id,provider,field',
        'SELECT * FROM rec_musical_sources ORDER BY source_key',
        'SELECT * FROM musical_source_observations ORDER BY source_key,field',
        'SELECT * FROM rec_musical_links ORDER BY source_key,recording_id',
        'SELECT * FROM metadata_conflicts ORDER BY id',
        'SELECT * FROM metadata_conflict_candidates ORDER BY conflict_id,position,observation_id',
    ]
    for query in queries:
        digest.update(query.encode())
        for row in conn.execute(query):
            check_cancellable_work()
            digest.update(repr(tuple(row)).encode())
            digest.update(b'\n')
    return digest.hexdigest()


def _save(path, document):
    fao.write(path, canonical(document))


def _initial(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return {'format': 1, 'database': str(Path(path).resolve()), 'source': signature(conn),
            'at': time.time(), 'stage': 0, 'cursor': None, 'counts': {}}


def _sources(conn, scope, cursor, limit, *, source=None):
    source = conn if source is None else source
    owner, key = ('recordings', 'id') if scope == 'recording' else ('catalog_entries', 'id')
    rows = source.execute(f'SELECT {key} FROM {owner} WHERE {key}>? ORDER BY {key} LIMIT ?',
                        (cursor or 0, limit)).fetchall()
    table = 'recording_field_sources' if scope == 'recording' else 'item_field_sources'
    for (identity,) in rows:
        target = 'metadata_runtime_'+scope+'_sources'
        conn.execute('DELETE FROM '+target+' WHERE '+scope+'_id=?', (identity,))
        for row in source.execute('SELECT field,source FROM '+table+' WHERE '+scope+'_id=?', (identity,)).fetchall():
            conn.execute('INSERT INTO '+target+' VALUES (?,?,?)',
                         (identity, runtime.code(row['field']), runtime.provider_id(conn, row['source'])))
        if scope == 'recording':
            genre = conn.execute('SELECT genre_key,unresolved_genre FROM recordings WHERE id=?', (identity,)).fetchone()
            conn.execute('INSERT INTO metadata_runtime_recording_genres VALUES (?,?,?) '
                'ON CONFLICT(recording_id) DO UPDATE SET genre_id=excluded.genre_id,unresolved=excluded.unresolved',
                (identity, runtime.genre_id(conn, genre[0]), genre[1]))
    return rows[-1][0] if rows else cursor, len(rows)


def _choices(conn, cursor, limit, *, source=None):
    source = conn if source is None else source
    cursor = cursor or [0, '']
    rows = source.execute('SELECT * FROM metadata_choice_heads WHERE (entry_id,field)>(?,?) '
                        'ORDER BY entry_id,field LIMIT ?', (*cursor, limit)).fetchall()
    for head in rows:
        history = source.execute("SELECT * FROM metadata_observations WHERE entry_id=? AND field=? "
            "AND origin IN ('user','legacy_choice') ORDER BY id", (head['entry_id'], head['field'])).fetchall()
        if not history:
            raise ValueError('Manual metadata head has no retained decisions')
        for revision, row in enumerate(history, 1):
            files.publish(conn, 'choices', (head['observation_id'],), revision, _choice_data(row))
        # Preserve the actual selected head even if a retained historical row
        # was not its latest ID. History order is not selection provenance.
        selected = next(row for row in history if row['id'] == head['observation_id'])
        value = metadata_evidence.value_from_row(selected)
        kind = {'withdrawn':0, 'null':1, 'blank':2, 'value':4 if value.number is not None else 5 if value.genre_key else 3,
                'unresolved':6}[value.kind]
        conn.execute('INSERT INTO metadata_runtime_choices VALUES (?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(entry_id,field) DO UPDATE SET id=excluded.id,provider_id=excluded.provider_id,'
            'revision=excluded.revision,kind=excluded.kind,text_value=excluded.text_value,'
            'number_value=excluded.number_value,genre_id=excluded.genre_id',
            (head['observation_id'], head['entry_id'], runtime.code(head['field']),
             runtime.provider_id(conn, selected['legacy_source'] or 'user'), len(history), kind,
             None if kind in (0,1,4,5) else value.text, value.number, runtime.genre_id(conn, value.genre_key)))
    return ([rows[-1]['entry_id'], rows[-1]['field']] if rows else cursor), len(rows)


def _choice_data(row):
    value = metadata_evidence.value_from_row(row)
    return {'entry_id': row['entry_id'], 'recording_id': row['recording_id'],
            'input_video_id': row['input_video_id'], 'field': row['field'],
            'value': value.number if value.number is not None else value.text,
            'withdrawn': value.kind == 'withdrawn', 'source': row['legacy_source'] or 'user',
            'at': row['observed_at']}


def _providers(conn, cursor, limit, at):
    cursor = cursor or [0, '']
    rows = conn.execute('SELECT * FROM enrichment_provider_state WHERE (recording_id,provider)>(?,?) '
                        'ORDER BY recording_id,provider LIMIT ?', (*cursor, limit)).fetchall()
    files.publish_many(conn, _provider_documents(conn, rows, at))
    return ([rows[-1]['recording_id'], rows[-1]['provider']] if rows else cursor), len(rows)


def _provider_documents(conn, rows, at):
    for row in rows:
        provider = runtime.provider_id(conn, row['provider'])
        if row['at'] <= at-runtime.ALTERNATIVE_SECONDS:
            continue
        fields = metadata_attempts.state_fields(conn, row['recording_id'], row['provider'])
        suppliers = conn.execute(
            'SELECT f.field,d.input_entry_id,d.input_video_id FROM provider_state_fields f '
            'JOIN metadata_deliveries d ON d.id=f.delivery_id WHERE f.recording_id=? AND f.provider=? '
            'ORDER BY d.observed_at DESC,d.id DESC,f.field', (row['recording_id'], row['provider'])).fetchall()
        inputs = {field:[entry, video] for field, entry, video in suppliers}
        yield ('alternatives', (row['recording_id'], provider), float(row['at']),
            {'recording_id':row['recording_id'], 'inputs':inputs,
             'entry_id':suppliers[0][1] if suppliers else None, 'input_video_id':suppliers[0][2] if suppliers else None,
             'provider':row['provider'], 'fields':fields,
             'at':row['at'], 'expires_at':row['at']+runtime.ALTERNATIVE_SECONDS, 'error':row['error']})


def _native(conn, cursor, limit):
    rows = conn.execute('SELECT * FROM rec_musical_sources WHERE source_key>? ORDER BY source_key LIMIT ?',
                        (cursor or '', limit)).fetchall()
    files.publish_many(conn, _native_documents(conn, rows))
    return rows[-1]['source_key'] if rows else cursor, len(rows)


def _native_documents(conn, rows):
    for row in rows:
        values = metadata_native.source_values(conn, row['source_key'], max_bytes=files.MAX_BYTES)
        head = metadata_native.installed_runtime_head(conn, row['source_key'], {})
        if head is not None:
            metadata_native.retain_runtime_head(conn, row['source_key'], head)
        else:
            conn.execute('DELETE FROM metadata_native_delivery_heads WHERE source_key=?', (row['source_key'],))
        yield 'native', (row['source_key'],), row['revision'], {'observations': values, 'provider': row['provider']}


def _conflicts(conn, cursor, limit):
    rows = conn.execute('SELECT * FROM metadata_conflicts WHERE id>? ORDER BY id LIMIT ?',
                        (cursor or 0, limit)).fetchall()
    files.publish_many(conn, _conflict_documents(conn, rows))
    return rows[-1]['id'] if rows else cursor, len(rows)


def _conflict_documents(conn, rows):
    statuses = {'unresolved':0, 'user_resolved':1, 'provider_converged':2}
    for row in rows:
        candidates = metadata_conflicts.candidates(conn, row['id'])
        for candidate in candidates:
            candidate['at'] = conn.execute('SELECT observed_at FROM metadata_observations WHERE id=?',
                                           (candidate['observation_id'],)).fetchone()[0]
        conn.execute('INSERT INTO metadata_runtime_conflicts VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
            'recording_id=excluded.recording_id,entry_id=excluded.entry_id,field=excluded.field,status=excluded.status,'
            'revision=excluded.revision,updated_at=excluded.updated_at',
            (row['id'], row['recording_id'], row['entry_id'], runtime.code(row['field']), statuses[row['status']],1,row['updated_at']))
        yield 'conflicts', (row['id'],), 1, {'candidates': candidates}


def prepare_batch(conn, stage, cursor, limit, *, at, source=None):
    """Convert one prefix inside the caller's transaction and checkpoint.

    The standalone preparer and the database rebuild share these domain rules;
    neither needs to adopt the other's progress file or publication lifecycle.
    """
    if not conn.in_transaction:
        raise ValueError('Metadata conversion requires an outer transaction')
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Metadata preparation batch size must be 1 to 128')
    if stage == 'recordings':
        return _sources(conn, 'recording', cursor, limit, source=source)
    if stage == 'entries':
        return _sources(conn, 'entry', cursor, limit, source=source)
    if stage == 'choices':
        return _choices(conn, cursor, limit, source=source)
    if stage == 'providers':
        return _providers(conn, cursor, limit, at)
    if stage == 'native':
        return _native(conn, cursor, limit)
    if stage == 'conflicts':
        return _conflicts(conn, cursor, limit)
    raise ValueError('Unknown metadata conversion stage: ' + str(stage))


def step(conn, progress, *, batch_size=32):
    if conn.in_transaction:
        raise ValueError('Metadata preparation needs an idle connection')
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError('Metadata preparation batch size must be 1 to 128')
    if runtime.active(conn):
        return {'complete': True, 'active': True}
    progress = Path(progress)
    if progress.exists():
        state = json.loads(fao.read(progress).bytes)
        if (state.get('format') != 1 or state.get('database') != str(Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve())):
            raise ValueError('Metadata preparation progress belongs to another database')
    else:
        with conn:
            conn.execute('BEGIN')
            state = _initial(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            for name, sql in reversed(list(runtime.definitions())):
                if sql.startswith('CREATE TABLE') and name != 'metadata_runtime_state':
                    conn.execute('DELETE FROM '+name)
        _save(progress, state)
    if state['stage'] == len(STAGES):
        return {**state, 'complete': True, 'active': False}
    stage = STAGES[state['stage']]
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        cursor, count = prepare_batch(conn, stage, state['cursor'], batch_size, at=state['at'])
    # Repeating a committed batch after a crash is idempotent, including the
    # choice IDs/revisions. Progress can safely lag SQL publication.
    state['cursor'] = cursor
    state['counts'][stage] = state['counts'].get(stage, 0)+count
    if count < batch_size:
        state['stage'] += 1
        state['cursor'] = None
    _save(progress, state)
    return {**state, 'complete': state['stage'] == len(STAGES), 'active': False}


def activate(conn, progress):
    if conn.in_transaction:
        raise ValueError('Metadata cutover needs an idle connection')
    state = json.loads(fao.read(progress).bytes)
    database = str(Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve())
    if state.get('format') != 1 or state.get('database') != database:
        raise ValueError('Metadata preparation progress belongs to another database')
    if state.get('stage') != len(STAGES):
        raise ValueError('Metadata preparation is incomplete')
    if runtime.active(conn):
        return {**state, 'active': True}
    # All library-sized reads and document checks run on a read snapshot. A
    # short writer reservation fences that exact snapshot before switching views.
    changes = conn.total_changes
    version = conn.execute('PRAGMA data_version').fetchone()[0]
    with conn:
        conn.execute('BEGIN')
        if signature(conn) != state['source']:
            raise ValueError('Metadata source changed during preparation; restart preparation before cutover')
        pending = (conn.execute('SELECT 1 FROM provider_refresh_frontiers LIMIT 1').fetchone()
                   if provider_frontiers.available(conn) else conn.execute(
                       "SELECT 1 FROM settings WHERE key LIKE 'enrich_provider_refresh:%' LIMIT 1").fetchone())
        if pending:
            raise ValueError('Finish the current provider configuration refresh before metadata cutover')
        from .metadata_runtime_verification import verify
        verify(conn, state)
        runtime.validate(conn)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.total_changes != changes or conn.execute('PRAGMA data_version').fetchone()[0] != version:
            raise ValueError('Metadata source changed during verification; retry cutover')
        runtime.activate(conn)
        conn.execute("UPDATE enrichment_provider_state SET error='' WHERE error<>''")
        runtime.validate(conn)
    return {**state, 'active': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--progress', type=Path, required=True)
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--batches', type=int, default=1)
    parser.add_argument('--restart', action='store_true', help='Discard inactive progress and re-export the current source')
    args = parser.parse_args()
    if args.batches < 1:
        parser.error('--batches must be positive')
    from ..store import Store
    store = Store(args.database)
    try:
        store.init_schema()
        if args.restart:
            if runtime.active(store.conn):
                raise ValueError('An active metadata format cannot restart preparation')
            if args.progress.exists():
                state = json.loads(fao.read(args.progress).bytes)
                if state.get('database') != str(args.database.resolve()):
                    raise ValueError('Metadata preparation progress belongs to another database')
                fao.delete(args.progress,missing_ok=True)
        for _ in range(args.batches):
            result = step(store.conn, args.progress)
            if result['complete']:
                break
        if args.activate:
            result = activate(store.conn, args.progress)
        print(json.dumps(result, sort_keys=True))
    finally:
        store.conn.close()


if __name__ == '__main__':
    main()
