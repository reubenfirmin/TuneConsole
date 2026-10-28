"""The egress guard is a security primitive, so its behavior is pinned by tests:
allow the allowlist, block everything else, strip secrets from the log, and gate
*both* HTTP stacks (requests and urllib). The request must never reach the
underlying transport when the host is off-allowlist."""
import io
import json
import ssl
import urllib.error
import urllib.request
from email.message import Message

import pytest
import requests

from yt_playlist.egress import EgressGuard, BlockedHost, host_allowed


def test_allowlist_membership():
    assert host_allowed("music.youtube.com")        # subdomain of youtube.com
    assert host_allowed("YouTube.com")              # case-insensitive
    assert host_allowed("ws.audioscrobbler.com")
    assert host_allowed("www.last.fm")              # album HTML page, for the Release Date
    assert host_allowed("open.spotify.com")         # public oEmbed metadata for import artwork
    assert not host_allowed("evil.com")
    assert not host_allowed("notyoutube.com")       # suffix must fall on a dot boundary
    assert not host_allowed("youtube.com.evil.com")
    assert not host_allowed("")
    assert not host_allowed('api.deepseek.com')  # Only the exact optional POST is permitted.


@pytest.mark.parametrize('stack', ['requests', 'urllib'])
@pytest.mark.parametrize('method,url', [
    ('POST', 'https://api.deepseek.com/chat/completions'),
    ('GET', 'https://api.deepseek.com/chat/completions'),
    ('POST', 'http://api.deepseek.com/chat/completions'),
    ('POST', 'https://api.deepseek.com/chat/completions?key=SECRET'),
    ('POST', 'https://api.deepseek.com/chat/completions#SECRET'),
    ('POST', 'https://user:SECRET@api.deepseek.com/chat/completions'),
    ('POST', 'https://api.deepseek.com:444/chat/completions'),
    ('POST', 'https://api.deepseek.com/chat/completions/'),
    ('POST', 'https://api.deepseek.com/files'),
    ('POST', 'https://api.deepseek.com.evil.com/chat/completions'),
])
def test_deepseek_is_exact_https_post_only_and_secrets_are_not_logged(logged_transports, stack, method, url):
    guard, reached = logged_transports, []
    def transport(*args, **kwargs):
        reached.append(url)
        return _urllib_response(url, 200) if stack == 'urllib' else requests.Response()
    allowed = method == 'POST' and url == 'https://api.deepseek.com/chat/completions'
    if stack == 'requests':
        guard._orig_adapter_send = transport
        call = lambda: requests.adapters.HTTPAdapter().send(_Req(url, method, b'SECRET'))
    else:
        guard._orig_opener_send = transport
        call = lambda: urllib.request.build_opener().open(
            urllib.request.Request(url, data=b'SECRET', headers={'Authorization': 'Bearer SECRET'}, method=method))
    if allowed:
        call()
    else:
        with pytest.raises(BlockedHost):
            call()
    assert reached == ([url] if allowed else [])
    assert 'SECRET' not in '\n'.join(guard.recent())


@pytest.mark.parametrize("method,url", [
    ("GET", "https://accounts.google.com/o/oauth2/v2/auth"),
    ("POST", "https://oauth2.googleapis.com/token"),
    ("GET", "https://www.googleapis.com/youtube/v3/playlists?part=snippet"),
    ("GET", "https://www.googleapis.com/youtube/v3/channels"),
    ("POST", "https://www.googleapis.com/youtube/v3/playlists"),
    ("GET", "https://www.googleapis.com/drive/v3/files"),
    ("GET", "https://gmail.googleapis.com/gmail/v1/users/me"),
    ("GET", "https://www.googleapis.com/youtube/v3/videos"),
    ("GET", "http://www.googleapis.com/youtube/v3/playlists"),
    ("GET", "https://www.googleapis.com:444/youtube/v3/playlists"),
    ("POST", "https://oauth2.googleapis.com/revoke"),
])
def test_google_oauth_and_data_api_are_blocked_before_transport(tmp_path, monkeypatch, method, url):
    g = EgressGuard(log_path=tmp_path / "net.log")
    # Register both originals so pytest restores the process-wide hooks after this test.
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", requests.adapters.HTTPAdapter.send)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", urllib.request.OpenerDirector.open)
    monkeypatch.setattr(urllib.request.OpenerDirector, "_open", urllib.request.OpenerDirector._open)
    g.install()
    reached = []
    g._orig_adapter_send = lambda *a, **k: reached.append("requests") or _Resp()
    g._orig_opener_send = lambda *a, **k: reached.append("urllib") or _Resp()
    with pytest.raises(BlockedHost):
        requests.adapters.HTTPAdapter().send(_Req(url, method))
    with pytest.raises(BlockedHost):
        urllib.request.build_opener().open(urllib.request.Request(url, method=method))
    assert reached == []
    assert all("BLOCK" in line for line in g.recent())


