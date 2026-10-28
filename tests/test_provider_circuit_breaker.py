"""Provider outages and confirmed misses exercise the supported waterfall and evidence sink."""
import urllib.error

import pytest

from yt_playlist.providers import deezer, lastfm, musicbrainz
from yt_playlist.providers.waterfall import run_waterfall
from yt_playlist.util import net


def test_breaker_trips_only_after_threshold_consecutive_failures():
    b = net.CircuitBreaker(threshold=3)
    dns = urllib.error.URLError("Name or service not known")
    b.record(dns)
    b.record(dns)
    assert not b.tripped()
    b.record(dns)
    assert b.tripped()


def test_server_response_or_success_resets_the_streak():
    b = net.CircuitBreaker(threshold=2)
    b.record(urllib.error.URLError("down"))
    b.record(urllib.error.HTTPError("u", 404, "missing", {}, None))
    assert b.consecutive == 0
    b.record(urllib.error.URLError("down"))
    b.record()
    assert b.consecutive == 0


def test_throttle_and_server_errors_do_not_clear_outage_streak():
    b = net.CircuitBreaker(threshold=3)
    b.record(TimeoutError())
    b.record(urllib.error.HTTPError("u", 429, "throttled", {}, None))
    b.record(urllib.error.HTTPError("u", 503, "busy", {}, None))
    assert b.tripped()
    b.record()
    assert b.consecutive == 0


def test_parse_error_counts_as_reachable():
    b = net.CircuitBreaker(threshold=1)
    b.record(ValueError("bad json"))
    assert not b.tripped()


@pytest.mark.parametrize("provider", [musicbrainz, deezer])
@pytest.mark.parametrize("offline", [False, True])
def test_waterfall_probe_distinguishes_outages_from_confirmed_misses(
        provider, offline, store, monkeypatch):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PL", "Mix", 8, "h", 1000)
    ids = [store.tracks.upsert_track(f"v{i}", f"T{i}", "Art", None, None) for i in range(8)]
    store.playlists.set_playlist_tracks(pid, ids)
    calls = []

    import io
    from types import SimpleNamespace
    from yt_playlist.integrations.musicbrainz import MusicBrainzIAO
    from yt_playlist.integrations.deezer import DeezerIAO

    def open_request(request, **kwargs):
        calls.append(request.full_url)
        if offline:
            raise urllib.error.URLError('offline')
        return io.StringIO('{"data":[]}' if provider is deezer else '{"recordings":[]}')

    boundary = DeezerIAO if provider is deezer else MusicBrainzIAO
    monkeypatch.setattr(provider, '_iao', boundary(open_fn=open_request,
        now_fn=lambda: 1000, pacer=SimpleNamespace(wait=lambda: None)))
    monkeypatch.setattr(lastfm, "api_key", lambda *args: "key")
    events = []
    completed = run_waterfall(store, store.tracks.tracks_for_waterfall(pid),
                              [{"name": provider.name, "enabled": True}], events.append,
                              registry={provider.name: provider}, now_fn=lambda: 1000)
    attempt = store.enrichment_state.enrichment_attempts("t0|art")[provider.name]
    assert attempt["status"] == ("retry" if offline else "no_match")
    if offline:
        assert completed == []
        assert len(calls) < 8
        assert any(event.get("warning") for event in events)
        recording_id = store.tracks.track_for_waterfall(ids[0])["recording_id"]
        state = store.enrichment_state.enrichment_provider_state(recording_id)[provider.name]
        assert state["retry_at"] > 1000
    else:
        assert len(completed) == 8
        assert not any(event.get("warning") for event in events)
