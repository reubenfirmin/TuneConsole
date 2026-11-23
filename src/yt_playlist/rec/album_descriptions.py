"""Worker-only paid copy, with durable album identity and request accounting.

Owns the Description controller's consent/budget policy, durable reservation
and completion (_admit, below). The DeepSeek IAO owns only one bounded, paced,
re-admitted attempt (providers.deepseek.attempt) -- see its docstring for why
the recheck-immediately-before-dispatch timing lives there and not here.
"""
import hashlib
import json
import threading

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.deepseek import (DescriptionUnavailable,
                                                RETRY_DELAYS, credential_id)
from yt_playlist.providers import deepseek
from yt_playlist.rec import paid_description_policy
from yt_playlist.util import net
from yt_playlist.util.matching import recording_title


class _AdmissionChanged(Exception):
    """A recheck immediately before dispatch found consent/credential/reservation state had moved."""


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


def _cached(store, album):
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


def read(store, album):
    """Request-safe read; never starts a generation or reserves budget."""
    _, record = _cached(store, album)
    return _description(record)


def get(store, album, now, *, now_fn=None):
    """Read durable copy even when disabled; reserve before any paid network work."""
    album_id, cached = _cached(store, album)
    description = _description(cached)
    if description:
        return description, cached
    if cached and cached.get('status') in ('unknown', 'invalid_response') and cached.get('retry_at', 0) > now:
        return None, cached
    if album_id is None:
        return None, {'status': 'unresolved_album'}

    def project(send):
        payload = deepseek.description_request(album)
        response = send(payload)
        document, usage = deepseek.description_response(response, deepseek.MODEL)
        result = deepseek.project_description(album, document, usage)
        finished_at = now_fn() if now_fn else now
        record = {**result, 'at': finished_at, 'status': 'ready'}
        if not record['description']:
            record.update(status='unknown', retry_at=finished_at + 7 * 86400)
        return record

    record = _admit(store, album_id, project, now, now_fn=now_fn, policy=deepseek.description_policy())
    return _description(record), record


def _admit(store, album_id, project, now, *, now_fn=None, policy):
    """Reserve and publish a single DeepSeek attempt, including failures and abstentions.

    Durable reservations serialize callers of the same database, including
    separate processes. Cancellation retains the counted short lease because
    a POST may already have reached the vendor. It is never replayed here.
    """
    return admit(deepseek.description_access(store), store.album_descriptions,
                 album_id, project, now, now_fn=now_fn, policy=policy,
                 attempt=deepseek.attempt, remember_failure=deepseek.remember_failure)


def admit(access, requests, album_id, project, now, *, now_fn=None, policy,
          attempt=deepseek.attempt, remember_failure=deepseek.remember_failure):
    """Complete paid command over access, durable requests, projection and transport ports."""
    check_cancellable_work()
    clock = now_fn or (lambda: now)
    admitted_at = clock()
    key = access.key()
    config = access.settings(admitted_at)
    if not key or not config['enabled'] or not config['daily_limit']:
        return {'status': 'not_configured' if not key else 'paused'}
    wait = access.availability(key, admitted_at)
    if wait:
        if config['requests_today'] >= config['daily_limit']:
            return {'status': 'daily_limit', 'retry_at': (int(admitted_at // 86400) + 1) * 86400}
        return dict(wait)
    digest = credential_id(key)
    claim = requests.claim(album_id, policy, digest, config['daily_limit'], admitted_at)
    if claim['status'] != 'reserved':
        return claim.get('record') or claim
    active, sent, lock = True, False, threading.Lock()

    def reauthorize():
        nonlocal sent
        with lock:
            if not active or sent:
                raise RuntimeError('A DeepSeek admission permits exactly one request')
            sent = True
        # Settings may have changed while awaiting the shared request slot.
        current = access.settings(clock())
        if (not current['enabled'] or not current['daily_limit']
                or current['requests_today'] > current['daily_limit'] or access.key() != key):
            raise _AdmissionChanged()
        wait = access.availability(key, clock())
        if wait:
            raise DescriptionUnavailable(wait['status'], wait['retry_at'])
        authorized = requests.authorize(claim['request_id'], claim['token'], digest,
                                        current['daily_limit'], clock())
        if not authorized:
            raise _AdmissionChanged()

    def send(payload):
        return attempt(key, payload, now_fn=clock, reauthorize=reauthorize)

    try:
        record = project(send)
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except _AdmissionChanged:
        finished_at = clock()
        record = {'status': 'paused', 'at': finished_at, 'retry_at': finished_at + 60}
    except DescriptionUnavailable as error:
        finished_at = clock()
        # HTTP Retry-After is captured against this attempt's clock as well.
        record = {'status': error.reason, 'at': finished_at,
                  'retry_at': max(finished_at + RETRY_DELAYS[error.reason], error.retry_at)}
        remember_failure(digest, record)
        net.record_probe_failure(DescriptionUnavailable(error.reason, record['retry_at']))
    finally:
        active = False
    if not requests.finish(claim['request_id'], claim['token'], record, clock()):
        return {'status': 'stale'}
    return record
