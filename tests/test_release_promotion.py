"""Release failure boundaries: no live channels until every staged artifact validates."""
import base64
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
spec = importlib.util.spec_from_file_location("promote_release", Path(__file__).parents[1] / "packaging/promote_release.py")
promotion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promotion)
IMAGE = "ghcr.io/reubenfirmin/tuneconsole"


@pytest.fixture
def candidate(tmp_path):
    version = "1.2.3"
    for name in ("TuneConsole.flatpak", "TuneConsole-aarch64.flatpak", "TuneConsole-macos.dmg",
                 "TuneConsole-macos-arm64.dmg", "TuneConsole-macos-x86_64.dmg"):
        (tmp_path / name).write_bytes(b"archive")
    for arch in ("arm64", "x86_64"):
        (tmp_path / f"appcast-macos-{arch}.xml").write_text(f'''
<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel><item>
<sparkle:version>{version}</sparkle:version><enclosure
 url="https://github.com/reubenfirmin/TuneConsole/releases/download/v{version}/TuneConsole-macos-{arch}.dmg"
 length="7" sparkle:edSignature="{base64.b64encode(bytes(64)).decode()}"/>
</item></channel></rss>''')
    with zipfile.ZipFile(tmp_path / f"yt_playlist-{version}-py3-none-any.whl", "w") as archive:
        archive.writestr(f"yt_playlist-{version}.dist-info/METADATA", f"Name: yt-playlist\nVersion: {version}\n")
    for arch in ("amd64", "arm64"):
        (tmp_path / f"container-{arch}.json").write_text(json.dumps({
            "arch": arch, "version": version, "digest": IMAGE + "@sha256:" + ("a" if arch == "amd64" else "b") * 64}))
    for arch in ("x86_64", "aarch64"):
        ref = tmp_path / f"flatpak-repo/refs/heads/app/com.tuneconsole.TuneConsole/{arch}/master"
        ref.parent.mkdir(parents=True)
        ref.write_text("c" * 64)
    (tmp_path / "flatpak-repo/summary.sig").write_bytes(b"signed")
    return tmp_path


class FakeBackend:
    def __init__(self, latest="v1.2.2", fail=None):
        self.current = latest
        self.release = None
        self.manifest = None
        self.calls = []
        self.fail = fail

    def existing(self, tag):
        return self.release

    def latest(self):
        return self.current

    def published_manifest(self, release):
        return self.manifest

    def record(self, action):
        self.calls.append(action)
        if action == self.fail:
            raise RuntimeError(action)

    def stage(self, tag, manifest, root):
        self.manifest = manifest
        self.release = {"draft": True}
        self.record("stage")

    def version_images(self, release, image, digests):
        assert len(digests) == 2 and all("@sha256:" in d for d in digests)
        self.record("version-images")

    def snapshot(self, image):
        self.record("snapshot")
        return "previous-stable"

    def flatpak(self, root):
        self.record("flatpak")

    def latest_image(self, image, version):
        self.record("latest-image")

    def publish(self, tag, *, prerelease, latest):
        self.record("publish")
        self.release = {"draft": False}
        self.flags = prerelease, latest

    def restore(self, snapshot, image):
        assert snapshot == "previous-stable"
        self.record("restore")


@pytest.mark.parametrize("missing", ["TuneConsole.flatpak", "TuneConsole-macos-x86_64.dmg",
                                    "appcast-macos-arm64.xml", "container-arm64.json",
                                    "flatpak-repo/summary.sig"])
def test_failed_platform_or_missing_artifact_never_writes_a_public_destination(candidate, missing):
    (candidate / missing).unlink()
    backend = FakeBackend()
    with pytest.raises(ValueError):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls == []


@pytest.mark.parametrize("failure", ["stage", "version-images", "flatpak", "latest-image", "publish"])
def test_failures_keep_github_draft_and_restore_changed_channels(candidate, failure):
    backend = FakeBackend(fail=failure)
    with pytest.raises(RuntimeError, match=failure):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.release["draft"]
    if failure in ("flatpak", "latest-image", "publish"):
        assert backend.calls[-1] == "restore"
    else:
        assert "flatpak" not in backend.calls
    backend.fail = None
    backend.calls.clear()
    promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls[-1] == "publish" and backend.flags == (False, True)


def test_success_publishes_last_and_retry_is_idempotent(candidate):
    backend = FakeBackend()
    promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls == ["stage", "version-images", "snapshot", "flatpak", "latest-image", "publish"]
    backend.calls.clear()
    assert promotion.promote(candidate, "v1.2.3", IMAGE, backend) == "already published"
    assert backend.calls == []
    (candidate / "TuneConsole.flatpak").write_bytes(b"different candidate")
    with pytest.raises(ValueError, match="immutable"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)


def test_old_concurrent_run_publishes_version_without_regressing_channels(candidate):
    backend = FakeBackend(latest="v1.3.0")
    promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls == ["stage", "version-images", "publish"]
    assert backend.flags == (False, False)


