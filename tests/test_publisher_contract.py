"""Exercise real Backend request/response handling across GitHub draft boundaries."""
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.error
import urllib.parse

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
from promote_release import Backend
import promote_release
from rehearse_publisher import rehearse
import release_services


class GitHub:
    def __init__(self, monkeypatch):
        self.release = None
        self.contents = {}
        self.requests = []
        self.commands = []
        self.next_id = 20
        self.interrupt_upload = False
        self.unreachable = False
        self.hidden_reads = 0
        self.hide_created_reads = 0
        monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
        monkeypatch.setattr("urllib.request.urlopen", self.urlopen)
        self.backend = Backend("owner/repo")
        monkeypatch.setattr(self.backend, "run", self.run)

    def urlopen(self, request, timeout):
        assert timeout == 60
        assert request.get_header("Authorization") == "Bearer fixture-token"
        if request.method == "GET":
            assert request.get_header("Cache-control") == "no-cache"
        path = urllib.parse.urlsplit(request.full_url).path
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.requests.append((request.method, path, query))
        if self.unreachable:
            raise urllib.error.URLError("offline")
        if request.method == "POST":
            data = json.loads(request.data)
            self.release = {"id": 12, "tag_name": data["tag_name"], "draft": data["draft"], "assets": []}
            self.hidden_reads = self.hide_created_reads
            result = self.release
        elif request.method == "DELETE":
            self.release = None
            return io.BytesIO(b"")  # GitHub DELETE returns 204 with no JSON body.
        elif "/releases/tags/" in path:
            # The contract that the old mocks missed: draft tag lookup is 404.
            if not self.release or self.release["draft"]:
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
            result = self.release
        elif "/releases/assets/" in path:
            return io.BytesIO(self.contents[int(path.rsplit("/", 1)[1])])
        else:
            assert query["per_page"] == ["100"]
            # Ensure drafts on later pages can be found, not just recent releases.
            if query["page"] == ["1"]:
                result = [{"tag_name": f"v0.0.{n}", "draft": False, "assets": []} for n in range(100)]
            elif self.hidden_reads:
                self.hidden_reads -= 1
                result = []
            else:
                result = [self.release] if self.release else []
        return io.BytesIO(json.dumps(result).encode())

    def run(self, *args):
        self.commands.append(args)
        if args[:3] == ("gh", "release", "create"):
            assert self.release is None
            self.release = {"id": 12, "tag_name": args[3], "draft": True, "assets": []}
            self.hidden_reads = self.hide_created_reads
            return ""
        assert args[:3] == ("gh", "release", "upload")
        assert self.release["draft"]
        files = [Path(arg) for arg in args[3:] if Path(arg).is_file()]
        for path in files:
            self.release["assets"] = [a for a in self.release["assets"] if a["name"] != path.name]
            content = path.read_bytes()
            self.contents[self.next_id] = content
            self.release["assets"].append({"id": self.next_id, "name": path.name, "size": len(content),
                                           "digest": "sha256:" + hashlib.sha256(content).hexdigest()})
            self.next_id += 1
            if self.interrupt_upload:
                self.interrupt_upload = False
                raise TimeoutError("upload interrupted after committing one asset")
        return ""


