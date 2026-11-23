"""Positive acquisition, ranking and save workflows without a database connection."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, call

import numpy as np
import pytest

from tests.repository_mock_support import no_database, repository_store
from yt_playlist.controllers.clusters import Clusters
from yt_playlist.controllers.discovery_actions import DiscoveryActions
from yt_playlist.controllers.discovery_catalog import DiscoveryCatalog
from yt_playlist.controllers.discovery_preparation import DiscoveryArtists, DiscoveryPreparation
from yt_playlist.controllers.road_trip_transitions import RoadTripFacts, RoadTripTransitions
from yt_playlist.controllers.suggestions import Suggestions, SuggestionFeedback
from yt_playlist.integrations.youtube_music_access import YouTubeMusicAccess
from yt_playlist.rec.discover import _rank_candidates

pytestmark = pytest.mark.usefixtures('no_database')


def test_artist_profile_read_leaves_alias_reconciliation_to_discovery():
    provider = Mock(spec_set=['get_artist', 'get_album'])
    provider.get_artist.return_value = {'name': 'Artist', 'albums': {'results': [
        {'title': 'Record', 'browseId': 'MPREnew', 'year': '2020'}]}}
    provider.get_album.return_value = {'title': 'Record', 'artists': [{'name': 'Artist'}],
        'year': '2020', 'trackCount': 1, 'tracks': [
            {'videoId': 'video', 'title': 'Song', 'duration': '3:00', 'isExplicit': False}]}
    collection = Mock(spec_set=['album_alias_candidates', 'claim_album_alias_check',
                               'observe_album_representation'])
    collection.album_alias_candidates.return_value = [
        {'title': 'Record', 'artist': 'Artist', 'browse_id': 'MPREold'}]
    collection.claim_album_alias_check.return_value = True
    access = YouTubeMusicAccess(provider)
    command = DiscoveryCatalog(catalog=lambda: access, radio_client=Mock(), collection=collection,
        discovery=Mock(), query=Mock(), tracks=Mock(), charts=Mock(), settings=Mock(),
        rebuild=Mock(), now=lambda: 1000, logger=Mock())

    profile = access.artist_profile('UCartist')
    assert [album['browse_id'] for album in profile['albums']] == ['MPREnew']
    assert provider.mock_calls == [call.get_artist('UCartist')]
    assert collection.mock_calls == []

    assert command.artist('Artist', 'UCartist') == profile
    collection.claim_album_alias_check.assert_called_once_with('MPREnew', 'MPREold', 1000)
    assert provider.get_album.call_args_list == [call('MPREold'), call('MPREnew')]
    assert collection.observe_album_representation.call_args_list == [
        call('MPREold', provider.get_album.return_value), call('MPREnew', provider.get_album.return_value)]


def test_catalog_population_bounds_requests_respects_exact_ownership_and_remembers_fetches():
    discovery = Mock(spec_set=['discovered_source_tokens', 'get_discovered_albums', 'fetched_albums',
                               'upsert_discovered_track', 'remember_album_fetch'])
    discovery.discovered_source_tokens.return_value = set()
    discovery.fetched_albums.return_value = {'fetched'}
    discovery.get_discovered_albums.return_value = [dict(browse_id=bid, artist='Artist', title='Album')
                                                   for bid in ('fetched', 'first', 'second')]
    client = Mock(spec_set=['get_album'])
    client.get_album.return_value = {'tracks': [dict(videoId=video, title=title)
        for video, title in [('owned-upload', 'Live song'), ('fresh-upload', 'Song (Remix)')]]}
    tracks = Mock(spec_set=['identity_key_for_video'])
    tracks.identity_key_for_video.side_effect = lambda video: 'owned' if video == 'owned-upload' else None
    query = Mock(spec_set=['library_keys'])
    query.library_keys.return_value = {'owned'}
    rebuild = Mock()
    command = DiscoveryCatalog(catalog=lambda: YouTubeMusicAccess(client), radio_client=Mock(), collection=Mock(),
        discovery=discovery, query=query, tracks=tracks, charts=Mock(), settings=Mock(),
        rebuild=rebuild, now=lambda: 1000, logger=Mock())
    assert command.populate(1000, budget=1) == 1
    client.get_album.assert_called_once_with('first')
    tracks.identity_key_for_video.assert_has_calls([call('owned-upload'), call('fresh-upload')])
    written = discovery.upsert_discovered_track.call_args.args
    assert written[1:5] == ('fresh-upload', 'Song (Remix)', 'Artist', 'Album')
    discovery.remember_album_fetch.assert_called_once_with('first')
    rebuild.assert_called_once()


def test_cluster_query_expands_then_detached_ring_keeps_recording_and_album_limits():
    from yt_playlist.controllers.clusters import ClusterQuery
    rows = {str(i): dict(video_id=f'v{i}', title=f'Song {i}', artist='Artist',
                         album='Same album' if i < 3 else 'Another album', thumbnail=None) for i in range(5)}
    query = Mock(spec_set=['tracks_by_keys', 'track_genres'])
    query.tracks_by_keys.side_effect = lambda keys: {key: rows[key] for key in keys if key in rows}
    query.track_genres.return_value = {key: 'Jazz' for key in rows}
    genres = Mock(spec_set=['feature_axes'])
    genres.feature_axes.return_value = {'Jazz': ('jazz', None)}
    expand = Mock(return_value=[(key, 1 - int(key)/10) for key in rows])
    command = ClusterQuery(query, Mock(), genres, Mock(), rank_candidates=expand, geometry=Mock())
    payload, offered = command.expansion({'pos_keys': ['seed'], 'k': 6})
    assert [row['video_id'] for row in payload['ring']] == ['v0', 'v1', 'v3', 'v4']
    assert offered == []
    assert expand.call_args.kwargs['topn'] == 24
    assert all(row['family'] == 'jazz' for row in payload['ring'])


def test_discovery_pass_bounds_scan_rotates_owned_albums_and_preserves_held_tracks():
    store = repository_store(query={'owned_albums': {7}, 'artist_genres': {'artist-key': 'Jazz'},
        'library_artists': {'artist-key'}, 'library_keys': {'owned-song'}},
        collection={'saved_album_ids': {'saved'}, 'album_id': None},
        artists={'key': 'artist-key', 'resolve': 1}, eligibility={'generated_track_keys': {'held-song'}},
        history={'spotify_history_stats': {'events': 1, 'days': 0, 'artists': 1}},
        discovery={'artists_due_for_scan': [{'artist': 'Artist', 'artist_id': 1, 'browse_id': 'channel'}],
            'discovered_albums_for_artist': {'retired': 2}, 'delete_discovered_albums': 1,
            'upsert_discovered_album': None, 'mark_scanned': None, 'upsert_discovered_artist': None,
            'prune_discovered': None, 'prune_discovered_tracks': None,
            'interested_artists': [{'artist_id': 1}], 'get_discovered_albums': [],
            'prune_orphan_discovered_tracks': 2})
    store.collection.album_id.side_effect = lambda browse: 7 if browse == 'owned' else None
    info = Mock(return_value={'albums': [
        {'browse_id': name, 'title': name} for name in ('owned', 'saved', 'new', '')]})
    populate, radio = Mock(), Mock()
    command = DiscoveryPreparation(store.query, store.collection, store.artists, store.discovery,
        store.eligibility, store.history, parameter=lambda name: 1, artist_info=info,
        new_artists=lambda: [{'artist': 'New artist', 'score': .8}], populate=populate, radio=radio, logger=Mock())
    assert command.run(1000, budget=2) == {'scanned': 1}
    store.discovery.artists_due_for_scan.assert_called_once_with(
        1000, budget=2, artist_limit=1, with_ids=True, require_channel=True)
    info.assert_called_once_with('Artist', 'channel')
    assert store.discovery.upsert_discovered_album.call_args.args[0] == 'new'
    assert store.discovery.upsert_discovered_album.call_count == 1
    assert store.discovery.delete_discovered_albums.call_args_list[0].args == (['retired'],)
    assert store.discovery.upsert_discovered_artist.call_args.kwargs['source'] == 'spotify_model'
    store.discovery.prune_discovered_tracks.assert_called_once_with({'owned-song'}, held_keys={'held-song'})
    store.discovery.prune_orphan_discovered_tracks.assert_called_once_with(held_keys={'held-song'})
    populate.assert_called_once_with(1000)
    radio.assert_called_once_with(1000)


def test_artist_query_uses_real_bridge_ranking_and_does_not_cache_outage_as_empty_success():
    from yt_playlist.rec.artist_model import ArtistNeighbor

    cache = Mock(spec_set=['cached_similar', 'cache_similar', 'lookup_name'])
    cache.cached_similar.return_value = None
    cache.lookup_name.return_value = 'Anchor'
    taste = SimpleNamespace(titles=['Favorites'], context_contributions=lambda value: np.array([value[0]]))
    command = DiscoveryArtists(taste=lambda: taste, vectors=lambda: (['seed'], np.array([[1., 0.]]), {'seed': 0}),
        anchors=lambda *args: [('anchor', 1., np.array([1., 0.]))], owned=lambda: {'owned'},
        reference=lambda key: {'id': 1, 'name': 'Anchor'} if key == 'anchor' else None,
        cache=cache, similar=lambda label: ([], False),
        neighbors=lambda name, **kwargs: [ArtistNeighbor('owned', 1., {'content': 1.}, None),
                                          ArtistNeighbor('new artist', .8, {'lastfm': .8}, None)], rank=_rank_candidates,
        thumbnail=lambda name: 'image', genre=lambda name: 'Jazz', now=lambda: 1000)
    result = command.query(limit=1)
    assert len(result) == 1 and result[0]['artist'] == 'New Artist'
    assert result[0]['because'] == ['Anchor'] and result[0]['score'] > 0
    assert result[0]['thumbnail'] == 'image' and result[0]['genre'] == 'Jazz'
    cache.cache_similar.assert_not_called()
    cache.cached_similar.assert_called_once_with('anchor', 1000)
    assert result[0]['sources'] == {'anchor': {'lastfm': .8}}


def test_suggestions_capture_cold_metadata_and_record_exact_ranked_offer():
    store = repository_store(playlists={}, query={'key_for_video': 'seed', 'tracks_by_keys': {'seed': {'title': 'Seed'}}},
        discovery={'playable_discoveries_by_keys': {'cold': {'video_id': 'cold-video', 'title': 'Cold', 'artist': 'Artist'}}},
        model={'rec_vectors_count': 1})
    record = Mock()
    command = Suggestions(store.playlists, store.query, store.discovery, store.model, complete=Mock(),
        neighbors=lambda key, **kwargs: [('cold', .75)], record=record, rebuild=Mock())
    result = command.similar('seed-video', 3, 1000)
    assert result['items'][0]['video_id'] == 'cold-video'
    assert record.call_args.args[1][0]['key'] == 'cold'
    assert record.call_args.args[1][0]['score'] == .75
    store.discovery.playable_discoveries_by_keys.assert_called_once_with(['cold'])


def test_feedback_applies_interpretation_and_lane_change_but_own_it_does_not_teach_taste():
    store = repository_store(model={'record_feedback': None, 'get_weights': {}, 'set_session_weight': None},
                             signals={}, playlists={}, recipes={})
    facts = Mock(return_value={})
    command = SuggestionFeedback(store.model, store.signals, store.playlists, store.recipes, facts=facts,
        parameter=lambda name: 1., lanes={'comfort'})
    command.feedback('exact', 'less', 'genre', 'for_you', 'track', 'comfort', 'genre:jazz', 1000)
    assert [call.args for call in store.model.set_session_weight.call_args_list] == [
        ('genre:jazz', .85, 1000), ('lane:comfort', .85, 1000)]
    store.model.set_session_weight.reset_mock()
    command.feedback('exact', 'less', 'own_it', 'for_you', 'track', '', '', 1001)
    store.model.set_session_weight.assert_not_called()
    facts.assert_not_called()
    assert store.model.record_feedback.call_count == 2


def test_acquisition_preflights_account_then_passes_exact_identity_to_confirmed_rating_owner():
    store = repository_store(discovery={'discovered_tracks_by_keys': {'recording-with-version': {
        'video_id': 'exact-video', 'title': 'Song (Live)', 'artist': 'Artist', 'album': 'Live'}}})
    account, rating = Mock(return_value=(1, object())), Mock(return_value={})
    command = DiscoveryActions(store.discovery, retention_days=lambda: 30, account=account, rating=rating, logger=Mock())
    assert command.acquire('track', 'recording-with-version')['status'] == 'confirmed'
    rating.assert_called_once_with('exact-video', 'LIKE', acquisition={'title': 'Song (Live)',
        'artist': 'Artist', 'album': 'Live', 'identity_key': 'recording-with-version'})
    account.side_effect = ValueError('Account missing')
    assert command.acquire('track', 'other')['status'] == 'unavailable'
    assert store.discovery.discovered_tracks_by_keys.call_count == 1


def test_cluster_save_owns_recipe_metadata_order_and_complete_generated_command():
    rows = {key: {'video_id': key, 'title': key, 'artist': 'Artist', 'album': '', 'thumbnail': None}
            for key in ('library', 'cold', 'central')}
    store = repository_store(query={'tracks_by_keys': {'library': rows['library'], 'central': rows['central']}},
        discovery={'playable_discoveries_by_keys': {'cold': rows['cold']}}, recipes={}, evidence={}, model={})
    generated = Mock(spec_set=['create'])
    generated.create.return_value = {'status': 'confirmed', 'new_ytm': 'remote', 'pid': 9, 'added': 3}
    recipe = Mock(return_value=({'model': 'cluster'}, ['cold', 'library', 'central']))
    command = Clusters(store.query, store.discovery, store.recipes, store.evidence, store.model,
        scans=Mock(), expand=Mock(), explain=Mock(), recipe=recipe, generated=lambda: generated,
        record=Mock(), now=lambda: 1000, logger=Mock())
    result = command.save('Mix', ['library', 'cold'], ['central'], ['Artist'], ['jazz'], 'shuffle', {'nodes': []}, 'intent')
    assert result['added'] == 3 and result['pid'] == 9
    recipe.assert_called_once_with(['library', 'cold', 'central'], ['Artist'], ['jazz'], journey='shuffle')
    assert [track['video_id'] for track in generated.create.call_args.args[1]] == ['cold', 'library', 'central']
    assert generated.create.call_args.kwargs == {'recipe': {'model': 'cluster'}, 'navigate': True,
                                               'intent': 'intent', 'canvas': {'nodes': []}}


def test_draft_transitions_capture_once_before_real_repick_and_preserve_exact_recording_ids():
    events = []
    songs = [{'key': f'exact-{i}', 'video_id': f'video-{i}', 'title': f'Song {i} (Live)',
              'artist': f'Artist {i}', 'album': '', 'thumbnail': None, 'duration': 300,
              'genre': 'Jazz', 'year': 2000, 'liked': False} for i in range(8)]
    store = repository_store(query={'library_songs': songs, 'play_counts': {}, 'artist_genre_years': {}},
        model={'suppressed_keys': {'exact-0'}, 'muted_artists': set()}, genres={'feature_axes': {'Jazz': ('jazz', None)}})
    @contextmanager
    def inputs():
        events.append('capture')
        yield RoadTripFacts(store.query, store.model, store.genres)
        events.append('released')
    command = RoadTripTransitions(inputs)
    recipe = {'id': 1, 'name': 'Trip', 'own_pct': 100, 'target_minutes': 15,
              'familiarity_pct': 50, 'artists': [], 'genres': []}
    state = command.start(recipe, 1000, 7)
    assert events == ['capture', 'released']
    assert len(state['picked']) == 3 and all(row['video_id'] != 'video-0' for row in state['picked'])
    assert all(row['title'].endswith('(Live)') and row['video_id'] in {song['video_id'] for song in songs}
               for row in state['picked'])
    store.query.library_songs.assert_called_once_with()
    state = command.reroll(state, 0, 1001)
    assert len(state['picked']) == 3 and store.query.library_songs.call_count == 2


def test_draft_owner_rejects_cancelled_worker_and_keeps_new_revision_with_real_transitions():
    from yt_playlist.web.road_trip_drafts import RoadTripDrafts
    recipe = {'id': 1, 'name': 'Trip', 'own_pct': 100, 'target_minutes': 15,
              'familiarity_pct': 50, 'artists': ['Guest'], 'genres': []}
    songs = [dict(key=str(i), video_id=f'v{i}', title=f'Song {i}', artist=f'Artist {i}',
                  album='', thumbnail=None, duration=300, genre='Jazz', year=2000, liked=False)
             for i in range(8)]
    store = repository_store(query={'library_songs': songs, 'play_counts': {}, 'artist_genre_years': {}},
        model={'suppressed_keys': set(), 'muted_artists': set()}, genres={'feature_axes': {'Jazz': ('jazz', None)}})
    @contextmanager
    def inputs():
        yield RoadTripFacts(store.query, store.model, store.genres)
    transitions = RoadTripTransitions(inputs)
    persisted, queued = {}, []
    repository = Mock(spec_set=['get_road_trip_recipe', 'get_road_trip_draft',
                                'save_road_trip_draft', 'replace_road_trip_draft'])
    repository.get_road_trip_recipe.return_value = recipe
    repository.get_road_trip_draft.side_effect = lambda rid: deepcopy(persisted.get(rid))
    repository.save_road_trip_draft.side_effect = lambda rid, state, now: persisted.update({rid: deepcopy(state)})
    def replace(rid, state, now, *, expected_revision):
        if persisted.get(rid, {}).get('revision') != expected_revision:
            return False
        persisted[rid] = deepcopy(state)
        return True
    repository.replace_road_trip_draft.side_effect = replace
    work = Mock(spec_set=['wrap'])
    work.wrap.side_effect = lambda label, run, job: lambda: run(job)
    acquisition = Mock(spec_set=['add', 'fill'])
    acquisition.add.side_effect = lambda state, client, item, now: state['done_inputs'].append(item['name'])
    owner = RoadTripDrafts(repository, transitions, acquisition, now=lambda: 1000, work=work,
        spawn=queued.append, artist_genre=Mock(), served=Mock(), identity=Mock(), record=Mock())
    client = object()
    first = owner.build(1, client)
    replacement = owner.shuffle(1, client)
    assert first['generation'] != replacement['generation']
    before = deepcopy(persisted[1])
    queued[0]()
    assert persisted[1] == before
    acquisition.add.assert_not_called()
    queued[1]()
    acquisition.add.assert_called_once()
    assert acquisition.add.call_args.args[1] is client
    assert persisted[1]['done_inputs'] == ['Guest'] and not persisted[1]['building']
    assert persisted[1]['revision'] != replacement['revision']
    assert len(persisted[1]['picked']) == 3 and owner._jobs == {}


def test_surface_query_bounds_candidates_and_real_rules_filter_suppressed_and_muted():
    from yt_playlist.controllers.surface_queries import SurfaceQueries
    rows = [dict(key=str(i), video_id=f'v{i}', title=f'Song {i}', artist=f'Artist {i}',
                 album='', thumbnail=None, plays=10, last_played=0, shared_playlists=2) for i in range(6)]
    store = repository_store(query={},
        candidates={'more_like_rotation': rows, 'deep_cuts': list(reversed(rows)), 'comfort_candidates': rows},
        model={'rec_vectors_count': 0, 'get_weights': {}, 'suppressed_keys': {'0'}, 'muted_artists': {'Artist 1'}},
        genres={})
    parameter = lambda name: {'candidate_pool_factor': 3, 'weight_revert_halflife_d': 30,
                             'comfort_recency_full_days': 30, 'comfort_min_plays': 5}[name]
    command = SurfaceQueries(store.query, store.candidates, store.model, store.genres,
        parameter=parameter, taste=Mock(), vectors=Mock(), score=Mock(), content=Mock())
    for result, expected in ((command.wheelhouse(1000, limit=2), ['2', '5']),
                             (command.comfort(1000, limit=2), ['2', '3'])):
        assert [row.key for row in result] == expected
        assert len({row.video_id for row in result}) == 2
        assert all(row.key != '0' and row.artist != 'Artist 1' for row in result)
    store.candidates.more_like_rotation.assert_called_once_with(limit=6)
    store.candidates.deep_cuts.assert_called_once_with(limit=6)
    store.candidates.comfort_candidates.assert_called_once_with(1000, min_plays=5, recency_full_days=30, limit=6)


def test_catalog_hydrates_bounded_batches_and_replenishes_after_ineligible_or_missing_rows():
    from yt_playlist.controllers.surface_queries import SurfaceQueries
    keys = [f'{i:05d}' for i in range(10000)]
    query = Mock(spec_set=['play_counts', 'library_keys', 'track_genres', 'tracks_by_keys'])
    query.play_counts.return_value = {}
    query.library_keys.return_value = set(keys)
    query.track_genres.side_effect = lambda batch: {key: 'Muted' if key == '00002' else 'Jazz' for key in batch}
    def documents(batch):
        return {key: dict(title=key, artist='Muted artist' if key == '00001' else 'Artist',
                         album='', video_id=key, thumbnail=None) for key in batch if key != '00003'}
    query.tracks_by_keys.side_effect = documents
    model = Mock(spec_set=['get_weights', 'suppressed_keys', 'muted_artists'])
    model.get_weights.return_value = {'genre:muted': 0.}
    model.suppressed_keys.return_value = {'00000'}
    model.muted_artists.return_value = {'Muted artist'}
    genres = Mock(spec_set=['feature_axes'])
    genres.feature_axes.side_effect = lambda values: {value: (value.lower(), None) for value in values}
    command = SurfaceQueries(query, None, model, genres,
        parameter={'candidate_pool_factor': 2, 'weight_revert_halflife_d': 30}.__getitem__,
        taste=lambda now: None, vectors=lambda: ([], None, {}), score=Mock(), content=lambda plays, now: {})
    result = command.catalog(1000, limit=2)
    assert [row.key for row in result] == ['00004', '00005']
    assert all('Never played, from your library' == row.reason for row in result)
    assert [call.args[0] for call in query.track_genres.call_args_list] == [keys[1:4], keys[4:8]]
    assert [call.args[0] for call in query.tracks_by_keys.call_args_list] == [['00001', '00003'], keys[4:8]]
    # Exhaustion still walks the bounded batches; it never drops exclusions to fill a card.
    query.track_genres.reset_mock()
    query.tracks_by_keys.reset_mock()
    query.library_keys.return_value = set(keys[:7])
    query.track_genres.side_effect = lambda batch: dict.fromkeys(batch, 'Muted')
    assert command.catalog(1000, limit=2) == []
    assert max(len(call.args[0]) for call in query.track_genres.call_args_list) <= 4
    query.tracks_by_keys.assert_not_called()


def test_version_policy_handoff_keeps_many_to_many_memberships_through_draft_and_save():
    from yt_playlist.rec import road_trip, recipes
    from yt_playlist.controllers.road_trip_commands import RoadTripCommands
    policy = {'confirmed_memberships': {'one': ['work-a'], 'medley': ['work-a', 'work-b'], 'two': ['work-b']},
        'destination_recordings': ['destination-live'], 'allow_multiple': True,
        'policy_revision': 'policy-3', 'membership_revision': 'membership-7'}
    own = [dict(video_id=f'v{i}', title=f'Song {i} (Live)', artist=f'Artist {i}',
        album='', thumbnail=None, duration=300, source='mine', genre='Jazz', family='jazz', year=2000,
        decade='2000', cluster_genre='', liked=False, plays=0, score=1., fam=.5) for i in range(6)]
    recipe = dict(id=1, name='Trip', own_pct=100, target_minutes=15, artists=[], genres=[])
    state = road_trip.start_values(recipe, 7, road_trip.DraftInputs(own, {'Jazz': ('jazz', None)}, {}, policy))
    assert state['version_policy'] == policy and state['version_policy'] is not policy
    drafts, generated = Mock(spec_set=['for_save']), Mock(spec_set=['road_trip'])
    drafts.for_save.return_value = recipe, state
    generated.road_trip.return_value = {'status': 'confirmed', 'new_ytm': 'remote', 'pid': 9, 'added': 3}
    command = RoadTripCommands(Mock(), drafts, account=Mock(), generated=lambda: generated,
                               artist_genre=Mock(), logger=Mock())
    assert command.save(1, 'intent')['pid'] == 9
    assert generated.road_trip.call_args.args[5]['version_policy'] == policy
    cluster, order = recipes.cluster_values(['one', 'medley', 'two'], [], [], 'shuffle',
        {}, {}, {}, {}, {}, {}, {}, version_policy=policy)
    assert set(order) == {'one', 'medley', 'two'}
    assert cluster['version_policy'] == policy and cluster['version_policy'] is not policy
