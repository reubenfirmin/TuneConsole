"""Application enrichment commands use supplied capabilities without SQLite."""
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec
from contextlib import nullcontext
from collections import OrderedDict

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment import EnrichmentCommands
from yt_playlist.controllers.enrichment_configuration import EnrichmentConfiguration
from yt_playlist.controllers.enrichment_drain import EnrichmentBatch, EnrichmentDrain
from yt_playlist.controllers.artwork import Artwork
from yt_playlist.controllers.settings import Settings
from yt_playlist.providers import album_editorial, discogs, genres, lastfm, musicbrainz, wikipedia, waterfall
from yt_playlist.core.editorial_catalog import EditorialCatalog
from yt_playlist.providers.coverart import CoverResult
from yt_playlist.rec.album_notes import prepare_notes

pytestmark = pytest.mark.usefixtures('no_database')


def test_configuration_saves_before_waking_and_rejects_invalid_order():
    settings = create_autospec(Settings, instance=True, spec_set=True)
    settings.get_setting.return_value = None
    vocabulary = Mock(spec_set=['enabled_matcher', 'taxonomy', 'get_genre_whitelist'])
    wake = Mock()
    command = EnrichmentConfiguration(settings, vocabulary, wake=wake)
    with pytest.raises(ValueError):
        command.save([{'name': 'acousticbrainz'}, {'name': 'musicbrainz'}])
    settings.set_setting.assert_not_called()
    wake.assert_not_called()
    command.save([{'name': 'musicbrainz', 'enabled': False}])
    settings.set_setting.assert_called_once()
    wake.assert_called_once()
    assert command.query()[0]['name'] == 'musicbrainz'


def test_genre_snapshot_is_immutable_and_independent_of_later_configuration():
    settings = create_autospec(Settings, instance=True, spec_set=True)
    vocabulary = Mock(spec_set=['enabled_matcher', 'taxonomy'])
    supplied = {'ambient': 'Ambient'}
    vocabulary.enabled_matcher.return_value = supplied
    vocabulary.taxonomy.return_value = {'genres': [{'name': 'Ambient', 'key': 'ambient'}]}
    matcher = EnrichmentConfiguration(settings, vocabulary).matcher()
    supplied.clear()
    assert genres.match_tag('ambient', matcher) == 'Ambient'
    with pytest.raises(TypeError):
        matcher.canon['ambient'] = 'changed'


def test_provider_uses_supplied_vocabulary_instead_of_process_global_matcher(monkeypatch):
    vocabulary = Mock(spec_set=['enabled_matcher', 'taxonomy'])
    vocabulary.enabled_matcher.return_value = {'custom sound': 'Custom sound'}
    vocabulary.taxonomy.return_value = {'genres': [{'name': 'Custom sound', 'key': 'custom'}]}
    monkeypatch.setattr(genres, '_active', {'custom sound': 'Wrong global label'})
    monkeypatch.setattr(lastfm, 'access', lambda store: nullcontext())
    monkeypatch.setattr(lastfm, '_get', lambda query: {
        'track': {'toptags': {'tag': [{'name': 'custom sound'}, {'name': '1996'}]}}})
    result = lastfm.probe({'title': 'Song', 'artist': 'Artist'},
                          SimpleNamespace(genres=vocabulary), key='explicit-key')
    assert result.fields == {'genre': 'Custom sound', 'year': '1996'}
    vocabulary.enabled_matcher.assert_called_once_with()


def test_artist_genre_cache_cannot_reuse_a_previous_vocabulary(monkeypatch):
    from yt_playlist.rec import road_trip_candidates
    snapshots = iter([genres.GenreMatcher.from_values({'tag': 'First label'}),
                      genres.GenreMatcher.from_values({'tag': 'Second label'})])
    monkeypatch.setattr(genres, 'snapshot', lambda store: next(snapshots))
    monkeypatch.setattr(lastfm, 'api_key', lambda store: 'explicit-key')
    monkeypatch.setattr(lastfm, 'access', lambda store: nullcontext())
    monkeypatch.setattr(lastfm, '_get', lambda query: {'toptags': {'tag': [{'name': 'tag'}]}})
    monkeypatch.setattr(road_trip_candidates, '_ARTIST_GENRE_CACHE', OrderedDict())
    assert road_trip_candidates.artist_genre(object(), 'Artist') == 'First label'
    assert road_trip_candidates.artist_genre(object(), 'Artist') == 'Second label'


