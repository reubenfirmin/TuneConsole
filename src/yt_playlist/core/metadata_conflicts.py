"""Relational metadata conflicts with explicit candidate evidence and decisions.

Callers hold the Store lock and an outer transaction. Candidate admission uses
supplied observation IDs, or captures the candidate actually offered by this
call. It never searches historical values to guess a supplying delivery.
"""
from . import metadata_deliveries as deliveries, metadata_evidence as evidence, metadata_values as values


def field_name(field):
    field = 'mb_year' if field == 'year' else field
    values.scope(field)
    return field


def candidates(conn, conflict_id):
    result = []
    for row in conn.execute('SELECT o.*,COALESCE(o.legacy_source,d.provider) AS provider '
            'FROM metadata_conflict_candidates c JOIN metadata_observations o ON o.id=c.observation_id '
            'LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE c.conflict_id=? ORDER BY c.position,o.id',
            (conflict_id,)):
        value = evidence.value_from_row(row)
        result.append({'provider': row['provider'], 'value': None if value.kind == 'unresolved' else value.selected(),
                       'observation_id': row['id'], 'value_kind': value.kind,
                       'field_scope': row['field_scope'], 'delivery_id': row['delivery_id']})
    return result


def for_entry(conn, entry_id, field=None):
    query = ('SELECT c.* FROM metadata_conflicts c JOIN catalog_entries e ON e.id=? '
             'WHERE (c.entry_id=e.id OR c.recording_id=e.recording_id)')
    args = [entry_id]
    if field is not None:
        query += ' AND c.field=?'
        args.append(field_name(field))
    return conn.execute(query + ' ORDER BY c.id', args).fetchall()


def _admit(conn, entry_id, field, candidate, now):
    provider = candidate.get('provider')
    if not isinstance(provider, str) or not provider or 'value' not in candidate:
        raise ValueError('A metadata candidate needs its provider and offered value')
    unit = values.DEFAULT_UNIT.get(field) if candidate['value'] not in (None, '') else None
    prepared = values.prepare(conn, field, candidate['value'], unit=unit)
    observation = candidate.get('observation_id')
    if observation is None:
        delivery = deliveries.capture_state(conn, entry_id, provider, 'conflict-offer:' + field,
            {'format': 'metadata-conflict-offer-1', 'field': field, 'value': candidate['value']}, now=now)
        return evidence.record_provider(conn, delivery, {field: candidate['value']},
                                        units={field: unit} if unit else {})[field]
    row = conn.execute('SELECT o.*,COALESCE(d.provider,o.legacy_source) AS provider FROM metadata_observations o '
                       'LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.id=?', (observation,)).fetchone()
    subject = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
    if (row is None or subject is None or row['provider'] != provider or row['field'] != field
            or row['field_scope'] != values.scope(field) or row['recording_id'] != subject['recording_id']
            or values.scope(field) != 'recording' and row['entry_id'] != entry_id
            or row['origin'] not in ('provider', 'selection_transfer')
            or (row['value_kind'], row['text_value'], row['number_value'], row['unit'])
               != (prepared.kind, prepared.text, prepared.number, prepared.unit)):
        raise ValueError('Metadata candidate has different evidence, subject, field or provider')
    return observation


def upsert(conn, entry_id, field, offered, *, now=None, refreshed_providers=None):
    field, now = field_name(field), evidence._clock(now)
    if not isinstance(offered, (list, tuple)) or len(offered) > 64:
        raise ValueError('Metadata conflict exceeds its candidate budget')
    if any(not isinstance(candidate, dict) for candidate in offered):
        raise ValueError('Metadata conflict candidates must be named values with providers')
    with evidence._atomic(conn):
        subject = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        if subject is None:
            raise ValueError('Unknown metadata conflict input')
        # Continue an installed entry-local conflict on its original owner.
        # Shared recording evidence is never used to rewrite that old scope.
        rows = for_entry(conn, entry_id, field)
        row = next((r for r in rows if r['entry_id'] == entry_id), rows[0] if rows else None)
        old = candidates(conn, row['id']) if row is not None else []
        offered_providers = {candidate.get('provider') for candidate in offered}
        retained = [candidate for candidate in old if refreshed_providers is not None
                    and candidate['provider'] not in refreshed_providers
                    and candidate['provider'] not in offered_providers]
        observations = [_admit(conn, entry_id, field, candidate, now) for candidate in offered]
        observations.extend(candidate['observation_id'] for candidate in retained)
        new_values = {str(candidate['value']) for candidate in [*offered, *retained]}
        previous_values = {str(candidate['value']) for candidate in old}
        status = 'unresolved' if row is None else row['status']
        if status == 'provider_converged' or status == 'user_resolved' and new_values - previous_values:
            status = 'unresolved'
        if row is None:
            scope = values.scope(field)
            conflict = conn.execute('INSERT INTO metadata_conflicts(entry_id,recording_id,field_scope,field,status,updated_at) '
                'VALUES (?,?,?,?,?,?)', (None if scope == 'recording' else entry_id,
                subject['recording_id'] if scope == 'recording' else None, scope, field, status, now)).lastrowid
        else:
            conflict = row['id']
            conn.execute('UPDATE metadata_conflicts SET status=?,updated_at=? WHERE id=?', (status, now, conflict))
            conn.execute('DELETE FROM metadata_conflict_candidates WHERE conflict_id=?', (conflict,))
        conn.executemany('INSERT OR IGNORE INTO metadata_conflict_candidates(conflict_id,observation_id,position) VALUES (?,?,?)',
                         ((conflict, observation, position) for position, observation in enumerate(observations)))
    return conflict


def resolve(conn, entry_id, field, observation_id, *, now=None):
    now = evidence._clock(now)
    with evidence._atomic(conn):
        for row in for_entry(conn, entry_id, field):
            conn.execute("UPDATE metadata_conflicts SET status='user_resolved',selected_observation_id=?,"
                         'updated_at=? WHERE id=?', (observation_id, now, row['id']))


def converge(conn, entry_id, agreeing, *, now=None):
    """The waterfall supplies answers already compared by its field policy."""
    now = evidence._clock(now)
    with evidence._atomic(conn):
        for row in for_entry(conn, entry_id):
            field = 'year' if row['field'] == 'mb_year' else row['field']
            refreshed = set(agreeing.get(field, {}))
            previous = {candidate['provider'] for candidate in candidates(conn, row['id'])}
            if row['status'] == 'unresolved' and len(refreshed) >= 2 and previous <= refreshed:
                conn.execute("UPDATE metadata_conflicts SET status='provider_converged',updated_at=? WHERE id=?",
                             (now, row['id']))