def test_cover_art_hosts_are_allowed():
    """Cover art enrichment reaches Cover Art Archive, which redirects its JSON to archive.org's
    CDN, so BOTH are needed: allowing only coverartarchive.org blocks the redirect and the fallback
    silently yields no art (which is exactly how it shipped broken once)."""
    assert host_allowed("coverartarchive.org")
    assert host_allowed("archive.org")
    assert host_allowed("dn721909.ca.archive.org")   # the redirect target is a wildcard CDN subdomain


def test_cover_art_hosts_do_not_widen_the_boundary():
    assert not host_allowed("notarchive.org")
    assert not host_allowed("archive.org.evil.com")
    assert not host_allowed("coverartarchive.org.evil.com")


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
    def __init__(self, url, method="GET", body=None):
        self.url, self.method, self.body = url, method, body


def test_install_gates_requests_stack(tmp_path, monkeypatch):
    """After install(), HTTPAdapter.send enforces the allowlist before the transport."""
    g = EgressGuard(log_path=tmp_path / "net.log")
    orig = requests.adapters.HTTPAdapter.send
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", urllib.request.OpenerDirector.open)
    monkeypatch.setattr(urllib.request.OpenerDirector, "_open", urllib.request.OpenerDirector._open)
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


def test_install_gates_urllib_stack(tmp_path, monkeypatch):
    """After install(), OpenerDirector.open enforces the allowlist before the transport."""
    g = EgressGuard(log_path=tmp_path / "net.log")
    orig = urllib.request.OpenerDirector.open
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", requests.adapters.HTTPAdapter.send)
    monkeypatch.setattr(urllib.request.OpenerDirector, "_open", urllib.request.OpenerDirector._open)
    reached = []
    try:
        g.install()
        g._orig_opener_send = lambda self, fullurl, *a, **k: reached.append(
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
    code = 200
    msg = "OK"

    def info(self):
        return self.headers


def test_github_has_no_domain_wide_allowance():
    assert not host_allowed("github.com")
    assert not host_allowed("api.github.com")
    assert not host_allowed("uploads.github.com")
    assert not host_allowed("raw.githubusercontent.com")


_UPDATE_URL = "https://api.github.com/repos/reubenfirmin/TuneConsole/releases/latest"


@pytest.fixture
def guarded_transports(tmp_path, monkeypatch):
    g = EgressGuard(log_path=tmp_path / "net.log")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", requests.adapters.HTTPAdapter.send)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", urllib.request.OpenerDirector.open)
    monkeypatch.setattr(urllib.request.OpenerDirector, "_open", urllib.request.OpenerDirector._open)
    g.install()
    reached = []
    g._orig_adapter_send = lambda *a, **k: reached.append("requests") or _Resp()
    g._orig_opener_send = lambda *a, **k: reached.append("urllib") or _Resp()
    return g, reached


def test_exact_github_update_request_is_allowed_on_both_stacks(guarded_transports):
    g, reached = guarded_transports
    requests.adapters.HTTPAdapter().send(_Req(_UPDATE_URL))
    urllib.request.build_opener().open(_UPDATE_URL)
    assert reached == ["requests", "urllib"]
    assert len(g.recent()) == 2
    assert all("verdict=ALLOW" in line for line in g.recent())


