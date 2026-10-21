"""Runtime catalog-key serialization, independent of historical data migrations.

Unmarked saved state uses format 1. Dictionary writes carry catalog_state_version; future formats
must add an explicit reader/rewriter here, and new writers must stamp their format. Unknown versions
are rejected so callers preserve them instead of silently rewriting a format they cannot interpret.
catalog_identity_version describes key identity, separately from this serialization format.
"""
from copy import deepcopy

STATE_VERSION = 1
IDENTITY_VERSION = 2
KEY_LIST_FIELDS = ('keys', 'rep_keys', 'pos_keys', 'neg_keys', 'path_keys')
SEED_FIELDS = ('seeds', 'central_seeds')
ITEM_LIST_FIELDS = ('nodes', 'pool', 'picked')


def _rewrite(value, keymap):
    if isinstance(value, list) and all(isinstance(k, str) for k in value):
        return [keymap.get(k, k) for k in value]
    if not isinstance(value, dict):
        return value
    if value.get('catalog_state_version', 1) != STATE_VERSION:
        raise ValueError('Unsupported catalog state format')
    for name in KEY_LIST_FIELDS:
        keys = value.get(name)
        if isinstance(keys, list) and all(isinstance(k, str) for k in keys):
            value[name] = [keymap.get(k, k) for k in keys]
    for name in SEED_FIELDS:
        if isinstance(value.get(name), list):
            value[name] = [_rewrite(seed, keymap) if isinstance(seed, dict) else
                           keymap.get(seed, seed) if isinstance(seed, str) else seed
                           for seed in value[name]]
    for name in ITEM_LIST_FIELDS:
        if isinstance(value.get(name), list):
            for item in value[name]:
                if isinstance(item, dict):
                    for field in ('key', 'identity_key'):
                        if isinstance(item.get(field), str):
                            item[field] = keymap.get(item[field], item[field])
                    _rewrite(item, keymap)
    value['catalog_identity_version'] = IDENTITY_VERSION
    return value


def rekey_state(value, keymap):
    """Copy and rewrite only declared recording-key slots, preserving labels and exact media IDs."""
    if isinstance(value, dict) and value.get('catalog_state_version', 1) != STATE_VERSION:
        raise ValueError('Unsupported catalog state format')
    converted = _rewrite(deepcopy(value), keymap)
    if isinstance(converted, dict):
        converted['catalog_state_version'] = STATE_VERSION
    return converted