def test_batch_publishes_completed_track_before_the_next_provider_failure():
    queue = Mock(spec_set=['mark_recordings_enriched'])
    def waterfall(batch, config, *, on_progress, **kwargs):
        on_progress({'type': 'track', 'job_id': 1, 'fully_attempted': True})
        queue.mark_recordings_enriched.assert_called_once_with([1], 100)
        raise ValueError('later lookup failed')
    command = EnrichmentBatch(queue, lambda: [], waterfall, lambda track: None,
                              now=lambda: 100, stopped=lambda: False)
    with pytest.raises(ValueError):
        command.run([{'job_id': 1}, {'job_id': 2}], 'queue', lambda: False, lambda **kwargs: None)
    queue.mark_recordings_enriched.assert_called_once_with([1], 100)


def test_drain_prefers_visible_radio_work_and_leaves_unfinished_jobs_queued():
    settings = Mock(spec_set=['get_setting', 'set_setting'])
    settings.get_setting.side_effect = lambda key, default=None: default
    queue = Mock(spec_set=['priority_enrich_batch', 'next_enrich_batch', 'resweep_batch'])
    batch = [{'job_id': 1, 'identity_key': 'one'}, {'job_id': 2, 'identity_key': 'two'}]
    queue.priority_enrich_batch.return_value = batch
    reports = Mock(spec_set=['queue_remaining'])
    state = Mock(spec_set=['expire_provider_alternatives', 'reconcile_provider_configuration'])
    refresh, artwork = Mock(), Mock()
    command = EnrichmentDrain(settings, queue, reports, state, now=lambda: 100,
        configuration=lambda registry: {'provider': {'status': 'available'}},
        run_batch=lambda tracks, scope, stop, activity: {1}, retry_artwork=artwork,
        refresh=refresh, logger=Mock())
    assert command.drain(priority=['one', 'two']) == (1, {'one'})
    refresh.assert_called_once_with({'one'})
    queue.next_enrich_batch.assert_not_called()
    artwork.assert_not_called()


def test_matching_policies_use_supplied_envelopes_and_keep_version_qualifiers():
    recording = {'title': 'Song (Live)', 'artist-credit': [{'name': 'Artist'}]}
    assert musicbrainz._matches_recording(recording, 'Song (Live)', 'Artist')
    assert not musicbrainz._matches_recording(recording, 'Song', 'Artist')
    album = {'id': 4, 'title': 'Artist - Album'}
    assert not discogs._matches_release(album, 'Song', 'Artist')
    detail = {'artists': [{'name': 'Artist'}], 'tracklist': [{'title': 'Song'}]}
    assert discogs._matches_release(album, 'Song', 'Artist', release=detail)
    assert not discogs._matches_release(album, 'Song (Remix)', 'Artist', release=detail)
    assert wikipedia.project_summary({'type': 'standard', 'title': 'Someone Else',
        'extract': 'Artist is mentioned here.'}, 'artist', 'Artist', 'Artist') is None


def test_editorial_catalog_is_explicit_and_both_file_and_match_results_are_defensive():
    read = Mock(return_value=SimpleNamespace(bytes=b'{"entries":[{"artist":"Artist","title":"Album"}]}'))
    catalog = EditorialCatalog('/catalog.json', read=read)
    entries = catalog.entries()
    entries[0]['artist'] = 'changed'
    found = album_editorial.lookup({'artist': 'Artist', 'title': 'Album'}, catalog.entries())
    assert found['artist'] == 'Artist'
    found['title'] = 'changed'
    assert catalog.entries()[0]['title'] == 'Album'
    read.assert_called_once_with('/catalog.json')


