"""Explicit, bounded ReccoBeats metadata pilot; never updates the live catalog.

TUNECONSOLE_PROVIDER_PILOT=1 uv run pytest -s tests/bench_musical_provider_pilot.py
Up to eight deterministic, stratified recordings; at most sixteen metadata requests.
No audio endpoints, downloads, accounts, credentials or production ingestion.
TUNECONSOLE_PROVIDER_PILOT_REQUIRE_DURATION=1 selects a separate duration-known
sample for matching feasibility; its hit rate is not whole-catalog coverage.
"""
from collections import Counter
from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unicodedata

import httpx
import pytest

from tests.bench_svd_background import _saved_library_source
from tests.musical_provider_support import PilotHTTP
from yt_playlist.rec import musical_profiles

BASE = 'https://api.reccobeats.com/v1'


def _literal(value):
    # General catalog normalization intentionally strips some version suffixes;
    # the pilot must preserve Live/Remix/etc. when comparing source recordings.
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold()))


def plausible_match(expected, candidate):
    """Conservative automatic filter; human version verification remains pending."""
    if _literal(candidate.get('trackTitle') or '') != _literal(expected['title'] or ''):
        return False
    if _literal(expected['artist'] or '') not in {_literal(artist.get('name') or '')
                                                   for artist in candidate.get('artists', [])}:
        return False
    duration, returned = expected.get('duration_s'), candidate.get('durationMs')
    return (type(duration) in (int, float) and math.isfinite(duration) and duration > 0
            and type(returned) in (int, float) and math.isfinite(returned)
            and abs(duration * 1000 - returned) <= max(3000, .02 * duration * 1000))


def select_sample(rows, *, require_duration=False):
    """Keep a duration-known feasibility sample separate from catalog sampling."""
    rows = sorted(rows, key=lambda row: musical_profiles.digest([row['key'], 'provider-pilot-1']))
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


def _sample(require_duration=False):
    with closing(sqlite3.connect(_saved_library_source().resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        rows = [dict(row) for row in conn.execute('SELECT identity_key AS key,title,artist,album,mb_year AS year,'
            'mb_recording_id,genre,video_id,'
            'duration_s,bpm,energy,danceability,instrumental FROM catalog_tracks '
            'WHERE available IS NOT 0 AND video_id IS NOT NULL')]
    return select_sample(rows, require_duration=require_duration)


def test_bounded_reccobeats_pilot():
    if os.environ.get('TUNECONSOLE_PROVIDER_PILOT') != '1':
        pytest.skip('Set TUNECONSOLE_PROVIDER_PILOT=1 for explicit external metadata requests')
    output = Path(tempfile.mkdtemp(prefix='tuneconsole-provider-pilot-'))
    report = {'schema': 1, 'at': time.time(), 'provider': 'reccobeats', 'api_spec_version': '1.0.0',
              'provider_model_version': None, 'manual_match_verification': None, 'listening_judgments': 0,
              'status': 'pilot_only_not_adopted', 'items': [], 'terms': [
                  'https://reccobeats.com/docs/documentation/terms-of-service',
                  'https://reccobeats.com/docs/documentation/rate-limiting'],
              'getsongbpm': 'Not piloted: requires an API key and registered application attribution.'}
    def save(name, value):
        path = output / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
    with httpx.Client(timeout=10, headers={'User-Agent': 'TuneConsole-metadata-pilot/1 (github.com/reubenfirmin/TuneConsole)'}) as client:
        pilot = PilotHTTP(client, BASE, output)
        get, requests = pilot.get, pilot.requests
        try:
            timed = os.environ.get('TUNECONSOLE_PROVIDER_PILOT_REQUIRE_DURATION') == '1'
            sample = _sample(require_duration=timed)
            report['sample_kind'] = 'duration_known_matching_feasibility' if timed else 'stratified_catalog'
            report['whole_catalog_coverage_estimate'] = None
            save('sample.json', sample)
            report['sample_strata'] = dict(Counter(row['stratum'] for row in sample))
            for row in sample:
                query = {'searchText': row['title'], 'artist': row['artist'], 'size': 5}
                result = get('/track/search', query)
                # Re-reading a cached query must add no remote request.
                count = len(requests)
                assert get('/track/search', query) == result and len(requests) == count
                result = result or {}
                matches = [candidate for candidate in result.get('content', []) if plausible_match(row, candidate)]
                item = {'key': row['key'], 'stratum': row['stratum'], 'candidates': len(result.get('content', [])),
                        'plausible_matches': len(matches), 'verified_match': None, 'features': None}
                if len(matches) == 1:
                    item['candidate'] = matches[0]
                    item['features'] = get('/track/' + matches[0]['id'] + '/audio-features')
                report['items'].append(item)
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            report['status'] = 'incomplete_provider_error'
            report['error'] = type(error).__name__ + ': ' + str(error)
        finally:
            report.update(requests=requests, cache_hits=pilot.cache_hits,
                          request_count=len(requests), response_bytes=sum(row['bytes'] for row in requests))
            save('report.json', report)
            print(json.dumps({'report': str(output / 'report.json'), 'status': report['status'],
                'request_count': len(requests), 'cache_hits': pilot.cache_hits,
                'sample_strata': report.get('sample_strata'),
                'plausible_single_matches': sum(row['plausible_matches'] == 1 for row in report['items'])}), flush=True)
    assert len(requests) <= 16
