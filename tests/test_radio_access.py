"""Radio access keeps journey prerequisites without preparing later destinations."""
import pytest
from tests.derived_rating_support import seed_dislike
from tests.enrichment_support import complete_entries, complete_discovered

from tests.conftest import seed_catalog_track
from tests.onboarding_support import publish_live_mix_inputs
from tests.test_growth_journey import early_journey
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, mock_snapshot_coordinator, native_sql_calls)
from yt_playlist.rec import account_growth, home_prepared
from yt_playlist.core.model_state import RecordingModelUpdate


@pytest.fixture
def radio_access_store(tmp_path, monkeypatch, native_sql_calls):
    # Access admission reads real ranked inputs, independently of the full
    # metadata vocabulary and snapshot-conflict machinery.
    with bounded_consumer_schema((('house', 'House'), ('jazz', 'Jazz')), snapshot_guards=False) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=False) as database:
            with mock_snapshot_coordinator(database, monkeypatch):
                yield database


@pytest.mark.parametrize('case, expected', [
    ('empty', False), ('missing_foundation', False), ('missing_albums', False),
    ('missing_discover', False), ('missing_mixes', False), ('missing_seeds', False),
    ('incomplete_coverage', False), ('ready', True), ('mix_feedback', True), ('prior_radio', True),
    ('prior_tv', True), ('prior_vibes', True), ('excluded_seed', False),
])
def test_radio_access_matches_journey_and_preserves_earned_access(radio_access_store, monkeypatch, case, expected):
    store = radio_access_store
    radio = {'seeds': [], 'state': {}, 'seedMenu': 'current'}
    if case != 'empty':
        if not case.startswith('prior_'):
            reserve = early_journey(store)
            if case != 'missing_mixes':
                mixes = publish_live_mix_inputs(store, fresh_reserve=reserve, tracks_per_lane=5)
        for i in range(4):
            key, video = f'start {i}|artist', f'start-{i}'
            seed_catalog_track(store, key, f'Start {i}', 'Artist', video_id=video)
            radio['seeds'].append({'key': key, 'video_id': video})
        if case != 'incomplete_coverage' and case != 'prior_radio':
            complete_entries(store, [row[0] for row in store.conn.execute('SELECT id FROM catalog_tracks WHERE id IN (SELECT id FROM track_sources WHERE library_origin IS NOT NULL)')], 1)
            complete_discovered(store, [row[0] for row in store.conn.execute(
                'SELECT identity_key FROM discovered_tracks')], 1)
        if case == 'missing_foundation':
            store.rec_model.publish_model_state(recording=RecordingModelUpdate(generation='unprepared-replacement'))
        elif case == 'missing_albums':
            store.rec_proposals.put_proposals('album_prepared', {}, 1000)
        elif case == 'missing_discover':
            store.rec_proposals.put_proposals(home_prepared.DISCOVERY_SURFACE, {}, 1000)
        elif case == 'mix_feedback':
            # The live Mix Tapes can still use the other four eligible tracks.
            seed_dislike(store, mixes[0]['key'], until=None, now=1000)
        elif case == 'missing_seeds':
            radio['seeds'] = []
        elif case == 'prior_radio':
            store.rec_proposals.put_proposals('listening_choices', {
                'home_format': {'episodes': {'radio': 1}}}, 1000)
        elif case == 'prior_vibes':
            store.rec_proposals.put_proposals('listening_choices', {
                'home_format': {'vibe_episodes': 1}}, 1000)
        elif case == 'prior_tv':
            store.tv_preparation.record_draw([])
        elif case == 'excluded_seed':
            # Bootstrap has already applied current exclusions to its offer.
            seed_dislike(store, radio['seeds'][0]['key'], until=None, now=1000)
            radio['seeds'] = radio['seeds'][1:]
    full = account_growth.view(store, 1000, radio=radio)
    assert full['ready_areas']['radio'] is expected
    if case == 'missing_mixes':
        nodes = {node['id']: node for node in full['nodes']}
        assert nodes['discover']['ready']
        assert not nodes['listening']['ready']

    def later_output(*args, **kwargs):
        pytest.fail('Radio access assembled an unrelated later destination')

    monkeypatch.setattr(account_growth.tv, 'queue', later_output)
    monkeypatch.setattr(account_growth.into_recently, 'load', later_output)
    monkeypatch.setattr(account_growth.growth_readiness, 'vibes', later_output)
    writes = store.conn.total_changes
    assert account_growth.radio_access(store, 1000, radio=radio) is expected
    assert store.conn.total_changes == writes