def test_enrichment_query_captures_reports_and_live_worker_state():
    reports = Mock(spec_set=['coverage_stats', 'queue_stats', 'processed_timeline',
        'enrichment_activity_stats', 'outstanding_conflicts'])
    reports.coverage_stats.return_value = {'processed': 5}
    reports.queue_stats.return_value = {'total': 3}
    state = Mock(spec_set=['enrichment_retry_status', 'artwork_retry_status'])
    state.enrichment_retry_status.return_value = {'count': 2}
    settings = Mock(spec_set=['get_setting'])
    settings.get_setting.return_value = '1'
    worker = SimpleNamespace(activity={'state': 'waiting'}, busy=True)
    command = EnrichmentCommands(reports, None, state, None, None, settings,
                                  now=lambda: 123, worker=worker)
    result = command.query()
    assert result['state'] == 'waiting' and result['remaining'] == 3
    reports.coverage_stats.assert_called_once_with(include_discovery=True)
    reports.enrichment_activity_stats.assert_called_once_with(123)


@pytest.mark.parametrize('source,expected', [('coverart', 'archive'), ('youtube', 'retained')])
def test_artwork_retry_respects_artwork_ownership_and_records_completion(source, expected):
    state = Mock(spec_set=['due_artwork_retries', 'record_artwork_result'])
    state.due_artwork_retries.return_value = [{'entry_id': 7, 'release_ids': ['release']}]
    tracks = Mock(spec_set=['catalog_subject', 'artwork_source', 'set_track_art'])
    tracks.catalog_subject.return_value = {'id': 7, 'title': 'Song', 'thumbnail': 'retained'}
    tracks.artwork_source.return_value = source
    lookup = Mock(return_value=CoverResult('found', 'archive'))
    assert Artwork(state, tracks, lookup, CoverResult, now=lambda: 100).retry_due() == 1
    outcome = state.record_artwork_result.call_args.args[2]
    assert outcome.url == expected
    state.due_artwork_retries.assert_called_once_with(100, limit=1)
    if source == 'coverart':
        tracks.set_track_art.assert_called_once_with(7, 'archive', source='coverart')
    else:
        lookup.assert_not_called()
        tracks.set_track_art.assert_not_called()


def test_notes_command_owns_demand_provider_evidence_and_revision_bound_publication():
    album = {'browse_id': 'album', 'artist': 'Artist', 'title': 'Album'}
    notes = Mock(spec_set=['request', 'next_request', 'publish'])
    notes.next_request.return_value = album, 9
    proposals = Mock(spec_set=['get_proposals'])
    proposals.get_proposals.side_effect = lambda kind: {'album': album} if kind == 'album_note_requests' else {}
    collection = Mock(spec_set=['album_listening_summary'])
    collection.album_listening_summary.return_value = {'album': {'complete': True}}
    evidence = Mock(spec_set=['artifact'])
    evidence.artifact.return_value = 'evidence-hash'
    catalog = Mock(spec_set=['entries'])
    catalog.entries.return_value = []
    photograph = {'url': 'https://example.org/artist.jpg', 'source_url': 'https://example.org/artist',
                  'kind': 'artist', 'credit': 'Artist'}
    fetch = Mock(return_value={'evidence': [], 'retry': False, 'links': [], 'label': 'Label'})
    public_artwork = Mock(return_value=photograph)
    fallback = Mock()
    assert not prepare_notes(notes, proposals, collection, evidence, catalog, 100, [album],
        fetch_release=fetch, get_description=lambda subject: (None, {'status': 'disabled'}),
        public_artwork=public_artwork, artist_artwork=fallback)
    notes.request.assert_called_once_with([album], 100, current_only=True)
    fetch.assert_called_once_with(album)
    public_artwork.assert_called_once_with('Artist')
    fallback.assert_not_called()
    browse, note, at = notes.publish.call_args.args
    assert (browse, at, note['label'], note['artwork'], note['evidence']) == (
        'album', 100, 'Label', photograph, 'evidence-hash')
    assert notes.publish.call_args.kwargs == {'expected_request': 9}
    assert evidence.artifact.call_args.args[0]['sources'][0]['provider'] == 'deepseek'


