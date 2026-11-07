"""Encode recommendation documents, retaining their policy references."""
import json

from yt_playlist.core import json_cache


def encode_proposals(surface, value):
    if surface not in {'mode_bundles', 'fresh_pool'}:
        return json.dumps(value)
    from yt_playlist.core.policy_config import references
    return json_cache.encode(value, group_pools=surface == 'mode_bundles', references=references(value))
