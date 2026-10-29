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
from rehearse_publisher import cleanup, rehearse
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
        self.listing_lag = 0
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
        elif path == "/repos/owner/repo/releases/12":
            result = self.release
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
                if self.release and self.listing_lag:
                    self.listing_lag -= 1
                    result = []
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

    fake_time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    monkeypatch.setattr(promote_release, "time", fake_time)
    monkeypatch.setattr("rehearse_publisher.time", fake_time)
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


def test_created_draft_discovery_recovers_only_after_confirming_the_exact_resource(monkeypatch, capsys, lookup_clock):
    github = GitHub(monkeypatch)
    github.listing_lag = 2
    rehearse(github.backend, "publisher-contract-lag", "a" * 40)
    assert lookup_clock.sleeps == [1, 1]
    direct = [(method, path) for method, path, _ in github.requests
              if path == "/repos/owner/repo/releases/12"]
    assert direct == [("GET", "/repos/owner/repo/releases/12")] * 2 + [
        ("DELETE", "/repos/owner/repo/releases/12")]
    assert len(github.commands) == 3
    output = capsys.readouterr().out
    assert '"attempt": 3' in output and '"created_id": 12' in output
    assert "visible by ID" in output and "fixture-token" not in output


def test_persistent_discovery_failure_is_bounded_and_cleans_up_by_created_id(monkeypatch, lookup_clock):
    github = GitHub(monkeypatch)
    github.listing_lag = 100
    with pytest.raises(RuntimeError, match="lookup failed after 10 seconds"):
        rehearse(github.backend, "publisher-contract-invisible", "a" * 40)
    assert lookup_clock.sleeps == [1] * 10
    assert github.release is None and github.commands == []
    assert github.requests[-2:] == [
        ("GET", "/repos/owner/repo/releases/12", {}),
        ("DELETE", "/repos/owner/repo/releases/12", {})]


@pytest.mark.parametrize("hidden_reads", [0, 3])
def test_lost_create_response_still_cleans_up_the_authenticated_draft(monkeypatch, lookup_clock, hidden_reads):
    github = GitHub(monkeypatch)
    github.hide_created_reads = hidden_reads
    send = github.urlopen

    def lose_reply(request, timeout):
        result = send(request, timeout)
        if request.method == "POST":
            raise urllib.error.URLError("reply lost after creation")
        return result

    monkeypatch.setattr("urllib.request.urlopen", lose_reply)
    with pytest.raises(urllib.error.URLError, match="reply lost"):
        rehearse(github.backend, "publisher-contract-lost-reply", "a" * 40)
    assert github.release is None and github.commands == []
    assert github.requests[-1][0] == "DELETE"
    assert lookup_clock.sleeps == [1] * hidden_reads


@pytest.mark.parametrize("change", [{"draft": False}, {"tag_name": "v1.2.3"}, {"id": 99}])
def test_cleanup_never_deletes_a_changed_or_public_created_release(monkeypatch, change):
    github = GitHub(monkeypatch)
    created = {"id": 12, "tag_name": "publisher-contract-test", "draft": True, "assets": []}
    github.release = {**created, **change}
    with pytest.raises(RuntimeError, match="identity or draft state differs"):
        cleanup(github.backend, "publisher-contract-test", created=created)
    assert github.release is not None
    assert not any(method == "DELETE" for method, _, _ in github.requests)


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
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
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


def test_pages_write_check_requires_an_actual_grant(monkeypatch):
    backend = Backend("owner/repo")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr(backend, "api", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="upload token"):
        release_services.pages_permissions(backend)


