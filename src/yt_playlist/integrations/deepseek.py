"""Metered DeepSeek access: one durable admission authorizes one billed POST."""
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import IntegrationUnavailable, RateLimiter
from yt_playlist.providers import credentials
from yt_playlist.util import net

ENDPOINT = 'https://api.deepseek.com/chat/completions'
MAX_RESPONSE_BYTES = 65536
TIMEOUT_S = 20
DEFAULT_DAILY_LIMIT = 20
MAX_DAILY_LIMIT = 100
_DELAYS = {'auth': 86400, 'balance': 86400, 'rate_limit': 3600,
           'invalid_response': 86400, 'unavailable': 900}


def credential_id(key):
    return hashlib.sha256(key.encode()).hexdigest() if key else None


class DescriptionUnavailable(IntegrationUnavailable):
    """A safe category and retry deadline, without vendor bodies or credentials."""

    def __init__(self, reason, retry_at=0):
        self.reason = reason if reason in _DELAYS else 'unavailable'
        super().__init__('deepseek', self.reason, retry_at, outage=self.reason == 'unavailable')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _AdmissionChanged(Exception):
    pass


class DeepSeekIAO:
    """Resolve credentials and coordinate budget, availability, pacing and transport.

    The projection callback builds and validates content. It receives a one-use
    send function, never a credential or an unmetered public HTTP helper.
    """

    def __init__(self, *, now_fn=time.time, open_fn=None, pacer=None):
        self.now = now_fn
        self.open = open_fn
        self.pacer = pacer or RateLimiter(1.0)
        self._lock = threading.RLock()
        self._host = {}
        self._credentials = OrderedDict()

    @staticmethod
    def api_key(store=None, config_path=None):
        return credentials.resolve(env_name='DEEPSEEK_API_KEY', setting_name='deepseek_api_key',
                                   store=store, config_path=config_path)

    def settings(self, store, now):
        key = self.api_key(store)
        try:
            limit = int(store.settings.get_setting('deepseek_daily_limit', str(DEFAULT_DAILY_LIMIT)))
        except (ValueError, TypeError):
            limit = DEFAULT_DAILY_LIMIT
        usage = store.rec_proposals.get_proposals('album_description_usage') or {}
        wait = self.availability(store, key, now)
        saved = bool((store.settings.get_setting('deepseek_api_key') or '').strip())
        source = ('environment' if (os.environ.get('DEEPSEEK_API_KEY') or '').strip()
                  else 'settings' if saved else 'config' if key else None)
        status = wait.get('status') or (usage.get('status')
                                       if usage.get('credential') == credential_id(key) else None)
        return {'configured': bool(key), 'saved_key': saved, 'key_source': source,
                'enabled': store.settings.get_setting('deepseek_album_descriptions_enabled', '1') == '1',
                'daily_limit': max(0, min(MAX_DAILY_LIMIT, limit)),
                'requests_today': usage.get('requests', 0) if usage.get('day') == int(now // 86400) else 0,
                'status': status, 'retry_at': wait.get('retry_at', 0)}

    def availability(self, store, key, now):
        digest = credential_id(key)
        with self._lock:
            candidates = [self._host, self._credentials.get(digest, {})]
        candidates.extend(store.rec_proposals.deepseek_availability(digest))
        blocked = [wait for wait in candidates if wait.get('status') == 'auth'
                   or wait.get('retry_at', 0) > now]
        return max(blocked, key=lambda wait: (wait.get('status') == 'auth', wait.get('retry_at', 0)),
                   default={})

    def _remember(self, digest, record):
        if record['status'] not in ('auth', 'balance', 'rate_limit', 'unavailable'):
            return
        wait = {key: record[key] for key in ('status', 'retry_at')}
        with self._lock:
            if record['status'] == 'unavailable':
                if wait['retry_at'] > self._host.get('retry_at', 0):
                    self._host = wait
            else:
                self._credentials[digest] = wait
                self._credentials.move_to_end(digest)
                while len(self._credentials) > 64:
                    self._credentials.popitem(last=False)

    def _request(self, payload, key, *, now_fn=None):
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                     'Accept': 'application/json'}, method='POST')
        try:
            # Resolve the currently installed egress guard, and never forward credentials.
            open_request = self.open or urllib.request.build_opener(_NoRedirect()).open
            with open_request(request, timeout=TIMEOUT_S) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise DescriptionUnavailable('invalid_response')
                return json.loads(body)
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except urllib.error.HTTPError as error:
            reason = ('auth' if error.code in (401, 403) else 'balance' if error.code == 402
                      else 'rate_limit' if error.code == 429 else 'invalid_response'
                      if error.code in (400, 422) else 'unavailable')
            retry_at = net.retry_after(error, (now_fn or self.now)())
            error.close()
            raise DescriptionUnavailable(reason, retry_at) from None
        except DescriptionUnavailable:
            raise
        except ValueError:
            raise DescriptionUnavailable('invalid_response') from None
        except OSError:
            raise DescriptionUnavailable('unavailable') from None

    def complete(self, store, identity, project, now, *, now_fn=None):
        """Reserve and publish a single attempt, including failures and abstentions.

        Durable reservations serialize callers of the same database, including
        separate processes. Cancellation retains the counted short lease because
        a POST may already have reached the vendor. It is never replayed here.
        """
        check_cancellable_work()
        clock = now_fn or (lambda: now)
        admitted_at = clock()
        key = self.api_key(store)
        config = self.settings(store, admitted_at)
        if not key or not config['enabled'] or not config['daily_limit']:
            return {'status': 'not_configured' if not key else 'paused'}
        wait = self.availability(store, key, admitted_at)
        if wait:
            if config['requests_today'] >= config['daily_limit']:
                return {'status': 'daily_limit', 'retry_at': (int(admitted_at // 86400) + 1) * 86400}
            return dict(wait)
        digest = credential_id(key)
        claim = store.rec_proposals.claim_album_description(identity, digest, config['daily_limit'], admitted_at)
        if claim['status'] != 'reserved':
            return claim.get('record') or claim
        active, sent = True, False

        def send(payload):
            nonlocal sent
            with self._lock:
                if not active or sent:
                    raise RuntimeError('A DeepSeek admission permits exactly one request')
                sent = True
            check_cancellable_work()
            self.pacer.wait()
            check_cancellable_work()
            # Settings may have changed while awaiting the shared request slot.
            current = self.settings(store, clock())
            if (not current['enabled'] or not current['daily_limit']
                    or current['requests_today'] > current['daily_limit'] or self.api_key(store) != key):
                raise _AdmissionChanged()
            wait = self.availability(store, key, clock())
            if wait:
                raise DescriptionUnavailable(wait['status'], wait['retry_at'])
            if not store.rec_proposals.authorize_album_description_send(
                    claim['token'], digest, current['daily_limit'], clock()):
                raise _AdmissionChanged()
            return self._request(payload, key, now_fn=clock)

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
                      'retry_at': max(finished_at + _DELAYS[error.reason], error.retry_at)}
            self._remember(digest, record)
            net.record_probe_failure(DescriptionUnavailable(error.reason, record['retry_at']))
        finally:
            active = False
        store.rec_proposals.finish_album_description(identity, claim['token'], record, clock())
        return record
