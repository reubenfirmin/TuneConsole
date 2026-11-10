"""Prevent candidate metadata from becoming verified provider coverage."""
import httpx
import pytest

from experiments.audio_lab_tools.studies.provider_pilot import CachedHTTP, decide, select


def pair(status='accepted'):
    expected = {'title': 'A Song (Live)', 'artist': 'Artist', 'duration_s': 200,
                'identifiers': [{'scheme': 'isrc', 'entity_type': 'recording', 'value': 'US-ABC-12-34567', 'status': status}]}
    candidate = {'trackTitle': 'A Song (Live)', 'artists': [{'name': 'Artist'}],
                 'durationMs': 200100, 'isrc': 'USABC1234567'}
    return expected, candidate


def test_candidate_identifier_is_not_accepted_identity():
    expected, candidate = pair('candidate')
    assert decide(expected, candidate) == ('ambiguous', 'no_shared_recording_identifier')
    expected['identifiers'][0]['status'] = 'accepted'
    assert decide(expected, candidate) == ('verified', 'isrc')


@pytest.mark.parametrize(('change', 'reason'), [
    ({'trackTitle': 'A Song'}, 'title_or_version_mismatch'),
    ({'artists': [{'name': 'Other'}]}, 'artist_mismatch'),
    ({'durationMs': 260000}, 'duration_mismatch'),
    ({'isrc': 'USOTHER12345'}, 'isrc_conflict'),
])
def test_identifier_does_not_overrule_conflicting_version_metadata(change, reason):
    expected, candidate = pair()
    assert decide(expected, {**candidate, **change}) == ('rejected', reason)


@pytest.mark.parametrize('duration', [None, 0, float('nan'), True])
def test_missing_or_invalid_duration_is_unknown(duration):
    expected, candidate = pair()
    expected['duration_s'] = duration
    assert decide(expected, candidate) == ('ambiguous', 'duration_unavailable')


def test_sampling_retains_empty_strata_and_deduplicates_recordings():
    rows = [{'key': str(i), 'recording_id': i, 'library': 1, 'discovery': 1, 'evidence_stratum': 'missing'}
            for i in range(5)]
    chosen, counts = select(rows)
    assert chosen == select(list(reversed(rows)))[0]
    assert len({r['recording_id'] for r in chosen}) == len(chosen) == 4
    assert counts['library:conflicting'] == 0 and counts['discovery:retained'] == 0


def test_cache_budget_and_rate_limit_preserve_exact_request_cost(tmp_path):
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={'content': []})
    with httpx.Client(transport=httpx.MockTransport(handle), headers={'X-API-KEY': 'secret'}) as client:
        api = CachedHTTP(client, 'https://example.org', tmp_path, 1)
        assert api.get('/search', {'q': 'song'}) == api.get('/search', {'q': 'song'})
        with pytest.raises(RuntimeError, match='budget'):
            api.get('/different')
    assert len(sent) == 1 and api.hits == 1
    assert 'secret' not in (tmp_path / 'requests.json').read_text()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={'Retry-After': '10'}))) as client:
        api = CachedHTTP(client, 'https://example.org', tmp_path / 'limited', 1)
        with pytest.raises(RuntimeError, match='rate_limited'):
            api.get('/search')
        assert api.requests[0]['retry_after'] == '10'
        assert not list((tmp_path / 'limited').glob('cache/*'))