def test_lost_publication_response_confirms_public_release_without_rollback(candidate):
    class LostResponseBackend(FakeBackend):
        def publish(self, tag, **kwargs):
            super().publish(tag, **kwargs)
            raise TimeoutError("GitHub committed before the connection closed")

    backend = LostResponseBackend()
    assert promotion.promote(candidate, "v1.2.3", IMAGE, backend) == "publication confirmed after lost response"
    assert not backend.release["draft"] and "restore" not in backend.calls


def test_published_repository_bytes_are_immutable_too(candidate):
    backend = FakeBackend()
    promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    (candidate / "flatpak-repo/summary.sig").write_bytes(b"different signed repository")
    with pytest.raises(ValueError, match="immutable"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)


def test_backend_promotes_the_two_tested_digests_without_rebuilding(monkeypatch):
    backend = promotion.Backend("reubenfirmin/TuneConsole")
    commands = []
    monkeypatch.setattr(backend, "run", lambda *args: commands.append(args))
    digests = [IMAGE + "@sha256:" + letter * 64 for letter in ("a", "b")]
    backend.version_images(promotion.classify("v1.2.3"), IMAGE, digests)
    backend.latest_image(IMAGE, "1.2.3")
    assert commands == [
        ("docker", "buildx", "imagetools", "create", "--tag", IMAGE + ":1.2.3",
         "--tag", IMAGE + ":v1.2.3", *digests),
        ("docker", "buildx", "imagetools", "create", "--tag", IMAGE + ":latest", IMAGE + ":1.2.3"),
    ]


@pytest.mark.parametrize("published", [True, False])
def test_backend_finds_published_releases_and_drafts(monkeypatch, published):
    backend = promotion.Backend("reubenfirmin/TuneConsole")
    release = {"id": 42, "tag_name": "v1.2.3", "draft": not published}
    paths = []

    def api(path):
        paths.append(path)
        if "/releases/tags/" in path:
            return release if published else None
        return [release]

    monkeypatch.setattr(backend, "api", api)
    assert backend.existing("v1.2.3") == release
    assert len(paths) == (1 if published else 2)


def test_backend_finds_a_draft_beyond_the_first_page(monkeypatch):
    backend = promotion.Backend("reubenfirmin/TuneConsole")
    draft = {"id": 42, "tag_name": "v1.2.3", "draft": True}

    def api(path):
        if "/releases/tags/" in path:
            return None
        if path.endswith("page=1"):
            return [{"tag_name": f"v0.0.{i}"} for i in range(100)]
        assert path.endswith("page=2")
        return [draft]

    monkeypatch.setattr(backend, "api", api)
    assert backend.existing("v1.2.3") == draft


def test_backend_distinguishes_a_missing_release_from_a_draft(monkeypatch):
    backend = promotion.Backend("reubenfirmin/TuneConsole")
    monkeypatch.setattr(backend, "api", lambda path: None if "/tags/" in path else [])
    assert backend.existing("v1.2.3") is None


def test_backend_restores_both_channels_even_if_one_rollback_fails(monkeypatch):
    backend = promotion.Backend("reubenfirmin/TuneConsole")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    commands = []

    def fail_api(path, **kwargs):
        assert path.endswith("/deployments/previous/rollback")
        assert kwargs == {"method": "POST", "cloudflare": True}
        raise OSError("Cloudflare temporarily unavailable")

    monkeypatch.setattr(backend, "api", fail_api)
    monkeypatch.setattr(backend, "run", lambda *args: commands.append(args))
    with pytest.raises(RuntimeError, match="Flatpak rollback failed"):
        backend.restore({"deployment": "previous", "digest": "sha256:" + "c" * 64}, IMAGE)
    assert commands == [("docker", "buildx", "imagetools", "create", "--tag", IMAGE + ":latest",
                         IMAGE + "@sha256:" + "c" * 64)]


def test_wrong_container_version_fails_before_upload(candidate):
    path = candidate / "container-arm64.json"
    data = json.loads(path.read_text())
    data["version"] = "0.0.0.dev0"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="container digest"):
        promotion.promote(candidate, "v1.2.3", IMAGE, FakeBackend())


@pytest.mark.parametrize("tag,version", [("v1.2.3-rc.1", "1.2.3rc1"), ("v1.2.3.dev2", "1.2.3.dev2")])
def test_prerelease_publishes_exact_version_without_touching_any_stable_destination(candidate, tag, version):
    (candidate / "yt_playlist-1.2.3-py3-none-any.whl").unlink()
    with zipfile.ZipFile(candidate / f"yt_playlist-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"yt_playlist-{version}.dist-info/METADATA", f"Name: yt-playlist\nVersion: {version}\n")
    for arch in ("arm64", "x86_64"):
        path = candidate / f"appcast-macos-{arch}.xml"
        path.write_text(path.read_text().replace("v1.2.3/", tag + "/").replace(">1.2.3<", f">{version}<"))
    for arch in ("amd64", "arm64"):
        path = candidate / f"container-{arch}.json"
        data = json.loads(path.read_text())
        data["version"] = version
        path.write_text(json.dumps(data))
    backend = FakeBackend()
    promotion.promote(candidate, tag, IMAGE, backend)
    assert backend.calls == ["stage", "version-images", "publish"]
    assert backend.flags == (True, False)