@pytest.mark.parametrize("method,url", [
    ("POST", _UPDATE_URL),
    ("PUT", _UPDATE_URL),
    ("HEAD", _UPDATE_URL),
    ("GET", "https://github.com/reubenfirmin/TuneConsole/releases/latest"),
    ("GET", "https://api.github.com/repos/other/project/releases/latest"),
    ("GET", "https://api.github.com/repos/reubenfirmin/TuneConsole/issues"),
    ("GET", "https://uploads.github.com/repos/reubenfirmin/TuneConsole/releases/latest"),
    ("GET", "https://sub.api.github.com/repos/reubenfirmin/TuneConsole/releases/latest"),
    ("GET", "https://raw.githubusercontent.com/other/project/main/file"),
    ("GET", _UPDATE_URL + "?data=SECRET"),
    ("GET", _UPDATE_URL + "#SECRET"),
    ("GET", _UPDATE_URL + "/"),
    ("GET", _UPDATE_URL + "/extra"),
    ("GET", _UPDATE_URL.replace("https://", "http://")),
    ("GET", _UPDATE_URL.replace("api.github.com", "api.github.com:443")),
    ("GET", _UPDATE_URL.replace("api.github.com", "api.github.com:444")),
    ("GET", _UPDATE_URL.replace("api.github.com", "SECRET@api.github.com")),
    ("GET", _UPDATE_URL.replace("api.github.com", "api.github.com.")),
    ("GET", _UPDATE_URL.replace("/releases/", "/%72eleases/")),
    ("GET", _UPDATE_URL.replace("api.github.com", "api.github.com.evil.example")),
])
def test_other_github_requests_are_blocked_before_transport(guarded_transports, method, url):
    g, reached = guarded_transports
    with pytest.raises(BlockedHost):
        requests.adapters.HTTPAdapter().send(_Req(url, method))
    with pytest.raises(BlockedHost):
        urllib.request.build_opener().open(urllib.request.Request(url, method=method))
    assert reached == []
    assert len(g.recent()) == 2
    assert all("verdict=BLOCK" in line for line in g.recent())
    assert "SECRET" not in "\n".join(g.recent())


@pytest.mark.parametrize("body", [b"SECRET", b""])
def test_github_update_request_cannot_send_a_body(guarded_transports, body):
    g, reached = guarded_transports
    with pytest.raises(BlockedHost):
        requests.adapters.HTTPAdapter().send(_Req(_UPDATE_URL, body=body))
    with pytest.raises(BlockedHost):
        urllib.request.build_opener().open(urllib.request.Request(_UPDATE_URL, data=body, method="GET"))
    assert reached == []
    assert all("verdict=BLOCK" in line for line in g.recent())
    assert "SECRET" not in "\n".join(g.recent())


@pytest.mark.parametrize("as_request", [False, True])
@pytest.mark.parametrize("as_keyword", [False, True])
def test_urllib_update_request_cannot_add_data_at_open(guarded_transports, as_request, as_keyword):
    g, reached = guarded_transports
    url = urllib.request.Request(_UPDATE_URL) if as_request else _UPDATE_URL
    with pytest.raises(BlockedHost):
        if as_keyword:
            urllib.request.build_opener().open(url, data=b"SECRET")
        else:
            urllib.request.build_opener().open(url, b"SECRET")
    assert reached == []
    assert "method=POST" in g.recent()[0]
    assert "SECRET" not in "\n".join(g.recent())


def test_update_checker_still_fetches_latest_release_through_guard(guarded_transports, monkeypatch):
    from yt_playlist.core import updatecheck
    from yt_playlist.integrations.github import GitHubReleasesIAO

    g, _ = guarded_transports
    sent = []

    def fetch(opener, request, *args, **kwargs):
        sent.append(request)
        response = urllib.response.addinfourl(
            io.BytesIO(b'{"tag_name": "v0.2.0"}'), {}, request.full_url, code=200)
        response.msg = "OK"
        return response

    g._orig_opener_send = fetch
    # A fresh boundary proves transport reached the guard instead of a shared cache.
    monkeypatch.setattr(updatecheck, '_github', GitHubReleasesIAO())
    assert updatecheck._fetch_latest_release() == "0.2.0"
    assert len(sent) == 1
    assert sent[0].full_url == _UPDATE_URL
    assert sent[0].get_method() == "GET"
    assert sent[0].data is None
    assert "verdict=ALLOW" in g.recent()[0]


