"""Sync/import decisions and lifecycle contracts without a Store, SQLite or ASGI."""
import asyncio
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.history_imports import HistoryImports
from yt_playlist.controllers.history_loading import SpotifyHistory, TakeoutHistory
from yt_playlist.controllers.spotify_library import recording_match, album_candidates, SpotifyLibrary
from yt_playlist.controllers.spotify_transfer import SpotifyTransfer, TransferReports, TransferRejected
from yt_playlist.controllers.sync_commands import SyncCommands
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.transfer_previews import TransferPreviews
from yt_playlist.library.history_matching import takeout_rows
from yt_playlist.library.spotify_transfer_policy import retained_report
from yt_playlist.library.sync_outcomes import Schedule, due_facts

pytestmark = pytest.mark.usefixtures('no_database')


class Jobs:
    def __init__(self):
        self.rows = []

    def create(self, *, kind):
        job = NS(id=len(self.rows) + 1, kind=kind, events=[], done=False, result=None, error=None, outcome=None)
        self.rows.append(job)
        return job

    def latest(self, kind, parameters):
        return next((j for j in reversed(self.rows) if j.kind == kind
                     and (parameters is None or j.parameters == parameters)), None)


class Work:
    def __init__(self):
        self.pending, self.active = [], 0

    def start_thread(self, target, **kwargs):
        self.pending.append(target)

    def admit(self, label):
        self.active += 1
        released = False
        def release():
            nonlocal released
            if not released:
                released = True
                self.active -= 1
        return release

    @contextmanager
    def entered(self, release):
        try:
            yield
        finally:
            release()

    async def to_thread(self, target, *args):
        return target(*args)


def test_manual_and_recurring_sync_share_lock_and_connection_retry():
    jobs, work, lock, due = Jobs(), Work(), threading.Lock(), Mock(return_value=True)
    execution = Mock()
    execution.run.return_value = NS(result={'status': 'complete'}, outcome='complete', error=None)
    owner = SyncCommands(jobs, lock, work, lambda: execution, due=due, state=Mock(), enabled=lambda: True)
    manual = owner.start()
    assert len(jobs.rows) == 1 and not manual.done
    with lock:
        assert owner.tick(configured=True, connected=True, connection='new', max_age_s=100) is None
    assert owner.last_connection is None and work.active == 0
    work.pending.pop()()
    assert manual.done and manual.outcome == 'complete'
    automatic = owner.tick(configured=True, connected=True, connection='new', max_age_s=100)
    assert automatic.done and work.active == 0 and not lock.locked()
    assert execution.run.call_args.kwargs == dict(automatic=True, retry_now=True, max_age_s=100)


def test_sync_releases_admission_and_lock_when_job_creation_fails():
    work, lock, jobs = Work(), threading.Lock(), Mock()
    jobs.create.side_effect = RuntimeError('job unavailable')
    owner = SyncCommands(jobs, lock, work, Mock(), due=lambda *a, **kw: True,
                         state=Mock(), enabled=lambda: True)
    with pytest.raises(RuntimeError, match='job unavailable'):
        owner.tick(configured=True, connected=True, connection='one', max_age_s=10)
    assert work.active == 0 and not lock.locked() and owner.last_connection is None


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('error', [WorkClosing('shutdown'), RuntimeError('unexpected failure')])
def test_sync_only_handles_expected_cancellation(automatic, error):
    jobs, work, lock = Jobs(), Work(), threading.Lock()
    execution = Mock()
    execution.run.side_effect = error
    owner = SyncCommands(jobs, lock, work, lambda: execution, due=lambda *a, **kw: True,
                         state=Mock(), enabled=lambda: True)

    def run():
        if automatic:
            owner.tick(configured=True, connected=True, connection='one', max_age_s=10)
        else:
            owner.start()
            work.pending.pop()()

    if isinstance(error, WorkClosing):
        run()
        assert jobs.rows[0].outcome == 'cancelled'
    else:
        with pytest.raises(RuntimeError) as caught:
            run()
        assert caught.value is error
    assert jobs.rows[0].done and jobs.rows[0].result is None
    assert work.active == 0 and not lock.locked()


