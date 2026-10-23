"""Exercise real Backend request/response handling across GitHub draft boundaries."""
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
from promote_release import Backend
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
        monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
        monkeypatch.setattr("urllib.request.urlopen", self.urlopen)
        self.backend = Backend("owner/repo")
        monkeypatch.setattr(self.backend, "run", self.run)

    def urlopen(self, request, timeout):
        assert timeout == 60
        assert request.get_header("Authorization") == "Bearer fixture-token"
        path = urllib.parse.urlsplit(request.full_url).path
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.requests.append((request.method, path, query))
        if self.unreachable:
            raise urllib.error.URLError("offline")
        if request.method == "POST":
            data = json.loads(request.data)
            self.release = {"id": 12, "tag_name": data["tag_name"], "draft": data["draft"], "assets": []}
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
            else:
                result = [self.release] if self.release else []
        return io.BytesIO(json.dumps(result).encode())

    def run(self, *args):
        self.commands.append(args)
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


def test_draft_create_lookup_partial_upload_retry_and_cleanup(monkeypatch):
    github = GitHub(monkeypatch)
    rehearse(github.backend, "publisher-contract-test", "a" * 40)
    assert github.release is None
    assert len(github.commands) == 3  # partial stage, complete stage, idempotent repeat
    assert any(query.get("page") == ["2"] for _, _, query in github.requests)
    assert github.requests[-1][0] == "DELETE"


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
def test_failed_lookup_is_not_release_absence(monkeypatch, status):
    github = GitHub(monkeypatch)

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "lookup failed", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(urllib.error.HTTPError) as failure:
        github.backend.existing("v1.2.3")
    assert failure.value.code == status
    assert github.commands == []


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
    monkeypatch.setattr(release_services.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(backend, "run", lambda *args: None)
    monkeypatch.setattr(release_services, "rehearse", lambda *args: None)

    def denied(image):
        raise urllib.error.URLError("credential=do-not-print")

    monkeypatch.setattr(release_services, "registry_permissions", denied)
    with pytest.raises(RuntimeError, match="GHCR package push"):
        release_services.preflight(backend, "v1.2.3", "a" * 40, "publisher-contract-test", stable=True)
    output = capsys.readouterr().out
    assert "GHCR package push" in output
    assert "do-not-print" not in output


def test_pages_write_check_requires_an_actual_grant(monkeypatch):
    backend = Backend("owner/repo")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr(backend, "api", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="upload token"):
        release_services.pages_permissions(backend)
