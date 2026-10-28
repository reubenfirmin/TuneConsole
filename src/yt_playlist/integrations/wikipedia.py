"""Wikipedia search, summaries and Wikimedia file metadata through one access owner."""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.integrations.base import Availability, IntegrationUnavailable, RateLimiter
from yt_playlist.util import net

SEARCH = 'https://en.wikipedia.org/w/api.php'
SUMMARY = 'https://en.wikipedia.org/api/rest_v1/page/summary/'
USER_AGENT = 'yt-playlist/0.1 ( https://4rc.io ; rf@4rc.io )'
TIMEOUT_S = 8
MIN_INTERVAL = .2
CACHE_SIZE = 512
POSITIVE_TTL = 86400
MISS_TTL = 3600
_LOG = logging.getLogger(__name__)


class _ApiFailure(Exception):
    def __init__(self, errors, headers):
        codes = [entry.get('code', '') for entry in errors if isinstance(entry, dict)]
        self.outage = any(code in {'maxlag', 'ratelimited', 'readonly', 'timeout'}
                          or code.startswith('internal_api_error_')
                          for code in codes if isinstance(code, str))
        self.headers = headers
        super().__init__('Wikipedia API error')


class WikipediaIAO:
    def __init__(self, *, now_fn=time.time, monotonic_fn=time.monotonic,
                 open_fn=None, pacer=None):
        self.now, self.monotonic = now_fn, monotonic_fn
        self.open = open_fn
        self.pacer = pacer or RateLimiter(MIN_INTERVAL)
        self._lock = threading.RLock()
        self._scope = ContextVar('wikipedia_access_scope', default=None)
        self._availability, self._failures = Availability(), 0
        self._cache, self._waits = OrderedDict(), OrderedDict()

    @contextmanager
    def context(self, store=None):
        token = self._scope.set(store) if store is not None else None
        try:
            yield self
        finally:
            if token is not None:
                self._scope.reset(token)

    def _state(self):
        return getattr(self._scope.get(), 'enrichment_state', None)

    def availability(self):
        with self._lock:
            state = self._state()
            saved = state.integration_cooldown('wikipedia') if state else None
            if saved and saved['retry_at'] > self._availability.retry_at:
                return Availability(saved['retry_at'], saved['error'])
            return self._availability

    @staticmethod
    def _resource(url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'en.wikipedia.org' or parsed.fragment
                or len(url) > 8192):
            raise ValueError('Unsupported Wikipedia resource')
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        params = dict(pairs)
        if len(params) != len(pairs):
            raise ValueError('Repeated Wikipedia parameters')
        if parsed.path == '/w/api.php' and params.get('action') == 'query' and params.get('format') == 'json':
            if (set(params) == {'action', 'format', 'list', 'srlimit', 'srsearch'}
                    and params['list'] == 'search' and params['srlimit'] in {'1', '2', '3', '4', '5'}
                    and 0 < len(params['srsearch']) <= 1024):
                key = ('search', params['srsearch'], params['srlimit'])
            elif (set(params) == {'action', 'format', 'prop', 'titles', 'iiprop', 'iiurlwidth'}
                    and params['prop'] == 'imageinfo' and params['iiprop'] == 'url|extmetadata'
                    and params['iiurlwidth'] == '1200' and params['titles'].startswith('File:')
                    and 5 < len(params['titles']) <= 2048):
                key = ('imageinfo', params['titles'])
            else:
                raise ValueError('Unsupported Wikipedia query')
            return key, SEARCH + '?' + urllib.parse.urlencode(sorted(params.items()))
        if parsed.path.startswith('/api/rest_v1/page/summary/') and params == {'redirect': 'true'}:
            title = urllib.parse.unquote(parsed.path.removeprefix('/api/rest_v1/page/summary/'))
            if (not title or len(title) > 1024 or title in {'.', '..'}
                    or any(ord(char) < 32 for char in title)):
                raise ValueError('Unsupported Wikipedia title')
            return ('summary', title), SUMMARY + urllib.parse.quote(title, safe='') + '?redirect=true'
        raise ValueError('Unsupported Wikipedia resource')

    @staticmethod
    def _validate(kind, data):
        if not isinstance(data, dict):
            raise ValueError('Wikipedia response is not an object')
        if kind == 'summary':
            if data.get('type') not in {'standard', 'disambiguation', 'no-extract'}:
                raise ValueError('Wikipedia summary type is invalid')
            if data['type'] == 'standard' and 'extract' not in data and 'originalimage' not in data:
                raise ValueError('Wikipedia standard summary has no content')
            for field in ('title', 'extract', 'description'):
                if field in data and not isinstance(data[field], str):
                    raise ValueError('Wikipedia summary text is invalid')
            for field in ('thumbnail', 'originalimage'):
                if field in data and (not isinstance(data[field], dict)
                        or not isinstance(data[field].get('source'), str) or not data[field]['source'].strip()):
                    raise ValueError('Wikipedia image reference is invalid')
            if 'content_urls' in data:
                urls = data['content_urls']
                if (not isinstance(urls, dict) or not isinstance(urls.get('desktop'), dict)
                        or not isinstance(urls['desktop'].get('page'), str)):
                    raise ValueError('Wikipedia article URL is invalid')
            return data['type'] == 'standard' and bool(data.get('extract', '').strip()
                                                       or data.get('originalimage'))
        query = data.get('query')
        if not isinstance(query, dict):
            raise ValueError('Wikipedia query response is invalid')
        if kind == 'search':
            hits = query.get('search')
            if (not isinstance(hits, list) or any(not isinstance(hit, dict)
                    or not isinstance(hit.get('title'), str) or not hit['title'].strip() for hit in hits)):
                raise ValueError('Wikipedia search results are invalid')
            return bool(hits)
        pages = query.get('pages')
        if not isinstance(pages, dict) or not pages:
            raise ValueError('Wikipedia file pages are invalid')
        useful = False
        for page in pages.values():
            if not isinstance(page, dict):
                raise ValueError('Wikipedia file page is invalid')
            if 'missing' in page and 'imageinfo' not in page:
                if not isinstance(page.get('title'), str):
                    raise ValueError('Wikipedia missing file has no identity')
                continue
            infos = page.get('imageinfo')
            if not isinstance(infos, list):
                raise ValueError('Wikipedia file information is invalid')
            for info in infos:
                if not isinstance(info, dict):
                    raise ValueError('Wikipedia file information is invalid')
                for field in ('url', 'thumburl', 'descriptionurl'):
                    if field in info and not isinstance(info[field], str):
                        raise ValueError('Wikipedia file URL is invalid')
                meta = info.get('extmetadata', {})
                if not isinstance(meta, dict):
                    raise ValueError('Wikipedia file attribution is invalid')
                for field in ('Artist', 'LicenseShortName', 'NonFree'):
                    if field in meta and (not isinstance(meta[field], dict)
                            or not isinstance(meta[field].get('value'), str)):
                        raise ValueError('Wikipedia file attribution value is invalid')
                useful = True
        return useful

    def _error(self, wait, outage):
        error = IntegrationUnavailable('wikipedia', wait.error, wait.retry_at, outage=outage)
        net.record_probe_failure(error)
        return error

    def get_url(self, url):
        key, url = self._resource(url)
        check_cancellable_work()
        with self._lock:
            check_cancellable_work()
            cached = self._cache.get(key)
            if cached and self.monotonic() < cached[1]:
                self._cache.move_to_end(key)
                return deepcopy(cached[0])
            admitted = self.availability()
            wait, outage = max(((admitted, True), self._waits.get(key, (Availability(), False))),
                               key=lambda value: value[0].retry_at)
            if wait.ready(self.now()):
                self.pacer.wait()
                check_cancellable_work()
                admitted = self.availability()
                if admitted.retry_at > wait.retry_at:
                    wait, outage = admitted, True
            if not wait.ready(self.now()):
                error = self._error(wait, outage)
                if cached and cached[2]:
                    self._cache.move_to_end(key)
                    return deepcopy(cached[0])
                raise error
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            started = self.monotonic()
            try:
                with (self.open or urllib.request.urlopen)(request, timeout=TIMEOUT_S) as response:
                    data = json.load(response)
                    if isinstance(data, dict) and ('error' in data or 'errors' in data):
                        errors = data.get('errors') or [data.get('error')]
                        raise _ApiFailure(errors if isinstance(errors, list) else [], getattr(response, 'headers', {}))
                check_cancellable_work()
                useful = self._validate(key[0], data)
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError) and key[0] == 'summary' and exc.code in (404, 410):
                    exc.close()
                    data, useful = {'type': 'absent'}, False
                else:
                    error = self._failed(exc, key)
                    if isinstance(exc, urllib.error.HTTPError):
                        exc.close()
                    _LOG.info('Wikipedia endpoint=%s outcome=retry retry_at=%.3f elapsed=%.3f',
                              key[0], error.retry_at, self.monotonic() - started)
                    if cached and cached[2]:
                        self._cache.move_to_end(key)
                        return deepcopy(cached[0])
                    raise error from exc
            self._failures, self._availability = 0, Availability()
            state = self._state()
            if state:
                state.clear_integration_cooldown('wikipedia', admitted.retry_at)
            self._waits.pop(key, None)
            self._cache[key] = (deepcopy(data), self.monotonic() +
                                (POSITIVE_TTL if useful else MISS_TTL), useful)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            return data

    def _failed(self, exc, key):
        now = self.now()
        outage = net.provider_outage(exc) or isinstance(exc, _ApiFailure) and exc.outage
        message = (f'Wikipedia HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError)
                   else f'Wikipedia {type(exc).__name__}')
        retry_at = max(now + 30, net.retry_after(exc, now))
        if outage:
            self._failures += 1
            retry_at = max(retry_at, now + min(3600, 30 * 2 ** min(self._failures - 1, 7)))
            state = self._state()
            if state:
                retry_at = state.defer_integration('wikipedia', now=now,
                    retry_after=retry_at, error=message)['retry_at']
            self._availability = Availability(retry_at, message)
        wait = Availability(retry_at, message)
        self._waits[key] = (wait, outage)
        self._waits.move_to_end(key)
        while len(self._waits) > CACHE_SIZE:
            self._waits.popitem(last=False)
        return self._error(wait, outage)
