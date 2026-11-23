"""The current Home offer deduplication window, independent of display output."""
import json

from . import worker_settings
from .recommendation_work import finite

KEY = 'home_shown'
MAX_SIGNATURES = 100


def parse(value, built_at):
    finite(built_at)
    if not isinstance(value, dict):
        raise ValueError('Home offer deduplication must be an object')
    if not value:
        return None
    if set(value) != {'epoch', 'signatures'} or type(value['epoch']) is not int or value['epoch'] < 0:
        raise ValueError('Invalid Home offer window')
    signatures = value['signatures']
    if (not isinstance(signatures, list) or len(signatures) > MAX_SIGNATURES
            or any(not isinstance(item, str) or not 0 < len(item.encode()) <= 1024 for item in signatures)
            or len(set(signatures)) != len(signatures)):
        raise ValueError('Invalid Home offer signatures')
    return value['epoch'], sorted(signatures)


def read(conn):
    raw = worker_settings.read(conn, KEY)
    if raw is None:
        return None
    value = json.loads(raw)
    return {'epoch': value['epoch'], 'signatures': value['signatures']}


def replace(conn, value, built_at):
    if not conn.in_transaction:
        raise RuntimeError('Home offer replacement requires an outer transaction')
    parsed = parse(value, built_at)
    if parsed is None:
        worker_settings.save(conn, KEY, None)
        return
    epoch, signatures = parsed
    value = json.dumps({'epoch': epoch, 'built_at': built_at, 'signatures': signatures})
    worker_settings.save(conn, KEY, value)