@pytest.fixture
def lookup_clock(monkeypatch):
    clock = SimpleNamespace(now=0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(promote_release, "time", SimpleNamespace(
        monotonic=lambda: clock.now, sleep=sleep))
    return clock


def test_draft_create_lookup_partial_upload_retry_and_cleanup(monkeypatch):
    github = GitHub(monkeypatch)
    rehearse(github.backend, "publisher-contract-test", "a" * 40)
    assert github.release is None
    assert len(github.commands) == 3  # partial stage, complete stage, idempotent repeat
    assert any(query.get("page") == ["2"] for _, _, query in github.requests)
    assert github.requests[-1][0] == "DELETE"


def test_rehearsal_waits_for_created_draft_to_appear_in_listing(monkeypatch, lookup_clock):
    github = GitHub(monkeypatch)
    github.hide_created_reads = 3
    rehearse(github.backend, "publisher-contract-delayed", "a" * 40)
    assert lookup_clock.sleeps == [1, 1, 1]
    assert len(github.commands) == 3
    assert github.release is None


def test_real_staging_waits_for_created_draft_before_upload(monkeypatch, tmp_path, lookup_clock):
    github = GitHub(monkeypatch)
    github.hide_created_reads = 2
    (tmp_path / "asset.txt").write_bytes(b"candidate")
    manifest = {"sha256": {"asset.txt": hashlib.sha256(b"candidate").hexdigest()}}
    github.backend.stage("v1.2.3", manifest, tmp_path)
    assert lookup_clock.sleeps == [1, 1]
    assert [args[2] for args in github.commands] == ["create", "upload"]
    assert {asset["name"] for asset in github.release["assets"]} == {
        "asset.txt", "release-manifest.json"}


def test_expected_draft_lookup_wait_is_bounded(monkeypatch, lookup_clock):
    github = GitHub(monkeypatch)
    github.release = {"id": 12, "tag_name": "v1.2.3", "draft": True, "assets": []}
    github.hidden_reads = 100
    assert github.backend.existing("v1.2.3") is None
    assert lookup_clock.sleeps == []
    assert github.backend.existing("v1.2.3", wait_seconds=3) is None
    assert lookup_clock.sleeps == [1, 1, 1]
    assert github.commands == []


def test_rehearsal_cleans_up_after_interrupted_upload(monkeypatch):
    github = GitHub(monkeypatch)
    github.interrupt_upload = True
    with pytest.raises(TimeoutError, match="upload interrupted"):
        rehearse(github.backend, "publisher-contract-failure", "b" * 40)
    assert github.release is None
    assert github.requests[-1][0] == "DELETE"


def test_interrupted_real_staging_resumes_and_published_assets_are_immutable(monkeypatch, tmp_path):
    github = GitHub(monkeypatch)
    github.release = {"id": 12, "tag_name": "v1.2.3", "draft": True, "assets": []}
    (tmp_path / "asset.txt").write_bytes(b"candidate")
    manifest = {"sha256": {"asset.txt": hashlib.sha256(b"candidate").hexdigest()}}
    github.interrupt_upload = True
    with pytest.raises(TimeoutError):
        github.backend.stage("v1.2.3", manifest, tmp_path)
    assert len(github.release["assets"]) == 1
    github.backend.stage("v1.2.3", manifest, tmp_path)
    assert len(github.release["assets"]) == 2
    github.release["draft"] = False
    github.requests.clear()
    assert github.backend.existing("v1.2.3") == github.release
    assert len(github.requests) == 1  # Published tag lookup does not list drafts.
    before = len(github.commands)
    with pytest.raises(ValueError, match="immutable"):
        github.backend.stage("v1.2.3", manifest, tmp_path)
    assert len(github.commands) == before


def test_outage_is_not_treated_as_a_missing_release(monkeypatch):
    github = GitHub(monkeypatch)
    github.unreachable = True
    with pytest.raises(urllib.error.URLError):
        github.backend.existing("v1.2.3")
    assert github.commands == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
@pytest.mark.parametrize("wait_seconds", [0, 10])
def test_failed_lookup_is_not_release_absence(monkeypatch, status, wait_seconds, lookup_clock):
    github = GitHub(monkeypatch)

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "lookup failed", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(urllib.error.HTTPError) as failure:
        github.backend.existing("v1.2.3", wait_seconds=wait_seconds)
    assert failure.value.code == status
    assert github.commands == []
    assert lookup_clock.sleeps == []


@pytest.mark.parametrize("field,value", [("size", 1), ("digest", "sha256:" + "f" * 64)])
def test_uploaded_asset_metadata_must_match(monkeypatch, tmp_path, field, value):
    github = GitHub(monkeypatch)
    github.release = {"id": 12, "tag_name": "v1.2.3", "draft": True, "assets": []}
    (tmp_path / "asset.txt").write_bytes(b"candidate")
    manifest = {"sha256": {"asset.txt": hashlib.sha256(b"candidate").hexdigest()}}
    upload = github.run

    def corrupt(*args):
        upload(*args)
        github.release["assets"][0][field] = value

    monkeypatch.setattr(github.backend, "run", corrupt)
    with pytest.raises(ValueError, match="artifact"):
        github.backend.stage("v1.2.3", manifest, tmp_path)


def test_preflight_reports_exact_failing_service_without_exposing_response(monkeypatch, capsys):
    backend = Backend("owner/repo")
    checked = []
    monkeypatch.setattr(release_services.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(backend, "run", lambda *args: None)
    monkeypatch.setattr(release_services, "rehearse", lambda *args: None)
    monkeypatch.setattr(release_services, "pages_permissions", lambda *args: checked.append("pages"))
    monkeypatch.setattr(backend, "require_reconciled_channels", lambda *args: checked.append("recovery"))
    monkeypatch.setattr(backend, "snapshot", lambda *args: checked.append("baseline"))

    def denied(image):
        raise urllib.error.URLError("credential=do-not-print")

    monkeypatch.setattr(release_services, "registry_permissions", denied)
    with pytest.raises(RuntimeError, match="GHCR package push"):
        release_services.preflight(backend, "v1.2.3", "a" * 40, "publisher-contract-test", stable=True)
    output = capsys.readouterr().out
    assert "GHCR package push" in output
    assert "do-not-print" not in output
    assert checked == ["pages", "recovery", "baseline"]


@pytest.mark.parametrize("location", [
    "/v2/owner/repo/blobs/uploads/probe",
    "https://ghcr.io/v2/owner/repo/blobs/uploads/probe?_state=fixture-secret",
])
@pytest.mark.parametrize("completion_status", [201, 202])
def test_registry_preflight_transfers_blob_and_completes_updated_upload_session(
        monkeypatch, location, completion_status):
    monkeypatch.setenv("GITHUB_ACTOR", "owner")
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    requests = []
    next_location = "/v2/owner/repo/blobs/uploads/probe-next?_state=updated%2Bstate%2Fvalue"

    def respond(request, timeout):
        assert timeout == 30
        requests.append(request)
        response = io.BytesIO(b'{"token":"registry-token"}' if len(requests) == 1 else b"")
        response.status = {1: 200, 2: 202, 3: 202, 4: completion_status}[len(requests)]
        response.headers = {"Location": next_location if len(requests) == 3 else location}
        return response

    monkeypatch.setattr(release_services.urllib.request, "urlopen", respond)
    if completion_status == 201:
        release_services.registry_permissions("ghcr.io/owner/repo")
    else:
        with pytest.raises(ValueError, match="did not complete"):
            release_services.registry_permissions("ghcr.io/owner/repo")
    assert [request.get_method() for request in requests] == ["GET", "POST", "PATCH", "PUT"]
    assert requests[0].get_header("Authorization").startswith("Basic ")
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0].full_url).query)["scope"] == [
        "repository:owner/repo:pull,push"]
    assert requests[2].full_url == urllib.parse.urljoin("https://ghcr.io/", location)
    assert requests[2].data == b"{}"
    assert requests[2].get_header("Content-range") == "0-1"
    assert requests[-1].full_url.startswith("https://ghcr.io" + next_location + "&digest=")
    upload = urllib.parse.urlsplit(requests[-1].full_url)
    assert (upload.scheme, upload.netloc, upload.path) == (
        "https", "ghcr.io", "/v2/owner/repo/blobs/uploads/probe-next")
    assert urllib.parse.parse_qs(upload.query) == {
        "_state": ["updated+state/value"],
        "digest": ["sha256:" + hashlib.sha256(b"{}").hexdigest()],
    }
    for request in requests[1:]:
        assert request.get_header("Authorization") == "Bearer registry-token"
        assert request.get_header("Content-type") == "application/octet-stream"
    assert requests[1].data == requests[-1].data == b""


