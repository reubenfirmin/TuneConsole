import json

import pytest

from yt_playlist.rec import album_choices, album_tuning, rec_params


def records():
    return [{'browse_id': f'album-{i}', 'artist': f'Artist {i}', 'title': f'Record {i}',
             'classification': 'rediscovery' if i < 6 else 'discovery',
             'score': 1 - i / 20, 'basis': 'content:test', 'interest': i % 2,
             'feature': [1., 0.] if i % 2 else [0., 1.], 'affinity': .9,
             'genre': 'Jazz' if i % 2 else 'Techno', 'year': str(1980 + i * 4)}
            for i in range(12)]


def test_default_menu_reserves_discovery_and_excludes_duplicate_artists(store):
    pool = records()
    pool[7]['artist'] = pool[6]['artist']
    pool[8]['artist'] = pool[0]['artist']
    for epoch in range(8):
        items, policy = album_choices.menu(pool, album_tuning.defaults(store), epoch)
        assert len(items) == len({item['artist'] for item in items}) == 4
        assert policy['discovery_offered'] == policy['discovery_requested'] == 2
        assert policy['coverage_gap'] == 0
        assert all(item['hypothesis']['interest'] is not None for item in items)
        assert policy['causal_comparison'] is False
        assert len(policy['eligible_options']) == len(pool)


def test_explicit_refresh_gets_four_different_records_and_reports_constraint_shortages(store):
    pool = records()
    first, _ = album_choices.menu(pool, album_tuning.defaults(store))
    excluded = [item['browse_id'] for item in first]
    second, policy = album_choices.menu(pool, album_tuning.defaults(store), 1, exclude=excluded)
    assert len(second) == 4 and not set(excluded) & {item['browse_id'] for item in second}
    assert policy['previous_selection'] == sorted(excluded)
    # A sparse genre/artist pool still supplies a complete set where possible.
    sparse, _ = album_choices.menu(pool[:4], album_tuning.defaults(store), exclude=excluded)
    assert len(sparse) == 4


def test_discovery_shortage_and_genre_restriction_remain_explicit(store):
    pool = records()[:7]
    items, policy = album_choices.menu(pool, album_tuning.defaults(store), genres=['techno'])
    assert all(item['genre'] == 'Techno' for item in items)
    assert policy['discovery_requested'] == 2 and policy['discovery_offered'] == 1
    assert policy['coverage_gap'] == 1
    assert len(items) == 4
    assert album_choices.menu(pool, album_tuning.defaults(store), genres=['not-present'])[0] == []


def test_comfort_and_novelty_change_familiarity_and_reach_without_an_era_bias(store):
    pool = records()[:6]
    near, familiar = album_choices.menu(pool, {'novelty': 0})
    far, novel = album_choices.menu(pool, {'novelty': 1})
    assert near[0]['browse_id'] == 'album-0' and far[0]['browse_id'] == 'album-5'
    assert familiar['discovery_requested'] == 0 and novel['discovery_requested'] == 4
    for album in pool:
        album['year'] = '2026'
    assert [a['browse_id'] for a in album_choices.menu(pool, {'novelty': 0})[0]] == [
        a['browse_id'] for a in near]


def test_album_overrides_expire_without_changing_shared_taste(store):
    rec_params.set_param(store, 'album_menu_novelty', .25)
    defaults = album_tuning.defaults(store)
    album_tuning.apply(store, 100, {'novelty': 1}, ['jazz'])
    assert album_tuning.state(store, 101)['values']['novelty'] == 1
    assert album_tuning.state(store, 101)['genres'] == ['jazz']
    assert rec_params.get_param(store, 'album_menu_novelty') == .25
    assert album_tuning.state(store, 100 + album_tuning.DURATION_S) == {
        'values': defaults, 'genres': [], 'remaining': 0}
    assert store.settings.get_setting('radio_tuning') is None
    for bad in (float('nan'), float('inf'), -.1, True):
        with pytest.raises(ValueError):
            album_tuning.apply(store, 100, {'novelty': bad}, [])
    store.settings.set_setting(album_tuning.SETTING, json.dumps({
        'until': 1000, 'values': defaults, 'genres': 'jazz'}))
    assert album_tuning.state(store, 101)['genres'] == []
    store.settings.set_setting(album_tuning.SETTING, json.dumps({'until': 1000,
        'values': {'discovery': .75, 'stretch': .2, 'variety': .4, 'recency': .5}, 'genres': ['jazz']}))
    assert album_tuning.state(store, 101) == {
        'values': {'novelty': .75}, 'genres': ['jazz'], 'remaining': 899}


