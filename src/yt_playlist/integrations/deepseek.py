"""Metered DeepSeek access: one durable admission authorizes one billed POST."""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import IntegrationUnavailable, RateLimiter
from yt_playlist.util import net

ENDPOINT = 'https://api.deepseek.com/chat/completions'
MAX_RESPONSE_BYTES = 65536
TIMEOUT_S = 20
RETRY_DELAYS = {'auth': 86400, 'balance': 86400, 'rate_limit': 3600,
                'invalid_response': 86400, 'unavailable': 900}


def credential_id(key):
    return hashlib.sha256(key.encode()).hexdigest() if key else None


class DescriptionUnavailable(IntegrationUnavailable):
    """A safe category and retry deadline, without vendor bodies or credentials."""

    def __init__(self, reason, retry_at=0):
        self.reason = reason if reason in RETRY_DELAYS else 'unavailable'
        super().__init__('deepseek', self.reason, retry_at, outage=self.reason == 'unavailable')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def description_response(data, expected_model):
    """Decode the provider envelope; no album or prose-selection policy lives here."""
    try:
        if not isinstance(data, dict) or data.get('model') != expected_model:
            raise ValueError
        choice = data['choices'][0]
        if not isinstance(choice, dict) or choice.get('finish_reason') != 'stop':
            raise ValueError
        message = choice['message']
        if not isinstance(message, dict) or not isinstance(message.get('content'), str):
            raise ValueError
        document = json.loads(message['content'])
        if not isinstance(document, dict):
            raise ValueError
        raw = data.get('usage') or {}
        usage = {key: value for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
            if type(value := raw.get(key)) is int and value >= 0} if isinstance(raw, dict) else {}
        return document, usage
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        raise DescriptionUnavailable('invalid_response') from None


class DeepSeekIAO:
    """Bounded paid transport, pacing and process-local provider availability."""

    def __init__(self, *, now_fn=time.time, open_fn=None, pacer=None):
        self.now = now_fn
        self.open = open_fn
        self.pacer = pacer or RateLimiter(1.0)
        self._lock = threading.RLock()
        self._host = {}
        self._credentials = OrderedDict()

    def availability(self, digest, now):
        with self._lock:
            candidates = [self._host, self._credentials.get(digest, {})]
        blocked = [wait for wait in candidates if wait.get('status') == 'auth'
                   or wait.get('retry_at', 0) > now]
        return max(blocked, key=lambda wait: (wait.get('status') == 'auth', wait.get('retry_at', 0)),
                   default={})

    def remember(self, digest, record):
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

    def attempt(self, key, payload, *, now_fn, reauthorize):
        """One admitted bounded attempt: the IAO's entire role in a paid request.

        Paces, then calls the supplied zero-arg `reauthorize` immediately before
        the single permitted dispatch -- the Description controller
        (rec/album_descriptions.py) supplies it to re-verify consent/credential/
        budget and re-authorize its durable reservation at exactly this point,
        the same timing the admission orchestration has always used, now an
        explicit seam instead of this IAO reaching into Store itself.
        `reauthorize` raises to abort; this method never retries on its own.
        """
        check_cancellable_work()
        self.pacer.wait()
        check_cancellable_work()
        reauthorize()
        return self._request(payload, key, now_fn=now_fn)