def test_schedule_uses_supplied_state_and_preserves_failed_attempt_backoff():
    failed = dict(status='failed', retry_at=400, last_success_at=1)
    schedule = Schedule({}, 399, automatic=True)
    assert not schedule.due(failed)
    assert Schedule({}, 399, automatic=True, retry_now=True).due(failed)
    assert not due_facts({}, set(), '100', None, 399, 1000)
    assert due_facts({}, set(), '100', None, 400, 1000)
    assert due_facts({}, set(), '100', None, 101, 1000, retry_now=True)
    assert failed == dict(status='failed', retry_at=400, last_success_at=1)


def transfer_owner():
    work, lock = Work(), threading.Lock()
    stored = [None]
    reports = Mock(spec_set=['lock', 'save', 'current', 'delete'])
    reports.lock = lock
    reports.save.side_effect = lambda value: stored.__setitem__(0, deepcopy(value))
    reports.current.side_effect = lambda: deepcopy(stored[0])
    reports.delete.side_effect = lambda: stored.__setitem__(0, None)
    identity, client = NS(id=7, label='Destination'), object()
    destination = Mock(return_value=(identity, client))
    parsed = dict(playlists=[dict(name='Road songs', tracks=[])], albums=[])
    transfer = Mock(return_value=dict(tracks_imported=1, albums_imported=0))
    owner = SpotifyTransfer(reports, TransferPreviews(), lambda: [identity], work,
        destination=destination, parse=Mock(return_value=parsed),
        prepare=Mock(side_effect=lambda selected, client, account: selected), transfer=transfer,
        candidates=Mock(), save_album=Mock(), imported=Mock(), now=lambda: 100,
        logger=Mock(), report_schema=6)
    return owner, work, stored


def ready_preview(owner, work):
    token = owner.select(b'export')['selection_token']
    owner.prepare(token, 7, ['0'], False)
    assert owner.reports.lock.locked()
    work.pending.pop()()
    assert not owner.reports.lock.locked()
    return owner.reports.current()['preview_token']


def test_transfer_consumes_each_preview_once_and_keeps_exact_destination():
    owner, work, stored = transfer_owner()
    ready = ready_preview(owner, work)
    owner.confirm(ready, [])
    with pytest.raises(TransferRejected) as caught:
        owner.confirm(ready, [])
    assert caught.value.reason == 'expired'
    assert len(work.pending) == 1
    work.pending.pop()()
    assert stored[0]['status'] == 'done' and not owner.reports.lock.locked()
    assert owner.transfer.call_args.args[2] == 7
    owner.imported.assert_called_once_with(100)
    assert owner.destination.call_args.args == (7,)


def test_destination_rejection_preserves_preview_and_prevents_write():
    owner, work, _ = transfer_owner()
    ready = ready_preview(owner, work)
    owner.destination.side_effect = TransferRejected('unavailable', 'Destination disconnected')
    with pytest.raises(TransferRejected):
        owner.confirm(ready, [])
    assert owner.snapshot(ready, 'ready')['account_id'] == 7
    owner.transfer.assert_not_called()
    assert not owner.reports.lock.locked() and work.pending == []


def test_invalid_album_candidate_does_not_consume_ready_preview():
    owner, work, _ = transfer_owner()
    owner.parse.return_value['albums'] = [dict(title='Album', artist='Artist', uri='source',
        resolution=dict(match=None, candidates=[dict(browse_id='allowed')]))]
    token = owner.select(b'export')['selection_token']
    owner.prepare(token, 7, [], True)
    work.pending.pop()()
    ready = owner.reports.current()['preview_token']
    with pytest.raises(TransferRejected, match='from this preview'):
        owner.confirm(ready, [('save', 'unreviewed')])
    assert owner.snapshot(ready, 'ready') and not owner.reports.lock.locked()
    owner.transfer.assert_not_called()


def test_submission_failure_releases_lock_and_retains_recoverable_job():
    owner, work, stored = transfer_owner()
    token = owner.select(b'export')['selection_token']
    work.start_thread = Mock(side_effect=RuntimeError('thread failed'))
    with pytest.raises(RuntimeError, match='thread failed'):
        owner.prepare(token, 7, [0], False)
    assert not owner.reports.lock.locked() and stored[0]['status'] == 'running'
    with pytest.raises(TransferRejected):
        owner.snapshot(token, 'selection')


