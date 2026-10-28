"""Worker-only paid copy, with durable album identity and request accounting."""
import hashlib
import json

from yt_playlist.integrations.deepseek import DEFAULT_DAILY_LIMIT, MAX_DAILY_LIMIT
from yt_playlist.providers import deepseek
from yt_playlist.util.matching import recording_title


def identity(album):
    # Preserve live/remix/mix qualifiers; equivalent catalogue IDs share the result.
    fields = [recording_title(album.get(key)) for key in ('artist', 'title')]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def settings(store, now):
    return deepseek.settings(store, now)


def _description(record):
    if not record or not record.get('description'):
        return None
    return {'text': record['description'], 'source': 'AI description · DeepSeek Flash',
            'url': 'https://www.deepseek.com/', 'quote': False,
            'kind': 'generated', 'acquisition': 'automatic', 'model': record['model']}


def read(store, album):
    """Request-safe read; never starts a generation or reserves budget."""
    return _description(store.rec_proposals.get_proposals('album_description:' + identity(album)))


def get(store, album, now, *, now_fn=None):
    """Read durable copy even when disabled; reserve before any paid network work."""
    album_id = identity(album)
    cached = store.rec_proposals.get_proposals('album_description:' + album_id)
    if _description(cached):
        return _description(cached), cached

    def project(send):
        result = deepseek.describe(album, send)
        finished_at = now_fn() if now_fn else now
        record = {**result, 'at': finished_at, 'status': 'ready'}
        if not record['description']:
            record.update(status='unknown', retry_at=finished_at + 7 * 86400)
        return record

    record = deepseek.generate(store, album_id, project, now, now_fn=now_fn)
    return _description(record), record