class RegistryResponse(io.BytesIO):
    def __init__(self, status, payload=b"", headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {}


class Registry:
    location = "/v2/owner/repo/blobs/uploads/disposable?opaque=do-not-print-upload"
    next_location = "/v2/owner/repo/blobs/uploads/next?_state=updated%2Bstate%2Fvalue"

    def __init__(self, monkeypatch):
        self.requests = []
        self.failure = None
        self.failure_status = 403
        self.statuses = [200, 202, 202, 201]
        monkeypatch.setenv("GITHUB_ACTOR", "fixture-actor")
        monkeypatch.setenv("GITHUB_TOKEN", "do-not-print-credential")
        monkeypatch.setattr(release_services.urllib.request, "urlopen", self.urlopen)

    def urlopen(self, request, timeout):
        assert timeout == 30
        stage = len(self.requests)
        self.requests.append(request)
        if self.failure == stage:
            raise urllib.error.HTTPError(request.full_url, self.failure_status,
                "do-not-print-reason", {"Secret": "do-not-print-header"},
                io.BytesIO(b"do-not-print-body"))
        return RegistryResponse(self.statuses[stage],
            b'{"token":"do-not-print-bearer"}' if stage == 0 else b"",
            {"Location": self.location if stage == 1 else self.next_location} if stage in (1, 2) else {})


@pytest.mark.parametrize("location", [
    "/v2/owner/repo/blobs/uploads/disposable?opaque=do-not-print-upload",
    "https://ghcr.io/v2/owner/repo/blobs/uploads/disposable?opaque=do-not-print-upload"])
def test_registry_transfers_blob_and_completes_the_updated_session(monkeypatch, capsys, location):
    registry = Registry(monkeypatch)
    registry.location = location
    release_services.registry_permissions("ghcr.io/owner/repo")
    token, upload, transfer, complete = registry.requests
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(token.full_url).query) == {
        "service": ["ghcr.io"], "scope": ["repository:owner/repo:pull,push"]}
    assert token.get_header("Authorization").startswith("Basic ")
    assert upload.method == "POST" and upload.data == b""
    assert upload.full_url == "https://ghcr.io/v2/owner/repo/blobs/uploads/"
    assert transfer.method == "PATCH" and transfer.data == b"{}"
    assert transfer.get_header("Content-range") == "0-1"
    assert transfer.full_url == urllib.parse.urljoin("https://ghcr.io/", registry.location)
    assert complete.method == "PUT" and complete.data == b""
    assert complete.full_url.startswith("https://ghcr.io" + registry.next_location + "&digest=")
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(complete.full_url).query) == {
        "_state": ["updated+state/value"],
        "digest": ["sha256:" + hashlib.sha256(b"{}").hexdigest()]}
    for request in (upload, transfer, complete):
        assert request.get_header("Authorization") == "Bearer do-not-print-bearer"
        assert request.get_header("Content-type") == "application/octet-stream"
    output = capsys.readouterr().out
    assert "token request: HTTP 200" in output
    assert "upload initiation: HTTP 202" in output
    assert "blob transfer: HTTP 202" in output
    assert "upload completion: HTTP 201" in output
    assert "do-not-print" not in output


@pytest.mark.parametrize("stage,operation", [
    (0, "token request"), (1, "upload initiation"),
    (2, "blob transfer"), (3, "upload completion")])