def test_notes_command_uses_fresh_cache_without_provider_or_file_acquisition():
    album = {'browse_id': 'album', 'artist': 'Artist', 'title': 'Album'}
    notes = Mock(spec_set=['request', 'next_request', 'publish'])
    notes.next_request.return_value = album, 12
    proposals = Mock(spec_set=['get_proposals'])
    proposals.get_proposals.side_effect = lambda kind: (
        {'album': album} if kind == 'album_note_requests' else
        {'album': {'editorial_version': 7, 'retry_at': 200}})
    forbidden = Mock(side_effect=AssertionError('fresh notes must not acquire optional evidence'))
    assert not prepare_notes(notes, proposals, None, None, None, 100,
        fetch_release=forbidden, get_description=forbidden,
        public_artwork=forbidden, artist_artwork=forbidden)
    forbidden.assert_not_called()
    assert notes.publish.call_args.kwargs == {'expected_request': 12}


def test_job_command_rejoins_without_loading_tracks_or_starting_work():
    job = object()
    jobs = Mock(spec_set=['create_or_rejoin_playlist'])
    jobs.create_or_rejoin_playlist.return_value = job, False
    # A joined command requires neither storage nor a thread capability.
    assert waterfall.start_or_rejoin_enrichment(jobs, None, None, None, playlist_id=4) is job
    jobs.create_or_rejoin_playlist.assert_called_once_with(4)


def test_job_command_owns_track_acquisition_progress_and_terminal_state(monkeypatch):
    job = SimpleNamespace(id='job', events=Mock(spec_set=['append']), done=False, error=None)
    jobs = Mock(spec_set=['create_or_rejoin_album'])
    jobs.create_or_rejoin_album.return_value = job, True
    tracks = Mock(spec_set=['album_tracks_for_waterfall'])
    tracks.album_tracks_for_waterfall.return_value = [{'id': 7}]
    settings = Mock(spec_set=['get_setting'])
    settings.get_setting.return_value = None
    store = SimpleNamespace(tracks=tracks, settings=settings, genres=None)
    def run(acquired_store, acquired_tracks, config, progress):
        assert acquired_store is store and acquired_tracks == [{'id': 7}]
        assert config[0]['name'] == 'musicbrainz'
        progress({'type': 'done', 'total': 1})
    monkeypatch.setattr(waterfall, 'run_waterfall', run)
    assert waterfall.start_or_rejoin_enrichment(jobs, store, Mock(), lambda work: work(), album_browse='album') is job
    tracks.album_tracks_for_waterfall.assert_called_once_with('album')
    job.events.append.assert_called_once_with({'type': 'done', 'total': 1})
    assert job.done and job.error is None


def test_provider_acquisition_preserves_remix_scope_and_successful_audio_half():
    from yt_playlist.controllers.provider_acquisition import acousticbrainz_read, musicbrainz_genre_year
    matcher = genres.GenreMatcher.from_values({'ambient': 'Ambient'})
    get = Mock(return_value={'genres': [], 'tags': []})
    recordings = [{'id': 'recording', 'title': 'Song (Remix)',
                   'artist-credit': [{'artist': {'id': 'artist'}}]}]
    assert musicbrainz_genre_year(musicbrainz, get, recordings, title='Song (Remix)', matcher=matcher) == (None, None)
    # An artist's broad genre must not label the remixer's recording.
    get.assert_called_once_with('recording/recording', {'inc': 'genres+tags+releases+isrcs', 'fmt': 'json'})
    low = {'rhythm': {'bpm': 120}}
    read = Mock(side_effect=[low, ValueError('high-level unavailable')])
    failure = Mock()
    assert acousticbrainz_read('recording', read, failure, Mock()) == (low, {})
    assert [call.args[1] for call in read.call_args_list] == ['low-level', 'high-level']
    failure.assert_called_once()
