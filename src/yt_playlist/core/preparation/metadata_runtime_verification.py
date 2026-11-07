"""Compare prepared metadata meaning with installed evidence before cutover.

Run under a read snapshot, never a library-sized writer reservation. No prepared
checksum or progress counter substitutes for comparison with the source owners.
"""
from itertools import zip_longest

from .. import metadata_runtime as runtime, metadata_runtime_files as files
from .. import metadata_evidence, metadata_native, metadata_attempts, metadata_conflicts
from ..activity import check_cancellable_work


def _equal(actual, expected, label):
    if actual != expected:
        raise ValueError('Prepared metadata differs from source: ' + label)


def _rows(conn, actual, expected, source=None):
    source = conn if source is None else source
    for a, b in zip_longest(conn.execute(actual), source.execute(expected)):
        check_cancellable_work()
        _equal(tuple(a) if a is not None else None, tuple(b) if b is not None else None, actual)


def _document(conn, kind, owner, revision, expected):
    check_cancellable_work()
    _equal(files.read(conn, kind, owner, revision), expected, kind + ' ' + repr(owner))


def verify(conn, state, *, source=None, native=True):
    verify_selected(conn, source=source)
    _verify_cached(conn, state, source=source)
    if native:
        verify_native(conn, conn if source is None else source)


def verify_selected(conn, *, source=None):
    """Selected catalog metadata and manual decisions must survive independently of caches."""
    source = conn if source is None else source
    for scope, owner in (('recording', 'recording_metadata_selections'), ('entry', 'entry_metadata_selections')):
        _rows(conn, f'SELECT s.{scope}_id,{runtime.field_expression("s")},p.name '
              f'FROM metadata_runtime_{scope}_sources s JOIN metadata_runtime_providers p ON p.id=s.provider_id '
              f'ORDER BY s.{scope}_id,2',
              f'SELECT s.{scope}_id,s.field,COALESCE(d.provider,o.legacy_source,s.legacy_source) '
              f'FROM {owner} s LEFT JOIN metadata_observations o ON o.id=s.observation_id '
              f'LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id ORDER BY s.{scope}_id,s.field', source)
    _rows(conn, 'SELECT r.recording_id,g.genre_key,r.unresolved FROM metadata_runtime_recording_genres r '
          'LEFT JOIN metadata_runtime_genres g ON g.id=r.genre_id ORDER BY r.recording_id',
          'SELECT id,genre_key,unresolved_genre FROM recordings ORDER BY id')
    _rows(conn, 'SELECT id,entry_id,field FROM metadata_runtime_choices ORDER BY id',
          'SELECT observation_id,entry_id,CASE field ' + ' '.join(
              f"WHEN '{field}' THEN {runtime.code(field)}" for field in runtime.FIELDS) +
          ' END FROM metadata_choice_heads ORDER BY observation_id', source)
    for head in source.execute('SELECT * FROM metadata_choice_heads ORDER BY observation_id'):
        history = list(source.execute("SELECT * FROM metadata_observations WHERE entry_id=? AND field=? "
                       "AND origin IN ('user','legacy_choice') ORDER BY id", (head['entry_id'], head['field'])))
        selected = next(row for row in history if row['id'] == head['observation_id'])
        value = metadata_evidence.value_from_row(selected)
        kind = {'withdrawn': 0, 'null': 1, 'blank': 2, 'value': 4 if value.number is not None else 5 if value.genre_key else 3,
                'unresolved': 6}[value.kind]
        actual = conn.execute('SELECT c.revision,c.kind,c.text_value,c.number_value,g.genre_key,p.name '
            'FROM metadata_runtime_choices c JOIN metadata_runtime_providers p ON p.id=c.provider_id '
            'LEFT JOIN metadata_runtime_genres g ON g.id=c.genre_id WHERE c.id=?', (head['observation_id'],)).fetchone()
        _equal(tuple(actual), (len(history), kind, None if kind in (0, 1, 4, 5) else value.text,
               value.number, value.genre_key, selected['legacy_source'] or 'user'), 'selected manual choice')
        for revision, row in enumerate(history, 1):
            value = metadata_evidence.value_from_row(row)
            _document(conn, 'choices', (head['observation_id'],), revision, {
                'entry_id': row['entry_id'], 'recording_id': row['recording_id'],
                'input_video_id': row['input_video_id'], 'field': row['field'],
                'value': value.number if value.number is not None else value.text,
                'withdrawn': value.kind == 'withdrawn', 'source': row['legacy_source'] or 'user',
                'at': row['observed_at']})


