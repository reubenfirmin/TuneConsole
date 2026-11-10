"""Validation of a pending deliberate album queue before catalog writes."""
from . import entity_names


def pending_album(value):
    value = entity_names.account_document(value)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) - {'opportunity_id', 'browse', 'playlist', 'at', 'action_id', 'videos', 'account_id'}
            or not {'opportunity_id', 'browse', 'playlist', 'at'} <= value.keys()
            or ('videos' in value and not isinstance(value['videos'], list))):
        raise ValueError('Unsupported pending album playback context')
    if not value['browse']:
        raise ValueError('An album requires a YouTube browse ID.')
    if any(not isinstance(video, str) or not video for video in value.get('videos', [])):
        raise ValueError('Pending album playback requires its exact video IDs')
    return value
