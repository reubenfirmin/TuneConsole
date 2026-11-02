"""Typed metadata selection, separate from item-scoped explicit overrides.

Callers own the connection transaction. Selections identify the retained evidence
that supplies the ordinary typed catalog columns.
"""
import json
import time

from .catalog_identity import RECORDING_FIELDS, ITEM_FIELDS
from . import genre_classifications, metadata_attempts, metadata_deliveries, metadata_evidence, metadata_values
from . import metadata_runtime
from yt_playlist.util import genre_catalog


def source_location(conn, entry_id, field):
    row = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown catalog entry')
    if metadata_evidence.available(conn):
        return (('recording_metadata_selections', 'recording_id', row[0]) if field in RECORDING_FIELDS else
                ('entry_metadata_selections', 'entry_id', entry_id))
    return (('recording_field_sources','recording_id',row[0]) if field in RECORDING_FIELDS else
            ('item_field_sources','entry_id',entry_id))


def selected_source(conn, entry_id, field):
    if metadata_runtime.active(conn):
        return metadata_runtime.selected_source(conn, entry_id, field)
    table,column,subject = source_location(conn,entry_id,field)
    if table in ('recording_metadata_selections', 'entry_metadata_selections'):
        row = conn.execute('SELECT COALESCE(d.provider,o.legacy_source,s.legacy_source) '
                           f'FROM {table} s LEFT JOIN metadata_observations o ON o.id=s.observation_id '
                           'LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id '
                           f'WHERE s.{column}=? AND s.field=?', (subject, field)).fetchone()
        return row[0] if row else None
    from . import metadata_reads
    table = metadata_reads.read_table(conn,table)
    row = conn.execute(f'SELECT source FROM {table} WHERE {column}=? AND field=?',
                       (subject,field)).fetchone()
    return row[0] if row else None


def select(conn, entry_id, field, value, source, *, now=None, observation_id=None):
    if field not in (*ITEM_FIELDS,*RECORDING_FIELDS):
        raise ValueError(f'Unknown metadata field: {field}')
    if metadata_runtime.active(conn):
        return metadata_runtime.select(conn, entry_id, field, value, source)
    if metadata_evidence.available(conn):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        at = time.time() if now is None else now
        if observation_id is None:
            delivery = metadata_deliveries.capture_state(conn, entry_id, source, 'selected-field:' + field,
                {'format': 'catalog-field-capture-1', 'field': field, 'value': value}, now=at)
            units = {field: metadata_values.DEFAULT_UNIT[field]} if field in metadata_values.NUMBER_FIELDS and value not in (None, '') else {}
            observation_id = metadata_evidence.record_provider(conn, delivery, {field: value}, units=units)[field]
        row = conn.execute('SELECT o.*,COALESCE(d.provider,o.legacy_source) AS provider FROM metadata_observations o '
                           'LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.id=?', (observation_id,)).fetchone()
        if row is None or row['provider'] != source or metadata_evidence.value_from_row(row).selected() != value:
            raise ValueError('Selected metadata value does not match its supplying observation')
        metadata_evidence.select(conn, entry_id, field, observation_id, now=at)
        return
    # Write the base even beneath an override. The effective projection still uses the explicit
    # choice, including a JSON null or empty string. Reset can then reveal fresh provider data.
    row = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?',
                       (entry_id,)).fetchone()
    if field in RECORDING_FIELDS:
        conn.execute(f'UPDATE recordings SET {field}=? WHERE id=?', (value,row[0]))
    elif row[1]:
        conn.execute(f'UPDATE youtube_items SET {field}=? WHERE video_id=?', (value,row[1]))
    else:
        conn.execute(f'UPDATE unresolved_items SET {field}=? WHERE entry_id=?', (value,entry_id))
    table,column,subject = source_location(conn,entry_id,field)
    conn.execute(f'INSERT INTO {table} VALUES (?,?,?,?) ON CONFLICT({column},field) DO UPDATE '
                 'SET source=excluded.source,selected_at=excluded.selected_at',
                 (subject,field,source,time.time() if now is None else now))


