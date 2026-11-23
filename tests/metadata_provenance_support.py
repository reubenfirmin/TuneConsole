"""Inspect retained metadata evidence and choices in supported test fixtures.

Use the storage owners for layout selection and file reads. This helper keeps
preservation assertions independent of the application's reporting API.
"""
import json

from yt_playlist.core import catalog, metadata, metadata_conflicts, metadata_evidence
from yt_playlist.core import metadata_runtime, metadata_runtime_files


def metadata_provenance(store, entry):
    with store._lock:
        conn = store.conn
        track = conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()
        assert track is not None
        if metadata_evidence.available(conn):
            return _evidence_provenance(conn, entry, track)
        if metadata_runtime.active(conn):
            choices = {metadata_runtime.FIELDS[row['field']-1]: row for row in conn.execute(
                'SELECT * FROM current_runtime_choices WHERE entry_id=?', (entry,))}
            decisions = []
            for field, choice in choices.items():
                for revision in range(1, choice['revision']+1):
                    document = metadata_runtime_files.read(conn, 'choices', (choice['id'],), revision)
                    decisions.append({'id': choice['id'], 'entry_id': entry, 'field': field,
                        'value_json': json.dumps(document['value']), 'source': document['source'],
                        'chosen_at': document['at'], 'withdrawn': document['withdrawn']})
            fields = {}
            for field in (*catalog.ITEM_FIELDS, *catalog.RECORDING_FIELDS):
                choice = choices.get(field)
                override = bool(choice and choice['kind'] != 0)
                source = None
                if override:
                    source = conn.execute('SELECT provider FROM enrichment_provider_config WHERE metadata_provider_id=?',
                                          (choice['provider_id'],)).fetchone()[0]
                fields[field] = {'value': track[field], 'override': override,
                    'source': source or metadata.selected_source(conn, entry, field)
                    or ('missing' if track[field] is None else 'unknown')}
            return {'fields': fields, 'decisions': sorted(decisions, key=lambda d: (
                d['chosen_at'] is not None, d['chosen_at'] if d['chosen_at'] is not None else 0, d['id']))}
        choices = {row['field']: dict(row) for row in conn.execute(
            'SELECT * FROM entry_field_choices WHERE entry_id=?', (entry,))}
        fields = {}
        for field in (*catalog.ITEM_FIELDS, *catalog.RECORDING_FIELDS):
            choice = choices.get(field)
            fields[field] = {'value': track[field], 'override': choice is not None,
                            'source': choice['source'] if choice else
                            metadata.selected_source(conn, entry, field) or
                            ('missing' if track[field] is None else 'unknown')}
        decisions = [dict(row) for row in conn.execute(
            'SELECT * FROM metadata_decisions'+
            ' WHERE entry_id=? ORDER BY chosen_at,id', (entry,))]
        return {'fields': fields, 'decisions': decisions}


def _evidence_provenance(conn, entry, track):
    observations, deliveries = {}, {}

    def explain(identity):
        if identity is None:
            return None
        if identity not in observations:
            row = dict(conn.execute('SELECT * FROM metadata_observations WHERE id=?', (identity,)).fetchone())
            delivery = row['delivery_id']
            if delivery is not None and delivery not in deliveries:
                deliveries[delivery] = dict(conn.execute('SELECT * FROM metadata_deliveries WHERE id=?',
                                                        (delivery,)).fetchone())
            observations[identity] = {'observation': row, 'delivery': deliveries.get(delivery),
                                      'derived_from': explain(row['derived_from_observation_id'])}
        return observations[identity]

    selections = {row['field']: dict(row) for row in conn.execute(
        'SELECT * FROM current_metadata_selections WHERE recording_id=?', (track['recording_id'],))}
    selections.update({row['field']: dict(row) for row in conn.execute(
        'SELECT * FROM current_metadata_selections WHERE entry_id=?', (entry,))})
    choices = {field: explain(observation) for field, observation in conn.execute(
        'SELECT h.key,h.atom FROM track_sources s,json_each(s.choice_observations) h WHERE s.id=?', (entry,))}
    fields, lineage = {}, {}
    for field in (*catalog.ITEM_FIELDS, *catalog.RECORDING_FIELDS):
        selection, choice = selections.get(field), choices.get(field)
        overridden = bool(choice and choice['observation']['value_kind'] != 'withdrawn')
        selected = explain(selection['observation_id']) if selection else None
        effective = choice if overridden else selected
        source = ((effective['delivery']['provider'] if effective['delivery'] else None)
                  or effective['observation']['legacy_source']) if effective else None
        source = source or (selection['legacy_source'] if selection and not overridden else None)
        fields[field] = {'value': track[field], 'override': overridden,
                         'source': source or ('missing' if track[field] is None else 'unknown')}
        lineage[field] = {'selection': selection, 'evidence': selected, 'choice': choice}
    decisions = []
    for row in conn.execute("SELECT id FROM metadata_observations WHERE entry_id=? "
            "AND origin IN ('user','legacy_choice') ORDER BY observed_at,id", (entry,)):
        evidence = explain(row['id'])
        value = evidence['observation']
        withdrawn = value['value_kind'] == 'withdrawn'
        decisions.append({'id': value['id'], 'entry_id': entry, 'field': value['field'],
            'value_json': json.dumps(None if withdrawn or value['value_kind'] == 'unresolved'
                                     else metadata_evidence.value_from_row(value).selected()),
            'source': value['legacy_source'], 'chosen_at': value['observed_at'],
            'legacy_ref': value['legacy_reference'], 'withdrawn': withdrawn, 'evidence': evidence})
    conflicts = [{**row, 'candidates': [explain(candidate['observation_id'])
                  for candidate in metadata_conflicts.candidates(conn, row['id'])]}
                 for row in metadata_conflicts.for_entry(conn, entry)]
    return {'fields': fields, 'lineage': lineage, 'decisions': decisions, 'conflicts': conflicts}
