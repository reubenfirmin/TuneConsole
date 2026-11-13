"""Home page-model helpers extracted from web/routes/home.py: visit accounting,
the recap notice and radio launch-playlist attachment."""
from tests.conftest import seed_catalog_track
from yt_playlist.rec import home_prepared


def test_with_launch_playlist_is_a_noop_without_a_launch_id(store):
    bootstrap = {'state': {}}
    assert home_prepared.with_launch_playlist(store, bootstrap, None) is bootstrap
    assert 'launchPlaylist' not in bootstrap


def test_with_launch_playlist_ignores_an_unknown_or_malformed_id(store):
    for bad in ('not-a-number', '99999999999999999999'):
        bootstrap = {'state': {}}
        home_prepared.with_launch_playlist(store, bootstrap, bad)
        assert 'launchPlaylist' not in bootstrap


def test_with_launch_playlist_attaches_available_tracks_only(store):
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    track = seed_catalog_track(store, 'song|artist', 'Song', 'Artist', video_id='v1')
    pid = store.playlists.upsert_playlist(iid, 'PL1', 'My Playlist', 1, 'h', 1.0)
    store.playlists.set_playlist_tracks(pid, [track])
    bootstrap = home_prepared.with_launch_playlist(store, {'state': {}}, str(pid))
    assert bootstrap['launchPlaylist']['id'] == pid
    assert bootstrap['launchPlaylist']['title'] == 'My Playlist'
    assert [t['video_id'] for t in bootstrap['launchPlaylist']['tracks']] == ['v1']


NOVEMBER_2026 = 1794700800.0  # 2026-11-15, so October 2026 is the last complete month


def test_recap_notice_is_none_without_a_completed_sync(store):
    store.rec_proposals.put_proposals('trend_rollups', {'story': {'month': '2026-10', 'month_name': 'October'}}, 1.0)
    assert home_prepared.recap_notice(store, NOVEMBER_2026) is None  # no last_sync_at yet


def test_recap_notice_appears_once_synced_and_disappears_once_viewed_or_dismissed(store):
    store.rec_proposals.put_proposals('trend_rollups', {'story': {'month': '2026-10', 'month_name': 'October'}}, 1.0)
    store.settings.set_setting('last_sync_at', '1')
    now = NOVEMBER_2026
    assert home_prepared.recap_notice(store, now) == {'month': '2026-10', 'month_name': 'October'}
    store.settings.set_setting('recap_viewed_month', '2026-10')
    assert home_prepared.recap_notice(store, now) is None
    store.settings.delete_setting('recap_viewed_month')
    store.settings.set_setting('recap_dismissed_month', '2026-10')
    assert home_prepared.recap_notice(store, now) is None


def test_record_visit_counts_without_raising(store):
    home_prepared.record_visit(store, 1000.0)


def _fail():
    raise AssertionError('mixes_ready must not be called')


def test_card_record_gate_rejects_other_paths_without_calling_mixes_ready():
    assert home_prepared.card_record_gate('/home/other', activity_paused=False,
        read_only=False, mixes_ready=_fail) is False


def test_card_record_gate_rejects_while_paused_or_read_only_without_calling_mixes_ready():
    assert home_prepared.card_record_gate('/', activity_paused=True,
        read_only=False, mixes_ready=_fail) is False
    assert home_prepared.card_record_gate('/', activity_paused=False,
        read_only=True, mixes_ready=_fail) is False


def test_card_record_gate_defers_to_mixes_ready_only_for_the_root_path():
    assert home_prepared.card_record_gate('/', activity_paused=False,
        read_only=False, mixes_ready=lambda: True) is True
    assert home_prepared.card_record_gate('/', activity_paused=False,
        read_only=False, mixes_ready=lambda: False) is False


def test_card_record_gate_other_allowed_paths_skip_mixes_ready():
    for path in ('/home/cards', '/home/refresh-cards'):
        assert home_prepared.card_record_gate(path, activity_paused=False,
            read_only=False, mixes_ready=_fail) is True


def test_polling_schedule_stops_once_live_or_not_waiting_or_vibe_pending():
    assert home_prepared.polling_schedule(0, live=True, waiting=True, vibe_pending=False) == (None, 3)
    assert home_prepared.polling_schedule(0, live=False, waiting=False, vibe_pending=False) == (None, 3)
    assert home_prepared.polling_schedule(0, live=False, waiting=True, vibe_pending=True) == (None, 3)


def test_polling_schedule_advances_and_caps_the_attempt_and_interval():
    assert home_prepared.polling_schedule(3, live=False, waiting=True, vibe_pending=False) == (4, 24)
    assert home_prepared.polling_schedule(9, live=False, waiting=True, vibe_pending=False) == (10, 30)


def test_with_starter_fallback_is_a_noop_once_anything_is_ready(store):
    from types import SimpleNamespace
    result = SimpleNamespace(protos=['already-ready'], slots=[], preparing=False, choice_context=None)
    assert home_prepared.with_starter_fallback(store, result, 1000.0) is result
    assert result.protos == ['already-ready']


def test_present_mix_choices_is_a_noop_without_protos_or_context(store):
    from types import SimpleNamespace
    result = SimpleNamespace(protos=[], choice_context=None, epoch=0, vibe=None)
    home_prepared.present_mix_choices(store, result, {}, 1000.0)  # must not raise