def test_spotify_report_thumbnail_uses_guarded_public_transport(guarded_transports, monkeypatch):
    from yt_playlist.integrations.spotify import SpotifyIAO
    from yt_playlist.providers import spotify

    g, _ = guarded_transports
    sent = []

    def fetch(opener, request, *args, **kwargs):
        sent.append(request)
        body = (b'{"type":"rich","version":"1.0","provider_name":"Spotify",'
                b'"title":"Record","html":"","thumbnail_url":"https://i.scdn.co/image/art"}')
        response = urllib.response.addinfourl(io.BytesIO(body), {}, request.full_url, code=200)
        response.msg = 'OK'
        return response

    g._orig_opener_send = fetch
    monkeypatch.setattr(spotify, '_iao', SpotifyIAO())
    assert spotify.thumbnail('https://open.spotify.com/album/abc123') == 'https://i.scdn.co/image/art'
    assert len(sent) == 1 and sent[0].get_method() == 'GET' and sent[0].data is None
    assert sent[0].full_url.startswith('https://open.spotify.com/oembed?url=')
    assert sent[0].get_header('Authorization') is None
    assert 'verdict=ALLOW' in g.recent()[0]


def test_youtube_storyboard_iao_uses_the_existing_narrow_guard(guarded_transports):
    import time
    from yt_playlist.integrations.youtube_music import YouTubeStoryboardIAO
    guard, _ = guarded_transports
    sent = []
    def fetch(adapter, request, *args, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code, response.url = 200, request.url
        response.headers = {'Content-Type': 'image/jpeg'}
        response.raw = io.BytesIO(b'sprite')
        return response
    guard._orig_adapter_send = fetch
    url = 'https://i.ytimg.com/sb/abcdefghijk/storyboard3_L1/M0.jpg?sigh=PRIVATE'
    assert YouTubeStoryboardIAO().read(url, time.monotonic() + 10) == b'sprite'
    assert len(sent) == 1 and sent[0].method == 'GET' and sent[0].body is None
    assert sent[0].headers.get('Authorization') is None
    lines = guard.recent()
    assert len(lines) == 1 and 'verdict=ALLOW' in lines[0]
    assert 'PRIVATE' not in lines[0] and 'sigh=' not in lines[0]


def test_deepseek_admission_uses_exact_guarded_post(guarded_transports, store, monkeypatch):
    from tests.providers.test_deepseek import ALBUM, TEXT, response
    from yt_playlist.integrations.deepseek import DeepSeekIAO
    from yt_playlist.providers import deepseek
    from yt_playlist.rec import album_descriptions

    g, _ = guarded_transports
    sent = []

    def fetch(opener, request, *args, **kwargs):
        sent.append(request)
        result = urllib.response.addinfourl(io.BytesIO(json.dumps(response()).encode()),
                                            {}, request.full_url, code=200)
        result.msg = 'OK'
        return result

    g._orig_opener_send = fetch
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    store.settings.set_setting('deepseek_api_key', 'sk-SECRET-12345678')
    monkeypatch.setattr(deepseek, '_iao', DeepSeekIAO())
    assert album_descriptions.get(store, ALBUM, 1000)[0]['text'] == TEXT
    assert len(sent) == 1 and sent[0].full_url == 'https://api.deepseek.com/chat/completions'
    assert sent[0].get_method() == 'POST'
    assert store.rec_proposals.get_proposals('album_description_usage')['requests'] == 1
    assert all('SECRET' not in entry for entry in g.recent())
    assert any('verdict=ALLOW' in entry for entry in g.recent())


@pytest.mark.parametrize("stack", ["requests", "urllib"])
@pytest.mark.parametrize("target", [
    "https://api.github.com/repos/other/project/releases/latest",
    _UPDATE_URL + "?data=SECRET",
    "https://github.com/reubenfirmin/TuneConsole/releases/latest",
])
def test_github_redirects_are_checked_before_transport(tmp_path, monkeypatch, stack, target):
    g = EgressGuard(log_path=tmp_path / "net.log")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", requests.adapters.HTTPAdapter.send)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", urllib.request.OpenerDirector.open)
    monkeypatch.setattr(urllib.request.OpenerDirector, "_open", urllib.request.OpenerDirector._open)
    g.install()
    reached = []

    if stack == "requests":
        def redirect(adapter, request, *args, **kwargs):
            reached.append(request.url)
            response = requests.Response()
            response.status_code = 302
            response.headers["Location"] = target
            response.url = request.url
            response.request = request
            response._content = b""
            response.raw = io.BytesIO()
            return response

        g._orig_adapter_send = redirect
        with requests.Session() as session:
            session.trust_env = False
            with pytest.raises(BlockedHost):
                session.get(_UPDATE_URL)
    else:
        class Redirect(urllib.request.BaseHandler):
            handler_order = 100

            def https_open(self, request):
                reached.append(request.full_url)
                headers = Message()
                headers["Location"] = target
                response = urllib.response.addinfourl(
                    io.BytesIO(), headers, request.full_url, code=302)
                response.msg = "Found"
                return response

            http_open = https_open

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
        with pytest.raises(BlockedHost):
            opener.open(_UPDATE_URL)

    assert reached == [_UPDATE_URL]
    assert any("verdict=BLOCK" in line for line in g.recent())
    assert "SECRET" not in "\n".join(g.recent())