@pytest.mark.parametrize("status", [401, 403, 404, 405, 503])
def test_registry_failure_identifies_the_operation_and_status_without_remote_details(monkeypatch, capsys, stage, operation, status):
    import traceback

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    registry = Registry(monkeypatch)
    registry.failure = stage
    registry.failure_status = status
    backend = Backend("owner/repo")
    monkeypatch.setattr(release_services.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(backend, "run", lambda *args: None)
    monkeypatch.setattr(release_services, "rehearse", lambda *args: None)
    pages = []
    monkeypatch.setattr(release_services, "pages_permissions", lambda *args: pages.append(True))
    with pytest.raises(RuntimeError, match="GHCR package push") as failure:
        release_services.preflight(backend, "v1.2.3", "a" * 40, "probe", stable=False)
    output = capsys.readouterr().out + "".join(traceback.format_exception(failure.value))
    assert f"GHCR {operation} (HTTP {status})" in output
    assert "do-not-print" not in output and "blobs/uploads" not in output
    assert len(registry.requests) == stage + 1 and pages == [True]


@pytest.mark.parametrize("stage,status", [(0, 202), (1, 200), (2, 200), (3, 200), (3, 202)])
def test_registry_requires_acceptance_and_confirmed_completion(monkeypatch, stage, status):
    registry = Registry(monkeypatch)
    registry.statuses[stage] = status
    with pytest.raises(release_services.RegistryPreflightError) as failure:
        release_services.registry_permissions("ghcr.io/owner/repo")
    assert failure.value.status == status
    assert len(registry.requests) == stage + 1


@pytest.mark.parametrize("location", [
    "http://ghcr.io/v2/owner/repo/blobs/uploads/disposable",
    "https://elsewhere.invalid/v2/owner/repo/blobs/uploads/disposable",
    "/v2/owner/repo/manifests/latest", "/v2/other/repo/blobs/uploads/disposable",
    "/v2/owner/repo/blobs/uploads/", "/v2/owner/repo/blobs/uploads/disposable#fragment"])
@pytest.mark.parametrize("stage", [1, 2])
def test_registry_only_targets_the_returned_https_upload(monkeypatch, location, stage):
    registry = Registry(monkeypatch)
    if stage == 1:
        registry.location = location
    else:
        registry.next_location = location
    with pytest.raises(release_services.RegistryPreflightError, match="location validation"):
        release_services.registry_permissions("ghcr.io/owner/repo")
    assert len(registry.requests) == stage + 1


def test_preflight_reports_all_service_failures_and_checks_stable_baseline(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
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
    assert "HTTP 403" in output and "secret" not in output


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "workflow_call", "release"])
def test_non_pr_preflight_never_runs_publisher_or_registry_rehearsals(monkeypatch, event):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    backend = Backend("owner/repo")
    checked = []
    monkeypatch.setattr(release_services.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(backend, "run", lambda *args: None)
    monkeypatch.setattr(release_services, "rehearse", lambda *args: pytest.fail("Publisher tests run only on PRs"))
    monkeypatch.setattr(release_services, "registry_permissions", lambda *args: pytest.fail("Registry tests run only on PRs"))
    monkeypatch.setattr(release_services, "pages_permissions", lambda *args: checked.append("pages"))
    monkeypatch.setattr(backend, "require_reconciled_channels", lambda *args: checked.append("recovery"))
    monkeypatch.setattr(backend, "snapshot", lambda *args: checked.append("baseline"))
    release_services.preflight(backend, "v1.2.3", "a" * 40, None, stable=True)
    assert checked == ["pages", "recovery", "baseline"]


@pytest.mark.parametrize("streamed", [False, True])
def test_public_downloads_use_an_anonymous_identified_request(monkeypatch, streamed):
    payload = b"signed repository metadata" * 50000
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-send")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "do-not-send")
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        assert timeout == 30
        assert request.get_method() == "GET"
        assert request.data is None
        assert dict(request.header_items()) == {"User-agent": release_services.PUBLIC_USER_AGENT}
        # Model the public CDN denying urllib's default Python user agent.
        assert not request.get_header("User-agent").startswith("Python-urllib/")
        return io.BytesIO(payload)

    monkeypatch.setattr(release_services.urllib.request, "urlopen", urlopen)
    url = "https://repo.tuneconsole.com/summary"
    if streamed:
        release_services.public_download(url, hashlib.sha256(payload).hexdigest())
        with pytest.raises(ValueError, match="checksum differs"):
            release_services.public_download(url, "0" * 64)
    else:
        assert release_services.public_bytes(url) == payload
    assert requests


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("status", [403, 404, 503, None])
def test_public_failure_reports_operation_and_status_without_remote_secrets(monkeypatch, streamed, status):
    import traceback

    url = "https://do-not-print-user:do-not-print-password@repo.tuneconsole.com/summary?token=do-not-print-query#secret"

    def denied(request, timeout):
        if status is None:
            raise urllib.error.URLError("do-not-print-reason")
        raise urllib.error.HTTPError(request.full_url, status, "do-not-print-reason",
                                     {"Secret": "do-not-print-header"}, io.BytesIO(b"do-not-print-body"))

    monkeypatch.setattr(release_services.urllib.request, "urlopen", denied)
    with pytest.raises(release_services.PublicVerificationError) as failure:
        if streamed:
            release_services.public_download(url, "0" * 64)
        else:
            release_services.public_bytes(url)
    output = "".join(traceback.format_exception(failure.value))
    assert "Public download GET https://repo.tuneconsole.com/summary" in output
    if status is not None:
        assert f"HTTP {status}" in output
    else:
        assert "failed" in output
    assert "do-not-print" not in output


@pytest.mark.parametrize("changed", ["summary", "summary.sig"])
def test_public_flatpak_rejects_candidate_hash_mismatch_before_trusting_remote(monkeypatch, tmp_path, changed):
    checksums = {name: hashlib.sha256(name.encode()).hexdigest() for name in ("summary", "summary.sig")}
    monkeypatch.setattr(release_services, "public_bytes",
                        lambda url: b"altered" if url.endswith(changed) else url.rsplit("/", 1)[-1].encode())

    def no_remote(*args, **kwargs):
        pytest.fail("Do not configure a remote before candidate hashes match")

    monkeypatch.setattr(release_services.subprocess, "run", no_remote)
    with pytest.raises(ValueError, match=f"Public Flatpak {changed} differs from signed candidate"):
        release_services.verify_flatpak(tmp_path, "1.2.3", checksums, {})