def _verify_cached(conn, state, *, source=None):
    source = conn if source is None else source
    for row in source.execute('SELECT * FROM enrichment_provider_state ORDER BY recording_id,provider'):
        if row['at'] <= state['at'] - runtime.ALTERNATIVE_SECONDS:
            continue
        provider = runtime.provider_id(conn, row['provider'], create=False)
        if provider is None:
            raise ValueError('Prepared metadata is missing its provider')
        suppliers = list(source.execute('SELECT f.field,d.input_entry_id,d.input_video_id FROM provider_state_fields f '
            'JOIN metadata_deliveries d ON d.id=f.delivery_id WHERE f.recording_id=? AND f.provider=? '
            'ORDER BY d.observed_at DESC,d.id DESC,f.field', (row['recording_id'], row['provider'])))
        _document(conn, 'alternatives', (row['recording_id'], provider), float(row['at']), {
            'recording_id': row['recording_id'], 'provider': row['provider'],
            'inputs': {field: [entry, video] for field, entry, video in suppliers},
            'entry_id': suppliers[0][1] if suppliers else None,
            'input_video_id': suppliers[0][2] if suppliers else None,
            'fields': metadata_attempts.state_fields(source, row['recording_id'], row['provider']),
            'at': row['at'], 'expires_at': row['at'] + runtime.ALTERNATIVE_SECONDS, 'error': row['error']})
    statuses = {'unresolved': 0, 'user_resolved': 1, 'provider_converged': 2}
    conflicts = 0
    for row in source.execute('SELECT * FROM metadata_conflicts ORDER BY id'):
        candidates = metadata_conflicts.candidates(source, row['id'])
        for candidate in candidates:
            candidate['at'] = source.execute('SELECT observed_at FROM metadata_observations WHERE id=?',
                                          (candidate['observation_id'],)).fetchone()[0]
        _document(conn, 'conflicts', (row['id'],), 1, {'candidates': candidates})
        actual = conn.execute('SELECT * FROM metadata_runtime_conflicts WHERE id=?', (row['id'],)).fetchone()
        _equal(tuple(actual) if actual is not None else None,
               (row['id'], row['recording_id'], row['entry_id'], runtime.code(row['field']), statuses[row['status']],
                1, row['updated_at']), 'conflict scope/status')
        conflicts += 1
    _equal(conn.execute('SELECT count(*) FROM metadata_runtime_conflicts').fetchone()[0], conflicts, 'conflict count')


def verify_native(conn, source):
    """Check historical model inputs without comparing mutable runtime choices."""
    heads = 0
    for row in source.execute('SELECT * FROM rec_musical_sources ORDER BY source_key'):
        key = row['source_key']
        expected_values = metadata_native.installed_source_values(source, key, max_bytes=files.MAX_BYTES)
        _document(conn, 'native', (key,), row['revision'], {
            'observations': expected_values, 'provider': row['provider']})
        expected = metadata_native.installed_runtime_head(source, key, {})
        actual = conn.execute('SELECT run_id,accepted_at,result_hash FROM metadata_native_delivery_heads '
                              'WHERE source_key=?', (key,)).fetchone()
        _equal(tuple(actual) if actual is not None else None,
               tuple(expected[name] for name in ('run_id', 'accepted_at', 'result_hash')) if expected else None,
               'native freshness')
        heads += expected is not None
    _equal(conn.execute('SELECT count(*) FROM metadata_native_delivery_heads').fetchone()[0], heads, 'native head count')
