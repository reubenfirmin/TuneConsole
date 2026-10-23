"""Network allowlist presentation and request-log parsing."""
from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.setup_support import _FakeRuntime
def test_network_page_renders_allowlist(store, tmp_path, monkeypatch):
    from yt_playlist.egress import ALLOWED_DOMAINS, EgressGuard
    from yt_playlist.web.routes import network

    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))   # keep the lazy guard's log off the real FS
    guard = EgressGuard(log_path=tmp_path / "network.log")
    guard.record(verdict="ALLOW", method="GET", host="musicbrainz.org", path="/ws/2/artist",
                 via="urllib", failure="timeout")
    monkeypatch.setattr(network, "guard", lambda: guard)
    rt = _FakeRuntime(store, configured=True, credentials_present=True)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.get("/network")
    assert r.status_code == 200
    for host in ALLOWED_DOMAINS:
        assert f'<span class="tag teal mono">{host}</span>' in r.text
    for purpose in ("YouTube Music", "Track Metadata", "Artist and Genre Summaries",
                    "Cover Art", "Update Checks"):
        assert f'>{purpose}</h3>' in r.text
    assert "GET https://api.github.com/repos/reubenfirmin/TuneConsole/releases/latest" in r.text
    assert '<span class="tag teal mono">github.com</span>' not in r.text
    assert "request bodies, and methods are blocked" in r.text
    assert "<th>Failure</th>" in r.text and "<td>timeout</td>" in r.text

def test_network_log_line_parser():
    from yt_playlist.web.routes.network import _parse
    row = _parse("2026-06-22 11:00:00,123 verdict=BLOCK via=requests method=POST "
                 "host=evil.example.com path=/exfil status=None bytes=None")
    assert row["ts"] == "2026-06-22 11:00:00,123"
    assert row["verdict"] == "BLOCK" and row["host"] == "evil.example.com"
    assert row["method"] == "POST" and row["path"] == "/exfil"


# --- vendored front-end assets (no third-party CDN at runtime) ---