@pytest.mark.parametrize("failed_request,operation", [
    (1, "token request"), (2, "upload initiation"), (3, "blob transfer"), (4, "upload completion"),
])
@pytest.mark.parametrize("status", [401, 403, 404, 405, 503])
def test_registry_preflight_names_http_failure_without_upload_url_or_credentials(
        monkeypatch, capsys, failed_request, operation, status):
    monkeypatch.setenv("GITHUB_ACTOR", "owner")
    monkeypatch.setenv("GITHUB_TOKEN", "credential-do-not-print")
    requests = []

    def respond(request, timeout):
        requests.append(request)
        if len(requests) == failed_request:
            raise urllib.error.HTTPError(request.full_url, status, "secret-response", {}, None)
        response = io.BytesIO(b'{"token":"registry-token-do-not-print"}')
        response.status = 200 if len(requests) == 1 else 202
        response.headers = {"Location": "/v2/owner/repo/blobs/uploads/probe?_state=secret-state"}
        return response

    monkeypatch.setattr(release_services.urllib.request, "urlopen", respond)
    with pytest.raises(urllib.error.HTTPError) as failure:
        release_services.registry_permissions("ghcr.io/owner/repo")
    assert failure.value.code == status
    assert len(requests) == failed_request
    output = capsys.readouterr().out
    assert f"GHCR {operation} (HTTP {status})" in output
    assert not any(secret in output for secret in (
        "credential-do-not-print", "registry-token-do-not-print", "secret-state", "secret-response"))