@pytest.fixture
def logged_transports(tmp_path, monkeypatch):
    for owner, attribute in ((requests.adapters.HTTPAdapter, "send"),
                             (urllib.request.OpenerDirector, "open"),
                             (urllib.request.OpenerDirector, "_open")):
        monkeypatch.setattr(owner, attribute, getattr(owner, attribute))
    guard = EgressGuard(log_path=tmp_path / "net.log")
    guard.install()
    return guard


def _urllib_response(url, status, headers=None, body=b"unconsumed body"):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    response = urllib.response.addinfourl(io.BytesIO(body), message, url, code=status)
    response.msg = "SECRET reason"
    return response


@pytest.mark.parametrize("stack", ["requests", "urllib"])
@pytest.mark.parametrize("failure", ["timeout", "connection", "tls", "transport", "http429", "http503", None])
def test_failed_allowed_requests_are_logged_once_without_secrets(logged_transports, stack, failure):
    g = logged_transports
    url = "https://private:password@musicbrainz.org/ws/2/artist?api_key=SECRET#SECRET"
    error = None
    status = int(failure[4:]) if failure and failure.startswith("http") else 200
    if failure in ("timeout", "connection", "tls", "transport"):
        if stack == "requests":
            kind = {"timeout": requests.exceptions.Timeout, "connection": requests.exceptions.ConnectionError,
                    "tls": requests.exceptions.SSLError, "transport": ValueError}[failure]
            error = kind("SECRET " + url)
        else:
            reason = {"timeout": TimeoutError, "connection": ConnectionError,
                      "tls": ssl.SSLError, "transport": ValueError}[failure]("SECRET " + url)
            error = urllib.error.URLError(reason) if failure != "transport" else reason

    def transport(*args, **kwargs):
        if error is not None:
            raise error
        if stack == "urllib":
            return _urllib_response(url, status)
        response = requests.Response()
        response.status_code, response.url = status, url
        response.raw = io.BytesIO(b"unconsumed body")
        return response

    if stack == "requests":
        g._orig_adapter_send = transport
        call = lambda: requests.adapters.HTTPAdapter().send(_Req(url), stream=True)
    else:
        g._orig_opener_send = transport
        call = lambda: urllib.request.build_opener().open(url)
    if error is not None:
        with pytest.raises(type(error)) as caught:
            call()
        assert caught.value is error
    elif stack == "urllib" and status >= 400:
        with pytest.raises(urllib.error.HTTPError) as caught:
            call()
        assert caught.value.code == status
        assert caught.value.read() == b"unconsumed body"
    else:
        response = call()
        body = response.raw.read() if stack == "requests" else response.read()
        assert body == b"unconsumed body"
    lines = g.recent()
    assert len(lines) == 1 and "verdict=ALLOW" in lines[0]
    category = "http" if status >= 400 else failure
    assert f"failure={category}" in lines[0]
    assert f"status={None if error is not None else status}" in lines[0]
    assert "host=musicbrainz.org path=/ws/2/artist" in lines[0]
    assert all(secret not in lines[0] for secret in ("SECRET", "private", "password", "api_key", "#"))


