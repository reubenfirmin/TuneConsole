"""Bounded, cached HTTP for explicitly enabled offline metadata pilots."""
import json
import math
import re
import time
import unicodedata

from yt_playlist.rec.musical_profiles import digest


def literal(value):
    # Preserve Live/Remix/etc. when comparing source recordings.
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold()))


def plausible_match(expected, candidate):
    """Conservative automatic filter; human version verification remains pending."""
    if literal(candidate.get('trackTitle') or '') != literal(expected['title'] or ''):
        return False
    if literal(expected['artist'] or '') not in {literal(artist.get('name') or '')
                                                   for artist in candidate.get('artists', [])}:
        return False
    duration, returned = expected.get('duration_s'), candidate.get('durationMs')
    return (type(duration) in (int, float) and math.isfinite(duration) and duration > 0
            and type(returned) in (int, float) and math.isfinite(returned)
            and abs(duration * 1000 - returned) <= max(3000, .02 * duration * 1000))


def select_sample(rows, *, require_duration=False):
    """Keep a duration-known feasibility sample separate from catalog sampling."""
    rows = sorted(rows, key=lambda row: digest([row['key'], 'provider-pilot-1']))
    chosen, used = [], set()
    for stratum, predicate in (
            ('slow_high_legacy_energy', lambda row: row['bpm'] is not None and row['bpm'] < 90 and (row['energy'] or 0) > .7),
            ('fast_low_legacy_energy', lambda row: row['bpm'] is not None and row['bpm'] > 140 and row['energy'] is not None and row['energy'] < .35),
            ('missing_legacy_audio', lambda row: row['bpm'] is None and row['energy'] is None),
            ('legacy_audio_present', lambda row: row['bpm'] is not None and row['energy'] is not None)):
        genres, count = set(), 0
        for row in rows:
            duration = row.get('duration_s')
            if require_duration and not (type(duration) in (int, float) and math.isfinite(duration) and duration > 0):
                continue
            if (row['key'] in used or row['genre'] in genres or not predicate(row)
                    or len(row['title'] or '') < 3 or len(row['artist'] or '') < 3):
                continue
            chosen.append({**row, 'stratum': stratum})
            used.add(row['key'])
            genres.add(row['genre'])
            count += 1
            if count == 2:
                break
    return chosen


def shortlist(expected, candidate):
    """Title/credit shortlist only: documented responses lack recording durations."""
    artist = candidate.get('artist') or {}
    artists = artist if isinstance(artist, list) else [artist]
    return (bool(literal(expected.get('title') or ''))
            and literal(expected.get('title') or '') == literal(candidate.get('title') or '')
            and bool(literal(expected.get('artist') or ''))
            and literal(expected['artist']) in {literal(artist.get('name') or '') for artist in artists})


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
