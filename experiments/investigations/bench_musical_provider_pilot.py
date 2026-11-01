"""Explicit, bounded ReccoBeats metadata pilot; never updates the live catalog.

Enable TUNECONSOLE_PROVIDER_PILOT and run:
./experiments/run.py investigate bench_musical_provider_pilot.py -s
Up to eight deterministic, stratified recordings; at most sixteen metadata requests.
No audio endpoints, downloads, accounts, credentials or production ingestion.
TUNECONSOLE_PROVIDER_PILOT_REQUIRE_DURATION=1 selects a separate duration-known
sample for matching feasibility; its hit rate is not whole-catalog coverage.
"""
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

import httpx
import pytest

from tests.bench_svd_background import _saved_library_source
from tests.musical_provider_support import PilotHTTP, plausible_match, select_sample

BASE = 'https://api.reccobeats.com/v1'


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
