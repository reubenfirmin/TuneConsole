"""Radio access keeps journey prerequisites without preparing later destinations."""
import pytest
from tests.derived_rating_support import seed_dislike
from tests.enrichment_support import complete_entries, complete_discovered

from tests.conftest import seed_catalog_track
from tests.onboarding_support import publish_personal_mix
from tests.test_growth_journey import early_journey
from yt_playlist.rec import account_growth, home_prepared


@pytest.mark.parametrize('case, expected', [
    ('empty', False), ('missing_foundation', False), ('missing_albums', False),
    ('missing_discover', False), ('missing_mixes', False), ('missing_seeds', False),
    ('incomplete_coverage', False), ('ready', True), ('prior_radio', True),
    ('prior_tv', True), ('prior_vibes', True), ('excluded_seed', False),
])
def test_radio_access_matches_journey_and_preserves_earned_access(store, monkeypatch, case, expected):
    radio = {'seeds': [], 'state': {}, 'seedMenu': 'current'}
    if case != 'empty':
        if not case.startswith('prior_'):
            early_journey(store)
            mixes = publish_personal_mix(store)
        for i in range(4):
            key, video = f'start {i}|artist', f'start-{i}'
            seed_catalog_track(store, key, f'Start {i}', 'Artist', video_id=video)
            radio['seeds'].append({'key': key, 'video_id': video})
        if case != 'incomplete_coverage' and case != 'prior_radio':
            complete_entries(store, [row[0] for row in store.conn.execute('SELECT id FROM tracks')], 1)
            complete_discovered(store, [row[0] for row in store.conn.execute(
                'SELECT identity_key FROM discovered_tracks')], 1)
        if case == 'missing_foundation':
            store.settings.set_setting('rec_content_generation', 'unprepared-replacement')
        elif case == 'missing_albums':
            store.rec_proposals.put_proposals('album_prepared', {}, 1000)
        elif case == 'missing_discover':
            store.rec_proposals.put_proposals(home_prepared.DISCOVERY_SURFACE, {}, 1000)
        elif case == 'missing_mixes':
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
            store.settings.set_setting('tv_draw', '1')
        elif case == 'excluded_seed':
            # Bootstrap has already applied current exclusions to its offer.
            seed_dislike(store, radio['seeds'][0]['key'], until=None, now=1000)
            radio['seeds'] = radio['seeds'][1:]
    full = account_growth.view(store, 1000, radio=radio)
    assert full['ready_areas']['radio'] is expected

    def later_output(*args, **kwargs):
        pytest.fail('Radio access assembled an unrelated later destination')

    monkeypatch.setattr(account_growth.tv, 'queue', later_output)
    monkeypatch.setattr(account_growth.into_recently, 'load', later_output)
    monkeypatch.setattr(account_growth.growth_readiness, 'vibes', later_output)
    writes = store.conn.total_changes
    assert account_growth.radio_access(store, 1000, radio=radio) is expected
    assert store.conn.total_changes == writes
