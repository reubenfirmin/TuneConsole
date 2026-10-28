"""Bounded, cached HTTP for explicitly enabled offline metadata pilots."""
import json
import time

from yt_playlist.rec.musical_profiles import digest


class PilotHTTP:
    def __init__(self, client, base, output, *, max_requests=16, interval=1.):
        self.client, self.base, self.output = client, base, output
        self.max_requests, self.interval = max_requests, interval
        self.requests, self.cache_hits, self.last_request = [], 0, 0.

    def get(self, path, params=None):
        # Credentials belong in client headers, never URLs, filenames or reports.
        cache = self.output / (digest([path, params]) + '.json')
        if cache.exists():
            self.cache_hits += 1
            return json.loads(cache.read_text())
        if len(self.requests) >= self.max_requests:
            raise RuntimeError('Pilot request budget exhausted')
        time.sleep(max(0., self.interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        response = self.client.get(self.base + path, params=params)
        self.requests.append({'path': path, 'status': response.status_code,
            'wall_s': time.monotonic() - self.last_request, 'bytes': len(response.content),
            'retry_after': response.headers.get('Retry-After')})
        if response.status_code == 429:
            raise RuntimeError('Provider rate-limited the pilot; stop without retrying')
        if response.status_code == 404:
            payload = None
        else:
            response.raise_for_status()
            payload = response.json()
        cache.write_text(json.dumps(payload, ensure_ascii=False) + '\n')
        cache.chmod(0o600)
        return payload
