# tests/test_acousticbrainz.py
from copy import deepcopy

import pytest

from yt_playlist.providers import acousticbrainz as ab


HIGHLEVEL = {"highlevel": {
    "danceability": {"all": {"danceable": 0.92, "not_danceable": 0.08}},
    "mood_party": {"all": {"party": 0.87, "not_party": 0.13}},
    "mood_aggressive": {"all": {"aggressive": 0.30, "not_aggressive": 0.70}},
    "mood_happy": {"all": {"happy": 0.70, "not_happy": 0.30}},
    "mood_sad": {"all": {"sad": 0.20, "not_sad": 0.80}},
    "mood_relaxed": {"all": {"relaxed": 0.60, "not_relaxed": 0.40}},
    "mood_acoustic": {"all": {"acoustic": 0.30, "not_acoustic": 0.70}},
    "voice_instrumental": {"all": {"instrumental": 0.10, "voice": 0.90}},
}}
LOWLEVEL = {"rhythm": {"bpm": 128.0}, "tonal": {"key_key": "A", "key_scale": "minor"},
            "lowlevel": {"average_loudness": 0.9, "dynamic_complexity": 3.2}}


def test_derive_energy_blends_mood_models():
    # 0.5*0.87 + 0.3*0.30 + 0.2*0.92 = 0.435 + 0.09 + 0.184 = 0.709
    assert ab.derive_energy(HIGHLEVEL["highlevel"]) == 0.709


def test_enrich_returns_feature_dict(monkeypatch):
    def fake_get_json(url):
        if url.endswith("/high-level"):
            return HIGHLEVEL
        if url.endswith("/low-level"):
            return LOWLEVEL
        raise AssertionError(url)
    monkeypatch.setattr(ab, "_get_json", fake_get_json)
    feat = ab.enrich("mbid-1")
    assert feat["bpm"] == 128.0
    assert feat["energy"] == 0.709
    assert feat["danceability"] == 0.92
    assert feat["music_key"] == "A"
    assert feat["music_scale"] == "minor"
    assert feat["mood_happy"] == 0.70
    assert feat["mood_sad"] == 0.20
    assert feat["mood_relaxed"] == 0.60
    assert feat["mood_acoustic"] == 0.30
    assert feat["instrumental"] == 0.10
    assert feat["loudness"] == 0.9
    assert feat["dynamic_complexity"] == 3.2
    assert feat['mood_party'] == 0.87
    assert feat['mood_aggressive'] == 0.30


def test_enrich_none_when_no_data(monkeypatch):
    import urllib.error

    def fake_get_json(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(ab, "_get_json", fake_get_json)
    assert ab.enrich("mbid-unknown") == ab._empty()
    assert not ab._breaker.tripped()   # a 404 is "reachable", must not trip the breaker


def test_waterfall_resolves_mbid_then_fills_and_records_evidence(store, monkeypatch):
    from yt_playlist.providers import musicbrainz
    from yt_playlist.providers.waterfall import run_waterfall
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t1 = store.tracks.upsert_track("v1", "Song", "Artist", "Alb", 200)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1000.0)
    store.playlists.set_playlist_tracks(pid, [t1])
    # Resolve a missing recording MBID, then retain both projection and source evidence.
    high = {'highlevel': {'mood_party': {'all': {'party': 0.7}},
                          'mood_aggressive': {'all': {'aggressive': 0.3}},
                          'danceability': {'all': {'danceable': 0.8}}}}
    monkeypatch.setattr(ab, '_get_json', lambda url: high if url.endswith('/high-level')
                        else {'rhythm': {'bpm': 130}})
    events = []
    monkeypatch.setattr(musicbrainz, "recording_mbid", lambda *args: "mbid-live")
    run_waterfall(store, store.tracks.tracks_for_waterfall(pid),
                  [{"name": "acousticbrainz", "enabled": True}], events.append)
    assert store.tracks.get_track_audio(t1) == (130.0, 0.6, 0.8)
    rows = store.tracks.tracks_missing_audio(pid)
    assert rows == []  # fully populated now
    assert any(e.get("type") == "done" for e in events)
    attempt = store.enrichment_state.enrichment_attempts("song|artist")["acousticbrainz"]
    assert attempt["status"] == "found" and attempt["fields"]["mb_recording_id"] == "mbid-live"
    observations = store.enrichment_state.metadata_observations('song|artist')
    assert {row['field'] for row in observations} == {
        'bpm', 'mood_party', 'mood_aggressive', 'danceability', 'energy'}
    assert all(row['provenance']['match_evidence'] == {
        'method': 'musicbrainz_lookup', 'recording_mbid': 'mbid-live'} for row in observations)