def fill(conn, entry_id, fields, source, *, blank=False, now=None):
    """Select supported findings only where the effective projection is missing.

    The selection owner records evidence and updates the correct physical subject.
    Explicit choices remain above the refreshed base, including NULL and blank.
    Callers own the complete mutation transaction.
    """
    if fields.keys() - {*ITEM_FIELDS, *RECORDING_FIELDS}:
        raise ValueError('Unsupported fill metadata fields')
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    row = conn.execute('SELECT * FROM catalog_tracks WHERE id=?',(entry_id,)).fetchone()
    if row is None:
        return
    for field,value in fields.items():
        if value is not None and (not blank or value!='') and (row[field] is None or blank and row[field]==''):
            select(conn,entry_id,field,value,source,now=now)


def reuse_recording_selections(conn, previous, target):
    """Fill genuinely missing facts for a verified equivalent recording.

    Copy only selected values and their attribution. Historical findings remain on
    their original subjects. An existing selection, including a blank or null,
    belongs to the target and is never replaced by a merge.
    """
    if metadata_runtime.active(conn):
        entry = conn.execute('SELECT id FROM catalog_entries WHERE recording_id=? ORDER BY id LIMIT 1', (target,)).fetchone()
        original = conn.execute('SELECT id FROM catalog_entries WHERE recording_id=? ORDER BY id LIMIT 1', (previous,)).fetchone()
        if entry and original:
            for field in RECORDING_FIELDS:
                value = (genre_classifications.read(conn, 'recordings', previous) if field == 'genre' else
                         conn.execute(f'SELECT {field} FROM recordings WHERE id=?', (previous,)).fetchone()[0])
                current = (genre_classifications.read(conn, 'recordings', target) if field == 'genre' else
                           conn.execute(f'SELECT {field} FROM recordings WHERE id=?', (target,)).fetchone()[0])
                if value not in (None, '') and current in (None, '') and not selected_source(conn, entry[0], field):
                    metadata_runtime.select(conn, entry[0], field, value, selected_source(conn, original[0], field) or 'legacy_unknown')
        return
    before = conn.execute('SELECT * FROM recordings WHERE id=?', (previous,)).fetchone()
    after = conn.execute('SELECT * FROM recordings WHERE id=?', (target,)).fetchone()
    before, after = dict(before), dict(after)
    if genre_classifications.available(conn):
        before['genre'] = genre_classifications.read(conn, 'recordings', previous)
        after['genre'] = genre_classifications.read(conn, 'recordings', target)
    typed = metadata_evidence.available(conn)
    for field in RECORDING_FIELDS:
        if before[field] in (None, '') or after[field] not in (None, ''):
            continue
        table = 'recording_metadata_selections' if typed else 'recording_field_sources'
        if conn.execute(f'SELECT 1 FROM {table} WHERE recording_id=? AND field=?',
                        (target, field)).fetchone():
            continue
        if typed:
            metadata_evidence.transfer_recording_selection(conn, previous, target, field)
            continue
        conn.execute(f'UPDATE recordings SET {field}=? WHERE id=?', (before[field], target))
        conn.execute('INSERT INTO recording_field_sources(recording_id,field,source,selected_at) '
                     'SELECT ?,field,source,selected_at FROM recording_field_sources '
                     'WHERE recording_id=? AND field=?', (target, previous, field))


