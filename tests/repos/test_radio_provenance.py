from yt_playlist.repos.base import GENERATED_GROUP


def _ident(store):
    return store.accounts.upsert_account("main", "ref", None, True)


def test_radio_playlist_play_is_quarantined_by_real_list_id(store):
    ident = _ident(store)
    # The app-managed radio playlist, tagged Generated (as create_generated_playlist does).
    store.playlists.upsert_playlist(ident, "PLRADIO", "TuneConsole Radio", 0, "", 100000.0)
    store.playlists.set_playlist_group("PLRADIO", GENERATED_GROUP)
    store.playlists.mark_generated_playlist('PLRADIO')
    # A play carrying the playlist's real ytm id as provenance.
    store.history.record_play_event(ident, "k1", "v1", 100000.0, playlist_ytm_id="PLRADIO", source_timezone='UTC')
    assert store.rec_evidence.generated_only_keys_between(86400, 2 * 86400) == {"k1"}


def test_real_non_generated_play_not_quarantined(store):
    ident = _ident(store)
    store.history.record_play_event(ident, "k2", "v2", 100000.0, playlist_ytm_id=None)
    store.history.record_play_event(ident, "k3", "v3", 100000.0, playlist_ytm_id="PLreal")
    assert store.rec_evidence.generated_only_keys_between(86400, 2 * 86400) == set()