@pytest.mark.parametrize("location_request", [2, 3])
@pytest.mark.parametrize("location", ["http://ghcr.io/upload", "https://other.example/upload"])
def test_registry_preflight_never_sends_upload_credentials_to_an_unexpected_origin(
        monkeypatch, location_request, location):
    monkeypatch.setenv("GITHUB_ACTOR", "owner")
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    requests = []

    def respond(request, timeout):
        requests.append(request)
        response = io.BytesIO(b'{"token":"registry-token"}')
        response.status = 200 if len(requests) == 1 else 202
        response.headers = {"Location": location if len(requests) == location_request
                            else "/v2/owner/repo/blobs/uploads/probe"}
        return response

    monkeypatch.setattr(release_services.urllib.request, "urlopen", respond)
    with pytest.raises(ValueError, match="unexpected upload origin"):
        release_services.registry_permissions("ghcr.io/owner/repo")
    assert len(requests) == location_request
    assert all(urllib.parse.urlsplit(request.full_url).netloc == "ghcr.io" for request in requests)


@pytest.mark.parametrize("failed_request,message", [(2, "disposable upload"), (3, "blob transfer")])
def test_registry_preflight_rejects_incomplete_upload_stages(monkeypatch, failed_request, message):
    monkeypatch.setenv("GITHUB_ACTOR", "owner")
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    requests = []

    def respond(request, timeout):
        requests.append(request)
        response = io.BytesIO(b'{"token":"registry-token"}')
        response.status = 200 if len(requests) in (1, failed_request) else 202
        response.headers = {"Location": "/v2/owner/repo/blobs/uploads/probe"}
        return response

    monkeypatch.setattr(release_services.urllib.request, "urlopen", respond)
    with pytest.raises(ValueError, match=message):
        release_services.registry_permissions("ghcr.io/owner/repo")
    assert len(requests) == failed_request


def test_preflight_reports_all_service_failures_and_checks_stable_baseline(monkeypatch, capsys):
    backend = Backend("owner/repo")
    checked = []
    monkeypatch.setattr(release_services.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(backend, "run", lambda *args: None)
    monkeypatch.setattr(backend, "require_reconciled_channels", lambda *args: None)

    def denied(service):
        checked.append(service)
        raise urllib.error.HTTPError("https://example.com/?credential=secret", 403, "secret", {}, None)

    monkeypatch.setattr(release_services, "rehearse", lambda *args: denied("github"))
    monkeypatch.setattr(release_services, "registry_permissions", lambda *args: denied("registry"))
    monkeypatch.setattr(release_services, "pages_permissions", lambda *args: denied("pages"))
    monkeypatch.setattr(backend, "snapshot", lambda *args: denied("baseline"))
    with pytest.raises(RuntimeError) as failure:
        release_services.preflight(backend, "v1.2.3", "a" * 40, "publisher-contract-test", stable=True)
    assert checked == ["github", "registry", "pages", "baseline"]
    for service in ("GitHub contents write", "GHCR package push", "Cloudflare Pages Write",
                    "recoverable stable-channel baseline"):
        assert service in str(failure.value)
    output = capsys.readouterr().out
    assert "HTTP 403" in output
    assert "secret" not in output


def test_pages_write_check_requires_an_actual_grant(monkeypatch):
    backend = Backend("owner/repo")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr(backend, "api", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="upload token"):
        release_services.pages_permissions(backend)