def test_probe_retains_native_units_versions_and_correlated_derivation(monkeypatch):
    high, low = deepcopy(HIGHLEVEL), deepcopy(LOWLEVEL)
    high['metadata'] = {'version': {'essentia': '2.1', 'extractor': 'high-v1'}}
    low['metadata'] = {'version': {'essentia': '2.0', 'extractor': 'low-v2'}}
    high['highlevel']['mood_party']['version'] = {'models_git_sha': 'party-model'}
    monkeypatch.setattr(ab, '_get_json', lambda url: high if url.endswith('/high-level') else low)

    def forbidden_lookup(*args):
        raise AssertionError('Stored recording MBID should be reused')

    result = ab.probe({'mb_recording_id': 'mbid-stored'}, mbid_fn=forbidden_lookup)
    observations = {o.field: o.payload() for o in result.observations}
    assert 'mood_party' not in result.fields and 'mood_aggressive' not in result.fields
    assert observations['mood_party'] == {
        'schema': 1, 'field': 'mood_party', 'value': 0.87, 'scope': 'recording',
        'entity_id': 'mbid-stored', 'kind': 'prediction', 'unit': 'classifier_probability',
        'source_path': 'high-level:highlevel.mood_party.all.party',
        'provider_version': high['metadata']['version'], 'model_version': {'models_git_sha': 'party-model'},
        'derivation_version': None, 'dependencies': [],
        'match_evidence': {'method': 'supplied_recording_mbid', 'recording_mbid': 'mbid-stored'}}
    assert observations['loudness']['value'] == 0.9
    assert observations['loudness']['unit'] == 'essentia.average_loudness'
    assert observations['dynamic_complexity']['unit'] == 'essentia.dynamic_complexity'
    assert observations['loudness']['provider_version'] == low['metadata']['version']
    assert observations['loudness']['model_version'] is None
    assert observations['energy']['kind'] == 'derived'
    assert observations['energy']['derivation_version'] == 'acousticbrainz-energy-v1'
    assert observations['energy']['dependencies'] == ['mood_party', 'mood_aggressive', 'danceability']
    assert all('confidence' not in o and 'reliability' not in o for o in observations.values())


@pytest.mark.parametrize('missing', ['mood_party', 'mood_aggressive', 'danceability'])
def test_missing_component_is_unknown_instead_of_zero(missing):
    high = deepcopy(HIGHLEVEL['highlevel'])
    del high[missing]
    assert ab.derive_energy(high) is None
    for model in HIGHLEVEL['highlevel']:
        high[model] = {'all': {label: 0 for label in HIGHLEVEL['highlevel'][model]['all']}}
    assert ab.derive_energy(high) == 0


@pytest.mark.parametrize('invalid', [True, float('nan'), float('inf'), -0.1, 1.1, 'bad', None])
def test_invalid_classifier_values_are_unknown(invalid):
    assert ab._prob({'mood_party': {'all': {'party': invalid}}}, 'mood_party', 'party') is None


@pytest.mark.parametrize('high', [None, [], {'highlevel': None}, {'highlevel': {'mood_party': None}}])
def test_malformed_high_level_does_not_lose_valid_low_level(monkeypatch, high):
    monkeypatch.setattr(ab, '_get_json', lambda url: high if url.endswith('/high-level') else LOWLEVEL)
    result = ab.probe({'mb_recording_id': 'mbid'})
    assert result.fields['bpm'] == 128
    assert 'energy' not in result.fields
    assert {o.field for o in result.observations} == {
        'bpm', 'music_key', 'music_scale', 'loudness', 'dynamic_complexity'}


