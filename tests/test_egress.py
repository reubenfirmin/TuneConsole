"""The egress guard is a security primitive, so its behavior is pinned by tests:
allow the allowlist, block everything else, strip secrets from the log, and gate
*both* HTTP stacks (requests and urllib). The request must never reach the
underlying transport when the host is off-allowlist."""
import urllib.request

import pytest
import requests

from yt_playlist.egress import EgressGuard, BlockedHost, host_allowed


def test_allowlist_membership():
    assert host_allowed("music.youtube.com")        # subdomain of youtube.com
    assert host_allowed("YouTube.com")              # case-insensitive
    assert host_allowed("ws.audioscrobbler.com")
    assert host_allowed("www.last.fm")              # album HTML page, for the Release Date
    assert not host_allowed("evil.com")
    assert not host_allowed("notyoutube.com")       # suffix must fall on a dot boundary
    assert not host_allowed("youtube.com.evil.com")
    assert not host_allowed("")


def test_gate_strips_query_and_returns_path(tmp_path):
    g = EgressGuard(log_path=tmp_path / "net.log")
    host, path = g.gate("https://ws.audioscrobbler.com/2.0/?api_key=SECRET&method=x",
                        method="GET", via="urllib")
    assert host == "ws.audioscrobbler.com" and path == "/2.0/"
    assert "SECRET" not in "\n".join(g.recent())     # api_key from the query never hits the log


def test_gate_blocks_unknown_host_and_logs_it(tmp_path):
    g = EgressGuard(log_path=tmp_path / "net.log")
    with pytest.raises(BlockedHost):
        g.gate("https://evil.example.com/exfil", method="POST", via="requests")
    assert any("BLOCK" in ln and "evil.example.com" in ln for ln in g.recent())


class _Req:
    """Minimal stand-in for a requests PreparedRequest (gate only reads url/method)."""
    def __init__(self, url, method="GET"):
        self.url, self.method = url, method


def test_install_gates_requests_stack(tmp_path):
    """After install(), HTTPAdapter.send enforces the allowlist before the transport."""
    g = EgressGuard(log_path=tmp_path / "net.log")
    orig = requests.adapters.HTTPAdapter.send
    reached = []
    try:
        g.install()
        # Replace the captured *original* with a stub so an allowed request never hits the network.
        g._orig_adapter_send = lambda self, request, *a, **k: reached.append(request.url) or _Resp()
        adapter = requests.adapters.HTTPAdapter()

        with pytest.raises(BlockedHost):                         # off-allowlist: refused...
            adapter.send(_Req("https://evil.example.com/exfil"))
        assert reached == []                                     # ...and never reached the transport

        adapter.send(_Req("https://music.youtube.com/youtubei/v1/browse"))  # allowed: passes through
        assert reached == ["https://music.youtube.com/youtubei/v1/browse"]
    finally:
        requests.adapters.HTTPAdapter.send = orig


def test_install_gates_urllib_stack(tmp_path):
    """After install(), OpenerDirector.open enforces the allowlist before the transport."""
    g = EgressGuard(log_path=tmp_path / "net.log")
    orig = urllib.request.OpenerDirector.open
    reached = []
    try:
        g.install()
        g._orig_opener_open = lambda self, fullurl, *a, **k: reached.append(
            fullurl if isinstance(fullurl, str) else fullurl.full_url) or _Resp()
        opener = urllib.request.OpenerDirector()

        with pytest.raises(BlockedHost):
            opener.open("https://evil.example.com/exfil")
        assert reached == []

        opener.open("https://musicbrainz.org/ws/2/release")
        assert reached == ["https://musicbrainz.org/ws/2/release"]
    finally:
        urllib.request.OpenerDirector.open = orig


class _Resp:
    """Stub HTTP response with the bits record() reads."""
    status_code = status = 200
    headers = {"Content-Length": "0"}