def test_cancel_removes_only_its_preview_report():
    owner, work, stored = transfer_owner()
    ready = ready_preview(owner, work)
    owner.cancel(ready)
    assert stored[0] is None
    with pytest.raises(TransferRejected):
        owner.snapshot(ready, 'ready')


@pytest.mark.parametrize('replacement', [None, {'status': 'done'}, {'status': 'preview', 'preview_token': 'new'}])
def test_restart_recovery_rechecks_under_lock_before_publishing(replacement):
    settings, publish = Mock(), Mock()
    settings.spotify_import_job.side_effect = [{'status': 'running'}, replacement]
    reports = TransferReports(settings, threading.Lock(), now=lambda: 100, publish_recovery=publish)
    assert reports.current() == replacement
    publish.assert_not_called()


def test_restart_projects_interrupted_job_and_retention_leaves_live_report_intact():
    settings, publish = Mock(), Mock()
    settings.spotify_import_job.return_value = dict(status='running', account_id=7)
    reports = TransferReports(settings, threading.Lock(), now=lambda: 100, publish_recovery=publish)
    recovered = reports.current()
    assert recovered['status'] == 'error' and recovered['account_id'] == 7 and recovered['finished_at'] == 100
    publish.assert_called_once_with(recovered)
    original = dict(report_schema=6, report=dict(details=dict(unmatched_tracks=[{'private': 'metadata'}], albums=[])))
    assert retained_report(original)['report']['details']['unmatched_tracks'] == []
    assert original['report']['details']['unmatched_tracks'] == [{'private': 'metadata'}]


def test_recording_matching_rejects_versions_ambiguous_and_unavailable_results():
    row = dict(title='Song', artist='Artist')
    result = dict(videoId='studio', title='Song', artists=[dict(name='Artist')])
    assert recording_match([result], row)['match']['videoId'] == 'studio'
    assert recording_match([{**result, 'title': 'Song (Live)'}], row)['status'] == 'unmatched'
    assert recording_match([result, {**result, 'videoId': 'duplicate'}], row)['status'] == 'ambiguous'
    assert recording_match([{**result, 'isAvailable': False}], row)['status'] == 'unavailable'
    candidates = album_candidates([dict(browseId='edition', title='Album Deluxe Edition',
        artists=[dict(name='The Artist')], year='2000')], dict(title='Album', artist='Artist', year='2001'))
    assert len(candidates) == 1 and not candidates[0]['confident']


def test_takeout_exact_video_never_falls_back_to_another_recording():
    parsed = [dict(video_id='missing-upload', title='Song', artist='Artist', ts=10),
              dict(video_id=None, title='Song', artist='Artist', ts=20)]
    matched, unmatched = takeout_rows(parsed, {'owned'}, {}, {('Song', 'Artist'): 'owned'})
    assert matched == [('owned', None, 20)] and unmatched == {'Artist': 1}


def test_spotify_loading_captures_identity_before_parse_and_matches_credit_once():
    identities = [NS(id=7, is_master=True)]
    row = dict(title='Song', artist='Artist', spotify_uri='spotify:track:exact', ts=10)
    def parse(raw):
        identities[:] = [NS(id=8, is_master=True)]
        return [row, {**row, 'ts': 20}], {'eligible': 2}
    history, match = Mock(), Mock(return_value='owned')
    history.import_spotify_listens.return_value = (2, 2, 2)
    history.spotify_unmatched_artists.return_value = {}
    command = SpotifyHistory(accounts=lambda: identities, parse=parse, library_keys=lambda: {'owned'},
        match_recording=match, history=history)
    report = command.run(b'export')
    assert report['identity_id'] == 7 and report['matched'] == 2
    match.assert_called_once_with('Song', 'Artist')
    assert history.import_spotify_listens.call_args.args == (7, [
        ('spotify:track:exact', 10, 'Artist', 'owned'), ('spotify:track:exact', 20, 'Artist', 'owned')])


