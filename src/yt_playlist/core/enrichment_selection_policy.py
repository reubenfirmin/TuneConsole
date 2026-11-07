"""Provider precedence and reuse rules, independent of storage."""
import json
from collections.abc import Mapping

from yt_playlist.core.metadata_contract import Capture, PROVIDER_FIELDS
from yt_playlist.core.metadata_values import ITEM_FIELDS, RECORDING_FIELDS, scope
from yt_playlist.util import genre_catalog


def canonical_fields(fields):
    return {'mb_year' if field == 'year' else field: value for field, value in fields.items()
            if ('mb_year' if field == 'year' else field) in (*ITEM_FIELDS, *RECORDING_FIELDS)}


def result_fields(provider, fields, run_id):
    if not isinstance(fields, Mapping) or len(fields) > len(PROVIDER_FIELDS) or fields.keys() - set(PROVIDER_FIELDS):
        raise ValueError('Unsupported provider result fields')
    if not isinstance(provider, str) or not 0 < len(provider.encode()) <= 4096:
        raise ValueError('A provider result needs a bounded provider identity')
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip() or len(run_id.encode()) > 4096):
        raise ValueError('A provider result needs a bounded run identity')
    if 'year' in fields and 'mb_year' in fields:
        validate_fields({'year': fields['mb_year']}, {'year': fields['year']})
    return canonical_fields(fields)


def validate_capture(subject, provider, fields, captured, *, run_id=None, complete=False):
    if captured is None:
        return
    if (not isinstance(captured, Capture) or
            (captured.entry, captured.recording, captured.video, captured.provider) !=
            (subject['entry_id'], subject['recording_id'], subject['video_id'], provider) or
            run_id is not None and captured.run != run_id):
        raise ValueError('Provider result has a different captured input or fields')
    validate_fields(captured.fields, fields)
    if complete and captured.fields != fields:
        raise ValueError('Provider completion must account for its whole captured result')


def validate_delivery(subject, provider, delivery, *, run_id=None):
    if (delivery is None or delivery['key_contract'] == 'legacy_record' or
            (delivery['provider'], delivery['input_entry_id'], delivery['input_recording_id'], delivery['input_video_id']) !=
            (provider, subject['id'], subject['recording_id'], subject['video_id'])):
        raise ValueError('Provider result has a different provider or captured input')
    if run_id is not None and delivery['delivery_key'] != 'enrichment-result:' + run_id:
        raise ValueError('Provider result belongs to a different run')


def provider_wins(field, value, provider, previous, current_genre, providers):
    rank = {name: index for index, name in enumerate(providers)}
    refinement = False
    if field == 'genre' and previous in rank:
        refinement = genre_catalog.is_subgenre(value, current_genre)
        if previous != provider and genre_catalog.is_subgenre(current_genre, value):
            return False
    return refinement or previous not in rank or rank[previous] >= rank.get(provider, len(rank))


def validate_fields(supplied, fields):
    encode = lambda value: json.dumps(value, sort_keys=True, allow_nan=False)
    if any(field not in supplied or encode(supplied[field]) != encode(value) for field, value in fields.items()):
        raise ValueError('Provider fields differ from their supplying result')


def delivery_fields(body):
    if isinstance(body, dict) and body.get('format') == 'enrichment-result-1':
        fields = body.get('fields')
    elif (isinstance(body, dict) and body.get('format') == 'legacy-metadata-record-1'
          and body.get('owner') in ('provider_attempts', 'enrichment_provider_state')):
        fields = json.loads(body['row']['fields_json'])
    else:
        raise ValueError('Provider fields have a different retained delivery owner')
    if not isinstance(fields, dict):
        raise ValueError('Invalid retained provider result')
    return fields


def scoped_fields(subject, data):
    if data.get('recording_id') != subject['recording_id']:
        return {}
    inputs = data.get('inputs', {})
    return {field: value for field, value in data.get('fields', {}).items()
            if field in RECORDING_FIELDS or inputs.get(field,
                [data.get('entry_id'), data.get('input_video_id')]) == [subject['entry_id'], subject['video_id']]}


def reusable_observation(subject, field, value, row):
    """Return whether a retained observation can supply this exact subject."""
    if row is None or row['value'].kind in ('unresolved', 'withdrawn'):
        return False
    validate_fields({field: row['value'].selected()}, {field: value})
    if field in RECORDING_FIELDS and row['field_scope'] == 'legacy_recording':
        return 'transfer'
    return (row['recording_id'] == subject['recording_id'] and row['field_scope'] == scope(field)
            and (field in RECORDING_FIELDS or row['entry_id'] == subject['entry_id']))


def replaces_hint(layout, source, current, hints):
    if layout == 'runtime':
        return source in ('catalog_import', 'discovery_import')
    return any(hint['value'].kind not in ('unresolved', 'withdrawn') and hint['value'].selected() == current
               for hint in hints)