def test_malformed_low_level_does_not_lose_valid_predictions(monkeypatch):
    low = {'rhythm': None, 'tonal': {'key_key': ['A']}, 'metadata': None, 'lowlevel': []}
    monkeypatch.setattr(ab, '_get_json', lambda url: HIGHLEVEL if url.endswith('/high-level') else low)
    result = ab.probe({'mb_recording_id': 'mbid'})
    assert result.fields['energy'] == 0.709
    assert 'bpm' not in result.fields and 'music_key' not in result.fields


def test_no_recording_match_does_not_invent_observations():
    result = ab.probe({'title': 'Unknown', 'artist': 'Unknown'}, mbid_fn=lambda *args: None)
    assert result.fields == {} and result.observations == ()


@pytest.mark.parametrize('membership', ['library', 'discovery'])
def test_partial_failure_keeps_previous_evidence_and_retries_for_both_catalogs(store, monkeypatch, membership):
    from yt_playlist.providers.waterfall import run_waterfall

    if membership == 'library':
        tid = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    else:
        store.discovery.upsert_discovered_track('song|artist', 'video', 'Song', 'Artist', '',
                                                '', None, None, None, 10)
        tid = store.conn.execute("SELECT id FROM catalog_entries WHERE video_id='video'").fetchone()[0]
    monkeypatch.setattr(ab.musicbrainz, 'recording_mbid', lambda *args: 'mbid')
    phase = ['healthy']

    def get(url):
        if url.endswith('/high-level'):
            if phase[0] == 'broken':
                error = ValueError('invalid high-level JSON')
                ab.net.record_probe_failure(error)
                raise error
            return HIGHLEVEL
        return {'rhythm': {'bpm': 140}} if phase[0] == 'broken' else LOWLEVEL

    monkeypatch.setattr(ab, '_get_json', get)

    def run(run_id, now):
        return run_waterfall(store, [store.tracks.track_for_waterfall(tid)],
                             [{'name': 'acousticbrainz', 'enabled': True}], lambda e: None,
                             run_id=run_id, now_fn=lambda: now)

    assert run('healthy', 1000) == [tid]
    before = store.enrichment_state.metadata_observations('song|artist')
    phase[0] = 'broken'
    assert run('partial', 2000) == []
    after = store.enrichment_state.metadata_observations('song|artist')
    assert after[:len(before)] == before
    assert store.tracks.get_track_audio(tid) == (140, 0.709, 0.92)
    state = store.enrichment_state.enrichment_provider_state(
        store.tracks.catalog_subject(tid)['recording_id'])['acousticbrainz']
    assert state['status'] == 'retry' and state['retry_at'] > 2000
    assert {row['field'] for row in after[len(before):]} == {'bpm'}
    phase[0] = 'healthy'
    assert run('recovery', 10000) == [tid]
    assert store.tracks.get_track_audio(tid) == (128, 0.709, 0.92)


def test_malformed_endpoint_is_retryable_without_losing_successful_half(store, monkeypatch):
    from yt_playlist.providers.waterfall import run_waterfall

    tid = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    monkeypatch.setattr(ab.musicbrainz, 'recording_mbid', lambda *args: 'mbid')
    monkeypatch.setattr(ab, '_get_json', lambda url: [] if url.endswith('/high-level') else LOWLEVEL)
    assert run_waterfall(store, [store.tracks.track_for_waterfall(tid)],
                         [{'name': 'acousticbrainz', 'enabled': True}], lambda e: None) == []
    assert store.tracks.get_track_audio(tid) == (128, None, None)
    assert len(store.enrichment_state.metadata_observations('song|artist')) == 5
    assert store.enrichment_state.enrichment_attempts('song|artist')['acousticbrainz']['status'] == 'retry'
