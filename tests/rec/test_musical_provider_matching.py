"""A provider name hit alone cannot establish recording/version agreement."""
import httpx
import pytest

from tests.bench_getsongbpm_pilot import shortlist
from tests.bench_musical_provider_pilot import plausible_match, select_sample
from tests.musical_provider_support import PilotHTTP


def test_recording_match_requires_title_artist_and_duration_without_dropping_version_words():
    expected = {'title': 'Song (Live)', 'artist': 'Artist', 'duration_s': 200}
    result = {'trackTitle': 'Song (Live)', 'artists': [{'name': 'Artist'}], 'durationMs': 201000}
    assert plausible_match(expected, result)
    for changes in ({'trackTitle': 'Song'}, {'artists': [{'name': 'Different Artist'}]},
                    {'durationMs': 210000}, {'durationMs': None}, {'durationMs': True}):
        assert not plausible_match(expected, {**result, **changes})
    assert not plausible_match({**expected, 'duration_s': None}, result)


def test_duration_known_feasibility_sample_preserves_strata_and_does_not_duplicate_recordings():
    rows = []
    for label, bpm, energy in (('slow', 80, .9), ('fast', 160, .1), ('missing', None, None), ('present', 110, .5)):
        for i, duration in enumerate((None, 180, 200)):
            rows.append({'key': f'{label}-{i}', 'title': f'Song {i}', 'artist': 'Artist',
                         'genre': f'Genre {i}', 'duration_s': duration, 'bpm': bpm, 'energy': energy})
    sample = select_sample(rows, require_duration=True)
    assert len(sample) == len({row['key'] for row in sample}) == 8
    assert all(row['duration_s'] in (180, 200) for row in sample)
    assert {row['stratum'] for row in sample} == {
        'slow_high_legacy_energy', 'fast_low_legacy_energy', 'missing_legacy_audio', 'legacy_audio_present'}


def test_getsong_shortlist_keeps_version_words_and_never_establishes_recording_identity():
    expected = {'title': 'Song (Live)', 'artist': 'Artist'}
    result = {'title': 'Song (Live)', 'artist': {'name': 'Artist'}, 'tempo': '140'}
    assert shortlist(expected, result)
    assert not shortlist(expected, {**result, 'title': 'Song'})
    assert not shortlist(expected, {**result, 'artist': {'name': 'Someone else'}})


def test_pilot_http_caches_without_repeating_requests_or_retaining_credentials(tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'search': []})
    with httpx.Client(transport=httpx.MockTransport(respond), headers={'X-API-KEY': 'private-test-key'}) as client:
        pilot = PilotHTTP(client, 'https://example.test', tmp_path, interval=0, max_requests=1)
        assert pilot.get('/search/', {'lookup': 'Song'}) == {'search': []}
        assert pilot.get('/search/', {'lookup': 'Song'}) == {'search': []}
        assert len(calls) == pilot.cache_hits == 1
        assert calls[0].headers['X-API-KEY'] == 'private-test-key'
        assert 'private-test-key' not in str(calls[0].url)
        assert 'private-test-key' not in str(pilot.requests)
        assert all('private-test-key' not in path.read_text() for path in tmp_path.iterdir())
        with pytest.raises(RuntimeError, match='budget exhausted'):
            pilot.get('/another/')


def test_pilot_http_stops_on_rate_limit_without_retrying(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(429, headers={'Retry-After': '60'}))) as client:
        pilot = PilotHTTP(client, 'https://example.test', tmp_path, interval=0)
        with pytest.raises(RuntimeError, match='rate-limited'):
            pilot.get('/search/')
        assert len(pilot.requests) == 1 and pilot.requests[0]['retry_after'] == '60'
        assert not list(tmp_path.iterdir())