@pytest.mark.parametrize("stack", ["requests", "urllib"])
@pytest.mark.parametrize("ending", ["success", "http", "timeout", "blocked"])
def test_redirect_hops_keep_their_own_status_and_failure(logged_transports, stack, ending):
    g = logged_transports
    start = "https://musicbrainz.org/start?key=SECRET"
    target = ("https://evil.example/blocked" if ending == "blocked" else "https://archive.org/final") + "?key=SECRET"
    reached = []

    def transport(request):
        url = request.url if stack == "requests" else request.full_url
        reached.append(url)
        if len(reached) == 1:
            status, headers = 302, {"Location": target}
        else:
            if ending == "timeout":
                raise TimeoutError("SECRET " + url)
            status, headers = (503 if ending == "http" else 200), {}
        if stack == "urllib":
            return _urllib_response(url, status, headers)
        response = requests.Response()
        response.status_code, response.headers = status, requests.structures.CaseInsensitiveDict(headers)
        response.url, response.request, response._content = url, request, b""
        response.raw = io.BytesIO()
        return response

    if stack == "requests":
        g._orig_adapter_send = lambda adapter, request, *a, **kw: transport(request)
        session = requests.Session()
        session.trust_env = False
        call = lambda: session.get(start)
    else:
        g._orig_opener_send = lambda opener, request, *a, **kw: transport(request)
        call = lambda: urllib.request.build_opener(urllib.request.ProxyHandler({})).open(start)
    if ending == "blocked":
        with pytest.raises(BlockedHost):
            call()
    elif ending == "timeout":
        with pytest.raises(TimeoutError):
            call()
    elif ending == "http" and stack == "urllib":
        with pytest.raises(urllib.error.HTTPError) as caught:
            call()
        assert caught.value.code == 503
    else:
        call().close()
    lines = g.recent()
    assert len(lines) == 2
    assert "path=/start status=302" in lines[0] and "failure=None" in lines[0]
    expected = {"success": "status=200", "http": "status=503", "timeout": "failure=timeout", "blocked": "verdict=BLOCK"}
    assert expected[ending] in lines[1]
    assert len(reached) == (1 if ending == "blocked" else 2)
    assert "SECRET" not in "\n".join(lines)


@pytest.mark.parametrize('ending', ['success', 'http503', 'timeout', 'blocked'])
def test_cover_art_iao_keeps_guarded_archive_redirects_and_retry_outcomes(logged_transports, monkeypatch, ending):
    from types import SimpleNamespace
    from yt_playlist.integrations.coverartarchive import CoverArtArchiveIAO
    from yt_playlist.providers import coverart

    guard, reached = logged_transports, []
    start = 'https://coverartarchive.org/release/release'
    target = ('https://archive.org.evil.example/index.json' if ending == 'blocked'
              else 'https://dn721909.ca.archive.org/mbid-release/index.json')
    data = {'images': [{'front': True, 'image': 'https://archive.org/cover.jpg'}]}

    def transport(opener, request, *args, **kwargs):
        url = request.full_url
        reached.append(url)
        if len(reached) == 1:
            return _urllib_response(url, 307, {'Location': target})
        if ending == 'timeout':
            raise TimeoutError('controlled archive outage')
        return _urllib_response(url, 503 if ending == 'http503' else 200,
            {'Retry-After': '120'} if ending == 'http503' else {}, json.dumps(data).encode())

    guard._orig_opener_send = transport
    monkeypatch.setattr(urllib.request, '_opener',
                        urllib.request.build_opener(urllib.request.ProxyHandler({})))
    monkeypatch.setattr(coverart, '_iao', CoverArtArchiveIAO(now_fn=lambda: 100,
        pacer=SimpleNamespace(wait=lambda: None)))
    outcome = coverart.lookup(['release'])
    if ending == 'success':
        assert outcome.status == 'found' and outcome.url == 'https://archive.org/cover.jpg'
    else:
        assert outcome.status == 'retry' and outcome.url is None
        if ending == 'http503':
            assert outcome.retry_in == 120
        # Host failures defer other releases; a blocked redirect defers its request.
        assert coverart.lookup(['release' if ending == 'blocked' else 'different']).status == 'retry'
    assert reached == ([start] if ending == 'blocked' else [start, target])
    lines = guard.recent()
    assert len(lines) == 2 and 'status=307' in lines[0]
    assert ('verdict=BLOCK' in lines[1]) == (ending == 'blocked')
    assert 'SECRET' not in '\n'.join(lines)
