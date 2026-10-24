"""Worker-only paid copy, with durable album identity and request accounting."""
import hashlib
import json
import os

from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded
from yt_playlist.providers import deepseek
from yt_playlist.util.matching import recording_title

DEFAULT_DAILY_LIMIT = 20
MAX_DAILY_LIMIT = 100


def identity(album):
    # Preserve live/remix/mix qualifiers; equivalent catalogue IDs share the result.
    fields = [recording_title(album.get(key)) for key in ('artist', 'title')]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def _credential(key):
    return hashlib.sha256(key.encode()).hexdigest() if key else None


def settings(store, now):
    key = deepseek.api_key(store)
    try:
        limit = int(store.settings.get_setting('deepseek_daily_limit', str(DEFAULT_DAILY_LIMIT)))
    except ValueError:
        limit = DEFAULT_DAILY_LIMIT
    usage = store.rec_proposals.get_proposals('album_description_usage') or {}
    status = usage.get('status') if usage.get('credential') == _credential(key) else None
    saved = bool((store.settings.get_setting('deepseek_api_key') or '').strip())
    source = ('environment' if (os.environ.get('DEEPSEEK_API_KEY') or '').strip()
              else 'settings' if saved else 'config' if key else None)
    return {'configured': bool(key),
            'saved_key': saved, 'key_source': source,
            'enabled': store.settings.get_setting('deepseek_album_descriptions_enabled', '1') == '1',
            'daily_limit': max(0, min(MAX_DAILY_LIMIT, limit)),
            'requests_today': usage.get('requests', 0) if usage.get('day') == int(now // 86400) else 0,
            'status': status, 'retry_at': usage.get('retry_at', 0)}


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
    config = settings(store, now)
    if not config['configured'] or not config['enabled'] or not config['daily_limit']:
        return None, {'status': 'not_configured' if not config['configured'] else 'paused'}
    key = deepseek.api_key(store)
    if not key:
        return None, {'status': 'not_configured'}
    # Public metadata may have taken time earlier in this Home job. The paid
    # reservation and its lease use the current clock, not the job's start time.
    now = now_fn() if now_fn else now
    claim = store.rec_proposals.claim_album_description(
        album_id, _credential(key), config['daily_limit'], now)
    if claim['status'] != 'reserved':
        return _description(claim.get('record')), claim.get('record') or claim
    try:
        result = deepseek.describe(album, key)
        now = now_fn() if now_fn else now
        record = {**result, 'at': now, 'status': 'ready'}
        if not record['description']:
            record.update(status='unknown', retry_at=now + 7 * 86400)
    except (WorkClosing, WorkBudgetExceeded):
        # The pre-paid reservation survives cancellation; its short lease expires.
        raise
    except deepseek.DescriptionUnavailable as error:
        now = now_fn() if now_fn else now
        delay = {'auth': 86400, 'balance': 86400, 'rate_limit': 3600,
                 'invalid_response': 86400}.get(error.reason, 900)
        record = {'status': error.reason, 'at': now, 'retry_at': now + delay}
    # Persist the paid result before publishing a disposable Home projection.
    store.rec_proposals.finish_album_description(album_id, claim['token'], record, now)
    return _description(record), record
