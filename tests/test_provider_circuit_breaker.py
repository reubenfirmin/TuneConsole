"""When an enrichment host goes unreachable, the playlist loop stops instead of plowing through
every remaining track (one wasted pace-interval each). A server response — even an error — or any
success clears the streak, so a real outage trips it but a per-track miss never does."""
import urllib.error

import pytest

from yt_playlist.providers import discogs, lastfm, musicbrainz
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
    dns = urllib.error.URLError("down")
    http = urllib.error.HTTPError("u", 503, "busy", {}, None)
    b.record(dns)
    b.record(http)                 # server answered (a 503) — host reachable, streak cleared
    assert b.consecutive == 0
    b.record(dns)
    b.record()                     # success — streak cleared
    assert b.consecutive == 0


def test_parse_error_counts_as_reachable():
    b = net.CircuitBreaker(threshold=1)
    b.record(ValueError("bad json"))   # we got bytes back, just couldn't parse them
    assert not b.tripped()


@pytest.mark.parametrize("provider", [musicbrainz, discogs, lastfm])
def test_enrich_loop_aborts_when_host_unreachable(provider, store, monkeypatch):
    ids = [store.upsert_track(f"v{i}", f"T{i}", "Art", None, None) for i in range(20)]
    pending = [{"id": tid, "video_id": f"v{i}", "title": f"T{i}", "artist": "Art"}
               for i, tid in enumerate(ids)]

    calls = []
    dns = urllib.error.URLError("Name or service not known")

    def fake_enrich(title, artist, *extra):       # discogs/lastfm pass a token/key as a 3rd arg
        calls.append(title)
        provider._breaker.record(dns)             # stand in for the failing network call
        return (None, None)

    monkeypatch.setattr(provider, "enrich", fake_enrich)
    # lastfm refuses to start without a key; give it one so we reach the loop
    monkeypatch.setattr(lastfm, "api_key", lambda *a, **k: "k", raising=False)

    events = []
    provider.enrich_playlist(store, None, events.append, pending=pending)

    threshold = provider._breaker.threshold
    assert len(calls) == threshold                # stopped at the threshold, not all 20 tracks
    assert any(e.get("type") == "err" and "unreachable" in e["text"] for e in events)


@pytest.mark.parametrize("provider", [musicbrainz, discogs, lastfm])
def test_enrich_loop_completes_when_host_reachable(provider, store, monkeypatch):
    """A genuine no-match (host up, empty result) must NOT trip the breaker — all tracks processed."""
    ids = [store.upsert_track(f"v{i}", f"T{i}", "Art", None, None) for i in range(8)]
    pending = [{"id": tid, "video_id": f"v{i}", "title": f"T{i}", "artist": "Art"}
               for i, tid in enumerate(ids)]

    calls = []

    def fake_enrich(title, artist, *extra):
        calls.append(title)
        provider._breaker.record()                # reachable, just no match for this track
        return (None, None)

    monkeypatch.setattr(provider, "enrich", fake_enrich)
    monkeypatch.setattr(lastfm, "api_key", lambda *a, **k: "k", raising=False)

    events = []
    provider.enrich_playlist(store, None, events.append, pending=pending)

    assert len(calls) == 8                         # plowed through every track, no early stop
    assert not any(e.get("type") == "err" for e in events)
