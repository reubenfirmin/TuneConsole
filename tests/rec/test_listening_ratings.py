"""Rating evidence, honest denominators, and variety across qualified readings."""
from tests.conftest import seed_catalog_track
from tests.rec.test_listening_editions import NOW, Repo, add, data
from yt_playlist.library import ratings
from yt_playlist.rec import listening_analytics, listening_editions, listening_insights


def find(payload, family):
    return next(s for s in listening_insights.candidates(payload) if s['family'] == family)


def test_known_start_share_includes_unknowns_without_rewriting_them_from_current_ratings():
    payload = data()
    for day in ('2026-10-21', '2026-10-22', '2026-10-23'):
        add(payload, 'one', 'Artist', day, 10, rated=8, liked=6)
    story = find(payload, 'liked_starts')
    assert story['stat'] == 75
    assert story['rating_counts'] == {'liked': 18, 'not_liked': 6, 'unknown': 6}
    assert '24 of 30 plays' in story['evidence']
    for row in payload['rows']:
        row[payload['columns'].index('rated')] = 0
        row[payload['columns'].index('liked')] = 0
    payload['retained'] = {'hearts': 10, 'skulls': 0}
    assert not any(s['family'] == 'liked_starts' for s in listening_insights.candidates(payload))


def test_confirmed_reversals_and_clears_remain_actions_and_collection_is_undated(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS', '1')
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    for i in range(9):
        seed_catalog_track(store, f'k{i}', f'Song {i}', f'Artist {i}', video_id=f'v{i}',
                           genre=('Jazz' if i < 5 else 'House') if i != 8 else None)
        ratings.record_observations(store, iid, {f'v{i}': 'LIKE'}, source='sync', now=NOW-1000)
    ratings.record_confirmed(store, iid, 'v0', 'DISLIKE', NOW-300)
    ratings.record_confirmed(store, iid, 'v0', 'INDIFFERENT', NOW-200)
    ratings.record_confirmed(store, iid, 'v0', 'LIKE', NOW-100)
    payload = listening_analytics.build(store, NOW)
    # Three reversals of one song are retained facts, not a feature panel.
    metrics = [dict(zip(payload['columns'], row)) for row in payload['rows']]
    assert {kind: sum(r[kind] for r in metrics) for kind in ('hearts', 'skulls', 'clears')} == {
        'hearts': 1, 'skulls': 1, 'clears': 1}
    assert not any(s['family'] == 'rating_actions' for s in listening_insights.candidates(payload))
    collection = find(payload, 'rating_collection')
    assert collection['title'] == 'Your hearts span 2 genres.'
    assert '8 of 9 hearts have a genre' in collection['evidence']
    assert collection['period_label'] == 'Current ratings'
    assert sum(g['hearts'] for g in collection['rating_genres']) == 9
    assert not any(s['family'] == 'liked_starts' for s in listening_insights.candidates(payload))


def test_low_genre_coverage_does_not_claim_preference_breadth():
    payload = data()
    payload['retained'] = {'hearts': 100, 'skulls': 3, 'known_genre_hearts': 4, 'hearted_genres': 2,
                          'genres': [{'name': 'Jazz', 'hearts': 2, 'skulls': 0}]}
    assert not any(s['family'] == 'rating_collection' for s in listening_insights.candidates(payload))


def exploring():
    payload = data()
    for artist in ('Ada', 'Bea', 'Cleo'):
        add(payload, artist+'old', artist, '2026-06-01')
        add(payload, artist+'old', artist, '2026-10-23')
        for i in range(5):
            add(payload, artist+str(i), artist, f'2026-10-{20+i%3}')
    return payload


def test_another_deeper_artist_cannot_repeat_the_theme_when_likes_offer_new_analysis():
    payload, repo = exploring(), Repo()
    first = listening_editions.prepare(payload, repo, NOW)
    assert [s['family'] for s in first['stories']].count('deeper') == 1
    repo.edition = first
    lead = first['stories'][0]
    repo.views = {lead['id']: NOW, 'hero:'+lead['id']: NOW, 'family:'+lead['family']: NOW}
    # Rating evidence arrives while this reading remains held.
    for day in ('2026-10-22', '2026-10-23', '2026-10-24'):
        add(payload, 'rated', 'Other', day, 30, rated=30, liked=21)
    assert listening_editions.prepare(payload, repo, NOW+3600) == first
    following = listening_editions.prepare(payload, repo, NOW+21601)
    assert following['stories'][0]['family'] == 'liked_starts'
    assert any(s['family'] == 'deeper' for s in following['stories'])  # useful support survives cooldown
    repo.edition = following
    repo.views.update({s['id']: NOW+21601 for s in following['stories']})
    repo.views['hero:'+following['stories'][0]['id']] = NOW+21601
    assert listening_editions.prepare(payload, repo, NOW+86400)['stories'] == following['stories']


def test_unchanged_collection_keeps_identity_across_week_boundary():
    payload = data()
    payload['retained'] = {'hearts': 40, 'skulls': 2, 'known_genre_hearts': 40, 'hearted_genres': 2,
        'leading_hearted_genre': {'name': 'Jazz', 'hearts': 25},
        'genres': [{'name': 'Jazz', 'hearts': 25, 'skulls': 1}, {'name': 'House', 'hearts': 15, 'skulls': 1}]}
    before = find(payload, 'rating_collection')
    payload['today'] = '2026-11-03'
    assert find(payload, 'rating_collection')['id'] == before['id']


def mixed_evidence():
    payload = exploring()
    # An established history predates this window; a single old imported day
    # cannot establish discovery, and recent listening actually returns to it.
    for i in range(100):
        add(payload, f'prior{i%30}', 'Established', f'2026-08-{1+i%14:02}', 1)
    for day in ('2026-10-19', '2026-10-21', '2026-10-23'):
        add(payload, 'prior0', 'Established', day, 10)
    # Reported sparse coverage: 17 known starts, 7 likes, 279 total plays.
    add(payload, 'sparse', 'Other', '2026-10-23', 221, rated=17, liked=7)
    add(payload, 'glory', 'Laika', '2026-10-24', 0, hearts=1)
    for day in ('2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23'):
        add(payload, 'sparse', 'Other', day, 0, new_tracks=0, seconds=1200)
    # Explicit exact first encounters from a mature full-history ledger.
    for i in range(10):
        add(payload, f'discovery{i}', f'New artist{i}', f'2026-10-{19+i%5}',
            1, new_tracks=1, new_artists=1, artist_plays=1)
    payload['retained'] = {'hearts': 553, 'skulls': 62, 'known_genre_hearts': 465, 'hearted_genres': 113,
        'leading_hearted_genre': {'name': 'Techno', 'hearts': 34},
        'genres': [{'name': 'Techno', 'hearts': 34, 'skulls': 3},
                   {'name': 'Complextro', 'hearts': 2, 'skulls': 6},
                   {'name': '', 'hearts': 88, 'skulls': 1},
                   {'name': 'Other genres / untagged', 'combined': True, 'hearts': 429, 'skulls': 52}]}
    return payload


def test_reported_sparse_share_and_single_heart_do_not_displace_four_useful_analyses():
    payload, repo = mixed_evidence(), Repo()
    # Simulate the rejected edition, including cooldown of all useful subjects.
    repo.edition = {'version': listening_editions.VERSION-1, 'day': '2026-10-24', 'built_at': NOW,
        'timezone': 'UTC', 'stories': [{'id': 'old-share'}, {'id': 'old-heart'}],
        'recent_subjects': [['artist:Ada', 'ratings:collection', 'format:albums', 'discovery:recent']]}
    for story in listening_insights.candidates(payload):
        repo.views.update({story['id']: NOW, 'hero:'+story['id']: NOW, 'family:'+story['family']: NOW})
    edition = listening_editions.prepare(payload, repo, NOW+60)
    assert len(edition['stories']) == 4
    families = {s['family'] for s in edition['stories']}
    assert len(families) == 4
    assert {'discovery', 'rating_collection', 'formats'} <= families
    assert families & {'deeper', 'return'}
    assert '2 hearts and 6 skulls' in find(payload, 'rating_collection')['evidence']
    repo.edition = edition
    assert listening_editions.prepare(payload, repo, NOW+3600) == edition
    assert listening_editions.prepare(payload, repo, NOW+86400)['stories'] == edition['stories']


def test_exact_17_of_279_and_one_heart_alone_never_earn_a_feature():
    payload = data()
    add(payload, 'song', 'Artist', '2026-10-23', 279, rated=17, liked=7)
    add(payload, 'glory', 'Laika', '2026-10-24', 0, hearts=1)
    assert listening_insights.candidates(payload) == []
    assert not listening_editions.prepare(payload, Repo(), NOW)['available']


def test_slow_earned_heart_requires_several_distinct_earlier_listening_days():
    payload = data()
    for day in ('2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22'):
        add(payload, 'glory', 'Laika', day)
    add(payload, 'glory', 'Laika', '2026-10-24', 0, hearts=1)
    stories = listening_insights.candidates(payload)
    assert len(stories) == 1 and stories[0]['family'] == 'slow_heart'
    assert '4 days' in stories[0]['evidence']


def test_discovery_requires_history_and_real_plays_and_retains_daily_denominators():
    payload = mixed_evidence()
    story = find(payload, 'discovery')
    assert sum(d['new_tracks'] for d in story['discovery_days']) == 10
    assert sum(d['plays'] for d in story['discovery_days']) == sum(
        r[payload['columns'].index('plays')] for r in payload['rows'] if r[0] >= '2026-10-11')
    payload['rows'] = [r for r in payload['rows'] if r[0] >= '2026-10-11']
    assert not any(s['family'] == 'discovery' for s in listening_insights.candidates(payload))


def test_new_account_and_one_old_import_do_not_establish_discovery():
    payload = data()
    for i in range(100):
        add(payload, f'new{i}', f'Artist{i}', f'2026-10-{19+i%5}',
            1, new_tracks=1, new_artists=1, artist_plays=1)
    assert not any(s['family'] == 'discovery' for s in listening_insights.candidates(payload))
    add(payload, 'import', 'Earlier', '2026-06-01', 0, sightings=1)
    assert not any(s['family'] == 'discovery' for s in listening_insights.candidates(payload))
    # Even a long imported baseline is insufficient without continuity: an
    # entirely first-seen new capture window must not announce 100% discovery.
    for i in range(100):
        add(payload, f'prior{i%30}', 'Earlier', f'2026-08-{1+i%14:02}', 0, sightings=1)
    assert not any(s['family'] == 'discovery' for s in listening_insights.candidates(payload))
