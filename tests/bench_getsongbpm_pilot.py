"""Opt-in GetSongBPM shortlist for manual recording review; no ingestion.

Requires TUNECONSOLE_GETSONGBPM_PILOT=1, GETSONGBPM_API_KEY from a registered
application, and TUNECONSOLE_GETSONGBPM_ATTRIBUTION with its existing backlink URL.
No registration, credentials, audio or live catalog data are modified by this pilot.
"""
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import time

import httpx
import pytest

from tests.bench_musical_provider_pilot import _literal, _sample
from tests.musical_provider_support import PilotHTTP


def shortlist(expected, candidate):
    """Title/credit shortlist only: documented responses lack recording durations."""
    artist = candidate.get('artist') or {}
    artists = artist if isinstance(artist, list) else [artist]
    return (bool(_literal(expected.get('title') or ''))
            and _literal(expected.get('title') or '') == _literal(candidate.get('title') or '')
            and bool(_literal(expected.get('artist') or ''))
            and _literal(expected['artist']) in {_literal(artist.get('name') or '') for artist in artists})


def test_bounded_getsongbpm_pilot():
    key = os.environ.get('GETSONGBPM_API_KEY')
    attribution = os.environ.get('TUNECONSOLE_GETSONGBPM_ATTRIBUTION')
    if os.environ.get('TUNECONSOLE_GETSONGBPM_PILOT') != '1' or not key or not attribution:
        pytest.skip('Enable the pilot with a registered GetSongBPM key and existing attribution URL')
    output = Path(tempfile.mkdtemp(prefix='tuneconsole-getsongbpm-pilot-'))
    report = {'schema': 1, 'at': time.time(), 'provider': 'getsongbpm', 'api_spec_version': '1.3',
        'provider_model_version': None, 'manual_match_verification': None, 'listening_judgments': 0,
        'status': 'manual_recording_review_required_not_adopted', 'attribution': attribution,
        'documentation': 'https://getsongbpm.com/api', 'whole_catalog_coverage_estimate': None,
        'matching_limit': 'Documented song/search fields do not provide duration or a recording ID usable by our automatic match filter.',
        'correlation': 'Documented danceability/acousticness originate from AcousticBrainz; do not count them as independent corroboration.',
        'native_units': {'tempo': 'BPM', 'danceability': '0–100', 'acousticness': '0–100'},
        'items': []}
    def save(name, value):
        path = output / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
    with httpx.Client(timeout=10, headers={'X-API-KEY': key,
            'User-Agent': 'TuneConsole-metadata-pilot/1 (github.com/reubenfirmin/TuneConsole)'}) as client:
        pilot = PilotHTTP(client, 'https://api.getsong.co', output, max_requests=8, interval=1.25)
        try:
            sample = _sample(require_duration=True)
            save('sample.json', sample)
            report['sample_strata'] = dict(Counter(row['stratum'] for row in sample))
            for row in sample:
                params = {'type': 'both', 'lookup': f"song:{row['title']} artist:{row['artist']}", 'limit': 5}
                payload = pilot.get('/search/', params)
                assert pilot.get('/search/', params) == payload
                payload = payload or {}
                if payload.get('error'):
                    raise RuntimeError('Provider reported an API error')
                candidates = payload.get('search', [])
                if not isinstance(candidates, list):
                    raise RuntimeError('Provider returned an unexpected search result')
                report['items'].append({'key': row['key'], 'stratum': row['stratum'],
                    'returned': len(candidates), 'verified_match': None,
                    'shortlist': [{**candidate, 'recording_verified': False} for candidate in candidates
                                  if shortlist(row, candidate)]})
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            report.update(status='incomplete_provider_error', error=type(error).__name__)
        finally:
            report.update(requests=pilot.requests, cache_hits=pilot.cache_hits,
                          request_count=len(pilot.requests),
                          response_bytes=sum(row['bytes'] for row in pilot.requests))
            save('report.json', report)
            print(json.dumps({'report': str(output / 'report.json'), 'status': report['status'],
                              'request_count': len(pilot.requests)}), flush=True)
    assert len(pilot.requests) <= 8