@pytest.mark.parametrize('action', ['known', 'saved'])
def test_current_album_state_and_reversals_reconcile_without_refitting(store, action):
    prepared = records()[6:10]
    target = prepared[0]

    def current():
        items, policy = album_choices.menu(album_choices.eligible(store, prepared, 101),
                                           album_tuning.defaults(store))
        offered = next(item for item in items if item['browse_id'] == target['browse_id'])
        assert policy['discovery_offered'] == sum(
            item['classification'] in {'discovery', 'familiar_artist'} for item in items)
        assert next(item for item in policy['eligible_options'] if item['id'] == target['browse_id'])[
            'classification'] == offered['classification']
        return offered, policy

    if action == 'known':
        album_choices.set_feedback(store, target['browse_id'], 'known', 100)
        expected = 'known'
    else:
        store.collection.add_saved_album({'browse': target['browse_id'], 'artist': target['artist'],
                                           'title': target['title']})
        expected = 'rediscovery'
    offered, policy = current()
    assert offered['classification'] == expected and policy['discovery_offered'] == 3
    # Rechecking never mutates the shared prepared object or its model scores.
    assert target['classification'] == 'discovery'
    assert offered['score'] == target['score'] and offered['feature'] == target['feature']
    if action == 'known':
        album_choices.set_feedback(store, target['browse_id'], 'clear', 102)
    else:
        store.collection.remove_saved_album(target['browse_id'])
    offered, policy = current()
    assert offered['classification'] == 'discovery' and policy['discovery_offered'] == 4


@pytest.mark.parametrize('classification', ['known', 'rediscovery'])
def test_prepared_familiarity_does_not_survive_its_current_state_or_lose_its_origin(store, classification):
    prepared = [{**records()[6], 'classification': classification, 'unsaved_classification': 'familiar_artist'}]
    assert album_choices.eligible(store, prepared, 100)[0]['classification'] == 'familiar_artist'
    album_choices.set_feedback(store, prepared[0]['browse_id'], 'known', 101)
    assert album_choices.eligible(store, prepared, 102)[0]['classification'] == 'known'
    album_choices.set_feedback(store, prepared[0]['browse_id'], 'clear', 103)
    assert album_choices.eligible(store, prepared, 104)[0]['classification'] == 'familiar_artist'


def test_album_mutes_use_canonical_homonyms_and_recheck_cached_frontiers(store):
    first = store.artists.ensure_artist('Same', provider='youtube', provider_id='UCfirst')
    second = store.artists.ensure_artist('Same', provider='youtube', provider_id='UCsecond')
    for browse, artist in [('first', first), ('second', second)]:
        store.discovery.upsert_discovered_album(browse, 'Same', 'Record', None, None, 1, artist_id=artist)
    prepared = [{'browse_id': browse, 'artist': 'Same', 'title': 'Record'} for browse in ('first', 'second')]
    assert len(album_choices.eligible(store, prepared, 2)) == 2
    store.rec_model.record_feedback('for_you', f'artist:@artist:{first}', 'mute', now=3, until=10)
    assert store.rec_model.muted_artists() == {f'@artist:{first}'}
    assert [a['browse_id'] for a in album_choices.eligible(store, prepared, 4)] == ['second']
    assert [a['browse_id'] for a in album_choices.eligible(store, prepared, 10)] == ['first', 'second']
    assert len(prepared) == 2


def test_album_mutes_include_individual_collaboration_credits(store):
    credits = [{'name': 'First', 'id': 'UCfirst'}, {'name': 'Second, with comma', 'id': 'UCsecond'}]
    store.collection.add_saved_album({'browse': 'joint', 'title': 'Joint',
                                      'artist': 'First, Second, with comma', 'credits': credits})
    store.collection.add_saved_album({'browse': 'solo', 'title': 'Solo', 'artist': 'First',
                                      'credits': credits[:1]})
    prepared = [{'browse_id': 'joint', 'artist': 'First, Second, with comma', 'title': 'Joint'},
                {'browse_id': 'solo', 'artist': 'First', 'title': 'Solo'}]
    muted = store.artists.resolve('Second, with comma')
    store.rec_model.record_feedback('for_you', f'artist:@artist:{muted}', 'mute', now=3)
    assert [a['browse_id'] for a in album_choices.eligible(store, prepared, 4)] == ['solo']
    assert not store.rec_model.muted_album_ids([], 4)
