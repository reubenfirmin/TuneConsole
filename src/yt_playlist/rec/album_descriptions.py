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


def settings(store, now, *, typed=None):
    return deepseek.settings(store, now, paid=typed)


def _description(record):
    if not record or not record.get('description'):
        return None
    return {'text': record['description'], 'source': 'AI description · DeepSeek Flash',
            'url': 'https://www.deepseek.com/', 'quote': False,
            'kind': 'generated', 'acquisition': 'automatic', 'model': record['model']}


def _typed_cached(store, album):
    owner = store.album_descriptions
    album_id = owner.album_id(album)
    saved = owner.successful_result(album_id) if album_id is not None else None
    # This is an unresolved historical lookup, never an identity association or
    # permission to make a new paid request using an artist/title fingerprint.
    legacy = owner.legacy_result(identity(album))
    if saved and saved.get('description'):
        return album_id, saved
    if legacy and legacy.get('description'):
        return album_id, legacy
    policy = owner.policy_version(deepseek.description_policy())
    return album_id, (owner.cached(album_id, policy) if album_id is not None else None) or legacy


def read(store, album, *, typed=None):
    """Request-safe read; never starts a generation or reserves budget."""
    typed = getattr(store, '_paid_descriptions', False) if typed is None else typed
    record = (_typed_cached(store, album)[1] if typed else
              store.rec_proposals.get_proposals('album_description:' + identity(album)))
    return _description(record)


def get(store, album, now, *, now_fn=None, typed=None):
    """Read durable copy even when disabled; reserve before any paid network work."""
    typed = getattr(store, '_paid_descriptions', False) if typed is None else typed
    if typed:
        album_id, cached = _typed_cached(store, album)
    else:
        album_id = identity(album)
        cached = store.rec_proposals.get_proposals('album_description:' + album_id)
    if _description(cached):
        return _description(cached), cached
    if typed:
        if cached and cached.get('status') in ('unknown', 'invalid_response') and cached.get('retry_at', 0) > now:
            return None, cached
        if album_id is None:
            return None, {'status': 'unresolved_album'}

    def project(send):
        result = deepseek.describe(album, send)
        finished_at = now_fn() if now_fn else now
        record = {**result, 'at': finished_at, 'status': 'ready'}
        if not record['description']:
            record.update(status='unknown', retry_at=finished_at + 7 * 86400)
        return record

    kwargs = {'policy': deepseek.description_policy()} if typed else {}
    record = deepseek.generate(store, album_id, project, now, now_fn=now_fn, **kwargs)
    return _description(record), record
