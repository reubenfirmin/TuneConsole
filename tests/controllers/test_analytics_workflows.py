"""Analytics decisions and workflow boundaries without SQLite, files, providers or ASGI."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock
from zoneinfo import ZoneInfoNotFoundError

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.analytics_queries import AnalyticsQueries, StoryQueries
from yt_playlist.controllers.analytics_preparation import ListeningPreparation
from yt_playlist.controllers import trend_preparation
from yt_playlist.controllers.story_preparation import StoryPreparation
from yt_playlist.controllers.taste_mode_acquisition import TasteModes
from yt_playlist.controllers.taste_readings import TasteReadings
from yt_playlist.core import fao
from yt_playlist.providers.base import LookupUnavailable
from yt_playlist.rec import listening_calculations as listening, recap_story, story_rules, taste_display
from yt_playlist.web import analytics_views

pytestmark = pytest.mark.usefixtures('no_database')
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def taxonomy_without_files(monkeypatch):
    names = {'idm': 'IDM', 'intelligent dance music': 'IDM',
             'psychedelic trance': 'Psytrance', 'psytrance': 'Psytrance'}
    taxonomy = story_rules.StoryTaxonomy(canonical=names,
        families={'shoegaze': 'rock-indie', 'psytrance': 'trance'},
        family_names={'rock-indie', 'indie & alternative rock'})
    names.clear()
    monkeypatch.setattr(fao, 'read', lambda *a, **kw: pytest.fail('Story decision acquired a file'))
    return taxonomy


def test_story_taxonomy_preserves_equivalent_identity_specificity_and_fallback_tint(taxonomy_without_files):
    taxonomy = taxonomy_without_files
    assert story_rules._usable_article({'kind': 'genre', 'display': 'idm',
        'title': 'Intelligent dance music'}, taxonomy=taxonomy)
    assert not story_rules._usable_article({'kind': 'genre', 'display': 'Dub techno',
        'title': 'Techno'}, taxonomy=taxonomy)
    assert not story_rules._specific_genre('Indie & Alternative Rock', taxonomy=taxonomy)
    assert story_rules._specific_genre('Shoegaze', taxonomy=taxonomy)
    assert story_rules._genres_in_token({'Psytrance': 2, 'Psychedelic Trance': 3, 'Trance': 4},
        'psytrance', taxonomy=taxonomy) == ['Psytrance', 'Psychedelic Trance']
    assert story_rules.decorate({'kind': 'genre', 'subject': 'genre:shoegaze', 'display': 'Shoegaze'},
        representative={'artist': 'Example'}, taxonomy=taxonomy) == {
            'color': 'var(--genre-rock-indie)', 'thumbnail': None, 'seed': 'Example', 'genre': 'shoegaze'}


def test_story_rank_and_familiarity_use_captured_canonical_names(taxonomy_without_files):
    taxonomy = taxonomy_without_files
    subjects = story_rules.ranked_subjects(leans={'genre:psytrance': 1},
        activity={'artist': {}, 'genre': {'Psychedelic Trance': {}}, 'recordings': {}},
        favorites=[], genre_base={}, axes={'Psychedelic Trance': ('trance', 'psytrance')}, taxonomy=taxonomy)
    assert subjects == [{'kind': 'genre', 'subject': 'genre:psytrance', 'display': 'psytrance'}]
    assert story_rules._familiar_genres({'Psytrance': 6, 'Psychedelic Trance': 6},
        taxonomy=taxonomy) == {'psytrance'}


def test_story_controller_shares_supplied_taxonomy_during_grouping_and_serving(taxonomy_without_files):
    taxonomy = taxonomy_without_files
    genre = {'kind': 'genre', 'subject': 'genre:idm', 'display': 'idm'}
    artist = {'kind': 'artist', 'subject': 'artist:Example', 'display': 'Example'}
    charts = NS(story_activity=lambda _: {'recordings': {
        'Example': [{'title': 'Song', 'genre': 'Intelligent Dance Music'}]}})
    owner = StoryPreparation(charts=charts, genres=None, wiki=NS(get=lambda _: None),
        proposals=None, ledger=None, leans=None, taxonomy=taxonomy)
    assert owner.connected([artist, genre]) == [[genre, artist]]
    card = {**genre, 'title': 'Intelligent dance music', 'extract': 'A genre.', 'thumbnail': 'image',
            'color': 'tint', 'seed': 'Example', 'genre': 'idm', 'depth': 2, 'lead_allowed': True}
    owner.rec_proposals = NS(get_proposals=lambda _: {
        'schema': story_rules.SCHEMA, 'cards': [card], 'built_at': NOW})
    cards, pending = owner.load(NOW)
    assert cards == [card] and not pending
    assert owner.groups(cards) == [[card]]


def inputs(**changes):
    return listening.ListeningInputs(**dict(names={}, catalog=(), radio_ids=(), timezone='UTC',
        mixes=(), videos={}, player=(), observations=(), plays=(), rating_actions=(), feedback=()) | changes)


def queries(**changes):
    ports = dict(reader=Mock(), listening=Mock(), artists=Mock(), collection=Mock(), provider=Mock(),
        settings=Mock(), capture=Mock(return_value=None), now=lambda: NOW, refresh_analytics=Mock())
    ports.update(changes)
    return AnalyticsQueries(**ports)


def test_artist_canonical_channel_binds_provider_and_catalog_reads_without_mutating_provider_data():
    provider = {'browse_id': 'channel', 'albums': [{'browse_id': 'alias', 'title': 'Owned'},
                {'browse_id': 'other', 'title': 'Other', 'year': '2020'}]}
    saved = deepcopy(provider)
    reader = NS(charts=Mock(), collection=Mock())
    reader.charts.artist_songs.return_value = [{'plays': 3}]
    reader.collection.saved_album_ids.return_value = {'owned'}
    reader.collection.artist_albums.return_value = [{'browse': 'owned', 'year': '2000'}]
    reader.collection.saved_album_owners.return_value = {'owned': ['main']}
    reader.collection.canonical_album_ids.return_value = {'owned': 1, 'alias': 1, 'other': 2}
    owner = queries(reader=lambda: nullcontext(reader), provider=Mock(return_value=provider))
    owner.artists.get_artist.return_value = {'id': 7, 'name': 'Canonical', 'provider_ids': {'youtube': 'channel'}}
    result = analytics_views.artist(owner.artist('Ambiguous', artist_id='7'))
    owner.provider.assert_called_once_with('Canonical', 'channel')
    reader.charts.artist_songs.assert_called_once_with(7, None)
    assert [a['title'] for a in result['yt_albums']] == ['Other'] and result['total_plays'] == 3
    assert provider == saved


def test_missing_artist_never_contacts_provider_or_opens_report_reader():
    owner = queries()
    owner.artists.get_artist.return_value = None
    owner.artists.resolve_exact.return_value = None
    assert owner.artist('') is None
    owner.provider.assert_not_called()
    owner.reader.assert_not_called()


def test_rankings_capture_bounded_windows_then_present_without_queries():
    chart = Mock()
    chart.history_bounds.return_value = (NOW - 40 * 86400, NOW)
    chart.corpus_distribution.return_value = {'A': 2}
    chart.listen_distribution.return_value = {'A': 1}
    chart.ticker_thumbnails.return_value = {}
    chart.album_browse_ids.return_value = {}
    chart.album_labels.return_value = {}
    chart.playlist_labels.return_value = {}
    chart.artist_labels.return_value = {}
    chart.top_tracks.return_value = []
    owner = queries(reader=lambda: nullcontext(NS(charts=chart, playlists=NS(get_playlists=lambda: []))))
    facts = owner.rankings(7)
    assert chart.listen_distribution.call_count == 20
    chart.top_tracks.assert_called_once_with(100, since=NOW - 7 * 86400)
    chart.reset_mock()
    result = analytics_views.rankings(facts)
    assert result['tickers']['genre']['close_days'] == 7
    assert result['tickers']['genre']['span_days'] == 28
    assert not chart.mock_calls


def test_timezone_validation_precedes_write_and_refresh():
    owner = queries()
    for value in (None, '', 'Invalid/Timezone', 'x' * 101):
        with pytest.raises((ValueError, ZoneInfoNotFoundError)):
            owner.timezone(value)
    owner.settings.set_setting.assert_not_called()
    owner.refresh_analytics.assert_not_called()
    assert owner.timezone('America/Costa_Rica') == 'America/Costa_Rica'
    owner.settings.set_setting.assert_called_once_with('listening_timezone', 'America/Costa_Rica')
    owner.refresh_analytics.assert_called_once_with()


def test_exposure_uses_current_edition_and_only_visible_story_families():
    queue = Mock()
    owner = queries(capture=lambda: queue)
    owner.listening.prepared.return_value = {'id': 'current', 'stories': [
        {'id': 'a', 'family': 'return'}, {'id': 'b', 'family': 'discovery'}]}
    assert owner.impressions('old', object())  # Stale editions have always been ignored.
    queue.enqueue.assert_not_called()
    assert not owner.impressions('current', ['a'] * 5)
    assert owner.impressions('current', ['a', 'unknown'])
    assert queue.enqueue.call_args.args[2] == {'a', 'family:return', 'hero:a'}


def test_story_query_refresh_and_viewed_command_preserve_pause():
    owner = StoryQueries(proposals=Mock(), settings=Mock(), refresh=Mock(), paused=lambda: True)
    owner.proposals.get_proposals.return_value = {'story': {'month': '2026-09', 'beats': [{'groups': []}]}}
    assert owner.story('2026-08') is None
    owner.refresh.assert_not_called()
    assert owner.story('2026-09')['month'] == '2026-09'
    owner.refresh.assert_called_once_with()
    owner.viewed('2026-09')
    owner.settings.set_setting.assert_not_called()


def test_detached_listening_batches_split_midnight_and_keep_input_metadata_unchanged():
    row = {'identity_key': 'k', 'artist_id': 1, 'artist': 'Old', 'genre': 'Jazz'}
    events = [{'account_id': 1, 'video_id': 'v', 'playlist_ytm_id': 'radio', 'kind': 'tick',
               'position': position, 'at': NOW - 43200 + offset, 'payload': '{}'}
              for position, offset in ((0, -10), (30, 20))]
    result = listening.calculate(inputs(names={'1': 'Canonical'}, catalog=([row],), radio_ids={'radio'},
        videos={'v': 'k'}, player=([events[0]], [events[1]])), NOW)
    assert [(r[0], r[1], r[3], r[result['columns'].index('seconds')]) for r in result['rows']] == [
        ('2026-10-23', 'Canonical', 'radio', 10), ('2026-10-24', 'Canonical', 'radio', 20)]
    assert row['artist'] == 'Old' and 'cohort' not in row


@pytest.mark.parametrize('current', [False, True])
def test_listening_publication_is_guarded_and_uses_complete_prepared_outputs(current):
    analytics = Mock()
    analytics.artist_names.return_value = analytics.video_keys.return_value = {}
    analytics.mix_playlist_ids.return_value = []
    for name in ('catalog_rows', 'player', 'observations', 'plays', 'rating_actions', 'feedback'):
        getattr(analytics, name).return_value = iter(())
    analytics.prepared.return_value = {}
    analytics.impressions.return_value = {}
    prepared, published = Mock(), Mock()
    source = NS(analytics=analytics, playlists=NS(radio_playlist_ids=lambda: []),
        settings=NS(get_setting=lambda _: 'UTC'), revision='source-generation',
        publish=lambda action: (action(), True)[1] if current else False)
    owner = ListeningPreparation(snapshot=lambda: nullcontext(source), prepare=prepared, publish=published)
    result = owner.build(NOW)
    analytics.prepare_generation.assert_not_called()
    plan = prepared.call_args
    assert plan.kwargs['input_revision'] == 'source-generation'
    assert json.loads(plan.args[1])['available'] is False
    assert json.loads(plan.args[2])['stories'] == []
    assert (result is not None) is current
    assert published.call_count == int(current)
    if current:
        published.assert_called_once_with(prepared.return_value, 'UTC')


@pytest.mark.parametrize('current,accepted', [(False, False), (True, False), (True, True)])
def test_trend_publication_uses_final_ordered_guard(monkeypatch, current, accepted):
    order = []
    source = NS(is_current=lambda: current)
    def ordered(action):
        order.append('guard')
        if accepted:
            action()
        return accepted
    source.publish_ordered = ordered
    monkeypatch.setattr(trend_preparation, 'calculate', lambda *_: {'built_at': NOW, 'story': None})
    def publish(encoded, now):
        assert order == ['guard'] and json.loads(encoded)['built_at'] == now == NOW
        order.append('publish')
    owner = trend_preparation.TrendPreparation(snapshot=lambda: nullcontext(source), publish=publish)
    assert (owner.build(NOW) is not None) is (current and accepted)
    assert order == (['guard', 'publish'] if accepted else ['guard'] if current else [])


def test_story_provider_work_follows_cached_publication_and_snapshot_release():
    order, rows = [], {}
    charts = NS(corpus_distribution=lambda _: {}, top_artists=lambda **_: [],
        story_activity=lambda _: {'artist': {'A': {}}, 'genre': {}, 'recordings': {}},
        listen_distribution=lambda *a, **kw: {}, artist_thumbnail=lambda _: None)
    wiki = NS(get=rows.get, is_fresh=lambda *_: False, should_refresh=lambda key, row, now: row is None,
        put=lambda key, kind, display, payload, now: rows.update({key: {'found': 1, **payload}}))
    @contextmanager
    def snapshot():
        order.append('open')
        try:
            yield NS(inputs=owner, revision='r', publish=lambda action: (action(), True)[1])
        finally:
            order.append('close')
    def fetch(subject, now, fn):
        assert order[-2:] == ['publish', 'close']
        order.append('fetch')
        return {'title': 'A', 'extract': 'A is a musician.', 'url': 'article', 'thumbnail': None}
    owner = StoryPreparation(charts=charts, genres=NS(feature_axes=lambda _: {}), wiki=wiki,
        proposals=NS(get_proposals=lambda _: {}, prepare_home=lambda *a, **kw: nullcontext('prepared'),
                     publish_home=lambda *a, **kw: order.append('publish')),
        ledger=NS(story_views=lambda: {}), leans=lambda _: {'artist:A': 1}, snapshot=snapshot, fetch=fetch)
    assert not owner.prepare(NOW)
    assert order == ['open', 'publish', 'close', 'fetch', 'open', 'publish', 'close']


def test_story_acquisition_limits_calls_and_retains_cached_summaries_on_unavailable_provider():
    wiki, fetched = Mock(), []
    wiki.get.return_value = {'title': 'A', 'extract': 'Previous', 'found': 1}
    wiki.should_refresh.return_value = True
    def fetch(subject, now, fn):
        fetched.append(subject)
        exc = LookupUnavailable('unavailable')
        exc.retry_at = NOW + 60
        raise exc
    owner = StoryPreparation(charts=None, genres=None, wiki=wiki, proposals=None, ledger=None, leans=None, fetch=fetch)
    subjects = [{'kind': 'artist', 'display': 'A', 'subject': f'artist:{i}'} for i in range(8)]
    assert owner.prewarm(NOW, subjects=subjects, fetch_limit=3) == 3
    assert len(fetched) == 3 and wiki.defer.call_count == 3
    wiki.put.assert_not_called()


def test_mode_acquisition_keeps_existing_modes_when_inputs_are_too_small():
    model, modes = Mock(), Mock()
    model.content_generation.return_value = {'id': 'new'}
    owner = TasteModes(model=model, modes=modes, axes=Mock(), parameter=lambda _: 80,
                       space=lambda _: 'new-space', vectors=lambda _: ([], None, {}))
    assert owner.prepare(NOW) is None
    modes.list_modes.assert_not_called()
    modes.next_mode_id.assert_not_called()


def test_engine_query_needs_only_three_scalar_inputs():
    owner = TasteReadings(queries=NS(model=NS(rec_vectors_count=lambda: 7),
        settings=NS(get_setting=lambda _: None)), analysis=NS(), parameter=None,
        configuration=NS(training_dim=32))
    assert owner.engine_panel() == {'vectors': 7, 'dim': 32, 'method': 'auto'}


def test_personality_and_lane_models_accept_detached_facts():
    personality = recap_story.personality({'plays': 20, 'new_artist_plays': 20, 'diversity': 1},
        {'k': ('A', 'techno')}, track_plays={'k': 20}, audio={}, hours=[0] * 23 + [20],
        genre_axes={'techno': ('techno', None)})
    assert personality['name'] == 'Midnight Voyager' and personality['top_family'] == 'techno'
    assert 'hue' not in personality['aura']
    result = taste_display.source_weights({'lane:x': .7}, {'lane:x': {'weight': 1., 'updated_at': NOW - 86400}},
        NOW, halflife=7, lane_definitions=[('x', 'X', '')], lane_default=.5)
    assert result['lanes'][0]['age_days'] == 1 and result['lanes'][0]['fade_delta'] == pytest.approx(-.3)