def test_history_job_uses_captured_import_identity_and_persists_aggregates_only():
    report = dict(identity_id=7, matched=1, unmatched=2, plays_added=1, events_added=1,
                  span_days=800, unmatched_artists={'Private Artist': 3})
    seed, settings, refresh = Mock(return_value=1), Mock(), Mock()
    owner = HistoryImports(jobs=Jobs(), lock=threading.Lock(), model_lock=nullcontext, work=Work(),
        settings=settings, history=Mock(), importers={'spotify': Mock(return_value=report)},
        seed_spotify=seed, seed_takeout=Mock(), resolve_spotify=Mock(side_effect=AssertionError('recapture')),
        refresh=refresh, now=lambda: 100)
    result = asyncio.run(owner.run('spotify', b'private export'))
    assert result['seeded'] == 1 and 'unmatched_artists' not in result['report']
    assert seed.call_args.args[:2] == (7, {'Private Artist': 3})
    key, summary = settings.set_setting.call_args_list[-1].args
    assert key == 'spotify_import_report' and 'Private Artist' not in summary
    assert json.loads(summary)['matched'] == 1
    assert owner.status('spotify')['outcome'] == 'complete'
    refresh.assert_called_once()


def test_history_admission_rejection_completes_job_without_importing():
    work, importer = Work(), Mock()
    async def closed(*args):
        raise WorkClosing('closing')
    work.to_thread = closed
    owner = HistoryImports(jobs=Jobs(), lock=threading.Lock(), model_lock=nullcontext, work=work,
        settings=Mock(), history=Mock(), importers={'spotify': importer}, seed_spotify=Mock(),
        seed_takeout=Mock(), resolve_spotify=Mock(), refresh=Mock(), now=lambda: 100)
    with pytest.raises(WorkClosing):
        asyncio.run(owner.run('spotify', b'export'))
    assert owner.status('spotify')['done'] and owner.status('spotify')['outcome'] == 'failed'
    importer.assert_not_called()


def test_takeout_loading_preserves_unknown_exact_upload_and_auditable_source():
    rows = [dict(video_id='unknown', title='Song', artist='Artist', ts=10),
            dict(video_id=None, title='Song', artist='Artist', ts=20)]
    history, match, video = Mock(), Mock(return_value='owned'), Mock(return_value=None)
    history.import_listening_events.return_value = (1, 1)
    history.record_takeout_import.return_value = 15
    command = TakeoutHistory(identity=lambda: 7, parse=lambda raw: (rows, 2),
        library_keys=lambda: {'owned'}, video_key=video, match_recording=match, history=history, now=lambda: 100)
    report = command.run(b'export')
    assert report['matched'] == 1 and report['unmatched'] == 1 and report['import_id'] == 15
    match.assert_called_once_with('Song', 'Artist')
    history.import_listening_events.assert_called_once_with(7, [('owned', None, 20)])
    digest, account, now, captured, unparsed = history.record_takeout_import.call_args.args
    assert len(digest) == 64 and (account, now, unparsed) == (7, 100, 2) and captured == rows


def test_spotify_library_preview_uses_explicit_ports_without_mutation_or_duplicate_search():
    accounts = Mock(spec_set=['get_accounts'])
    accounts.get_accounts.return_value = [NS(id=7)]
    collection = Mock(spec_set=['get_saved_albums'])
    collection.get_saved_albums.return_value = []
    settings = Mock(spec_set=['get_setting'])
    settings.get_setting.return_value = None
    claim, save, playlists = Mock(), Mock(), Mock(spec_set=[])
    command = SpotifyLibrary(accounts=accounts, collection=collection, playlists=playlists,
        settings=settings, claim=claim, save_album=save)
    song = dict(title='Song', artist='Artist', album='', uri='spotify:track:one')
    parsed = dict(playlists=[dict(name='Mix', tracks=[song, song])], albums=[])
    client = Mock(spec_set=['search'])
    client.search.return_value = [dict(videoId='exact', title='Song', artists=[dict(name='Artist')])]
    prepared = command.prepare(parsed, client, 7)
    assert [row['match']['videoId'] for row in prepared['playlists'][0]['resolution']] == ['exact', 'exact']
    client.search.assert_called_once()
    collection.get_saved_albums.assert_called_once_with(account_id=7)
    with pytest.raises(ValueError, match='not part of this report'):
        command.resolve(client, dict(candidates=[]), 'unreviewed', account_id=7)
    claim.assert_not_called()
    save.assert_not_called()