def apply_provider(conn, entry_id, provider, fields, providers, *, now=None, delivery_id=None,
                   supplying_observations=None):
    """The first provider wins disagreements; documented genre refinements can be more precise.

    Unknown legacy values and weak import hints are replaceable, while overrides live above
    this selection. Missing answers never erase a previous useful finding.
    """
    rank = {name:i for i,name in enumerate(providers)}
    typed = metadata_evidence.available(conn)
    observed_at = time.time() if now is None else now
    observations = {}
    if metadata_runtime.active(conn):
        metadata_runtime.validate_capture(conn, entry_id, provider, fields, delivery_id)
        if delivery_id is not None:
            previous = conn.execute('SELECT at FROM enrichment_provider_state WHERE recording_id=? AND provider=?',
                                    (delivery_id.recording, provider)).fetchone()
            if previous and previous[0] >= delivery_id.at:
                return {}
    if typed:
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        captured = {'mb_year' if name == 'year' else name: value for name, value in fields.items()
                    if ('mb_year' if name == 'year' else name) in (*ITEM_FIELDS, *RECORDING_FIELDS)}
        if supplying_observations is not None:
            if delivery_id is not None or supplying_observations.keys() - captured.keys():
                raise ValueError('Provider reuse requires its actual captured fields')
            observations = supplying_observations
        elif delivery_id is not None:
            observations = metadata_attempts.result_evidence(conn, entry_id, provider, delivery_id, fields)
        elif captured:
            delivery = metadata_deliveries.capture_state(conn, entry_id, provider, 'provider-fields',
                {'format': 'catalog-provider-fields-1', 'fields': captured}, now=observed_at)
            units = {field: metadata_values.DEFAULT_UNIT[field] for field, value in captured.items()
                     if field in metadata_values.NUMBER_FIELDS and value not in (None, '')}
            observations = metadata_evidence.record_provider(conn, delivery, captured, units=units)
    for concept,value in fields.items():
        field = 'mb_year' if concept == 'year' else concept
        if field not in (*ITEM_FIELDS,*RECORDING_FIELDS) or value in (None,''):
            continue
        if typed and supplying_observations is not None and field not in observations:
            continue
        recording = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?',
                                 (entry_id,)).fetchone()[0]
        if not typed and not metadata_runtime.active(conn):
            conn.execute('INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,'
                         'observed_at) VALUES (?,?,?,?,?,?)',
                         (entry_id,recording,field,provider,json.dumps(value),observed_at))
        previous = selected_source(conn,entry_id,field)
        refinement = False
        if field == 'genre' and previous in rank:
            current = genre_classifications.read(conn, 'recordings', recording)
            refinement = genre_catalog.is_subgenre(value, current)
            if previous != provider and genre_catalog.is_subgenre(current, value):
                continue
        if not refinement and previous in rank and rank[previous] < rank.get(provider,len(rank)):
            continue
        select(conn,entry_id,field,value,provider,now=observed_at,observation_id=observations.get(field))
        if not metadata_runtime.active(conn):
            hint_table = 'metadata_hints' if typed else 'catalog_field_hints'
            conn.execute(f'UPDATE {hint_table} SET active=0 WHERE entry_id=? AND field=?',
                         (entry_id,field))
    return observations


def refresh_genre(conn, recording_id, answers, providers):
    """Replace a superseded automatic genre when a refresh supplies an alternative.

    The caller supplies completed, current answers. Historical findings remain retained;
    item-scoped explicit choices continue to sit above this recording-level selection.
    """
    entry = conn.execute('SELECT id FROM catalog_entries WHERE recording_id=? ORDER BY id LIMIT 1',
                         (recording_id,)).fetchone()
    if entry is None:
        return
    previous = selected_source(conn, entry[0], 'genre')
    if not previous or previous not in answers or answers[previous]:
        return
    genre, source = None, None
    for name in providers:
        candidate = answers.get(name)
        if candidate and (genre is None or genre_catalog.is_subgenre(candidate, genre)):
            genre, source = candidate, name
    if genre:
        if metadata_attempts.available(conn):
            observations = metadata_attempts.reusable_evidence(conn, entry[0], source, {'genre': genre})
            if 'genre' in observations:
                select(conn, entry[0], 'genre', genre, source, observation_id=observations['genre'])
        else:
            select(conn, entry[0], 'genre', genre, source)
    # A no-match is absence of new evidence, not evidence that the previous genre
    # is wrong. Retain both the value and its attribution until a replacement exists.


def record_choice(conn, entry_id, field, value, source, *, now=None):
    if metadata_runtime.active(conn):
        return metadata_runtime.choose(conn, entry_id, field, value, source=source, now=now)
    if metadata_evidence.available(conn):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        return metadata_evidence.choose(conn, entry_id, field, value, source=source, now=now)
    conn.execute('INSERT INTO metadata_decisions(entry_id,field,value_json,source,chosen_at) '
                 'VALUES (?,?,?,?,?)',
                 (entry_id,field,json.dumps(value),source,time.time() if now is None else now))
