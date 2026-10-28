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
    (tmp_path / "flatpak-repo/summary").write_bytes(b"signed summary")
    return tmp_path


class FakeBackend:
    def __init__(self, latest="v1.2.2", fail=None):
        self.current = latest
        self.release = None
        self.manifest = None
        self.calls = []
        self.fail = fail
        self.checkpoint_record = None

    def require_reconciled_channels(self, tag):
        pass

    def recovery(self, release):
        return self.checkpoint_record

    def checkpoint(self, tag, record, root):
        self.record("checkpoint")
        self.checkpoint_record = record

    def clear_recovery(self, tag):
        self.record("clear-recovery")
        self.checkpoint_record = None

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
                                    "flatpak-repo/summary.sig", "flatpak-repo/summary"])
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
        assert backend.calls[-2:] == ["restore", "clear-recovery"]
    else:
        assert "flatpak" not in backend.calls
    backend.fail = None
    backend.calls.clear()
    promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls[-1] == "publish" and backend.flags == (False, True)


def test_success_publishes_last_and_retry_is_idempotent(candidate):
    backend = FakeBackend()
    promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls == ["stage", "version-images", "snapshot", "checkpoint", "flatpak", "latest-image", "publish"]
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


@pytest.mark.parametrize("committed", [False, True])
def test_confirmation_outage_keeps_durable_baseline_and_retry_reconciles(candidate, committed):
    class OutageBackend(FakeBackend):
        outage = False

        def existing(self, tag):
            if self.outage:
                raise TimeoutError("GitHub unreachable")
            return super().existing(tag)

        def publish(self, tag, **kwargs):
            if committed:
                super().publish(tag, **kwargs)
            self.outage = True
            # Also covers Backend.publish's read failing before its PATCH.
            raise TimeoutError("publication response unavailable")

    backend = OutageBackend()
    with pytest.raises(promotion.PublicationUnresolved, match="outcome unresolved"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert "restore" not in backend.calls
    saved = backend.checkpoint_record.copy()
    assert saved["previous"] == "previous-stable"
    assert saved["previous_latest"] == "v1.2.2"
    assert saved["manifest_sha256"] == promotion.manifest_digest(backend.manifest)

    # The checkpoint belongs to the remote draft, independent of the process or
    # workspace. A new runner must not snapshot the partly promoted channels.
    retry = FakeBackend()
    retry.release, retry.manifest = backend.release, backend.manifest
    retry.checkpoint_record = saved
    result = promotion.promote(candidate, "v1.2.3", IMAGE, retry)
    if committed:
        assert result == "already published"
        assert retry.calls == []
    else:
        assert retry.calls[:2] == ["restore", "clear-recovery"]
        assert retry.calls.count("publish") == 1
        assert result == "published stable"


def test_manifest_confirmation_failure_does_not_roll_back_a_public_release(candidate):
    class UnreachableManifest(FakeBackend):
        def publish(self, tag, **kwargs):
            super().publish(tag, **kwargs)
            raise TimeoutError("lost publication response")

        def published_manifest(self, release):
            raise OSError("manifest service unavailable")

    backend = UnreachableManifest()
    with pytest.raises(promotion.PublicationUnresolved, match="outcome unresolved"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert "restore" not in backend.calls
    assert backend.checkpoint_record is not None


def test_rollback_failure_retains_original_checkpoint_and_retries_before_new_snapshot(candidate):
    class RollbackFailure(FakeBackend):
        def restore(self, snapshot, image):
            super().restore(snapshot, image)
            raise OSError("rollback connection failed")

    backend = RollbackFailure(fail="publish")
    with pytest.raises(promotion.PublicationUnresolved, match="recovery incomplete"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.checkpoint_record["previous"] == "previous-stable"
    assert "clear-recovery" not in backend.calls
    backend.fail = None
    backend.calls.clear()
    with pytest.raises(promotion.PublicationUnresolved, match="recovery incomplete"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls == ["restore"]


def test_draft_recovery_uses_saved_manifest_when_retry_regenerates_flatpak_metadata(candidate):
    backend = FakeBackend(fail="publish")
    backend.restore = lambda *args: (_ for _ in ()).throw(OSError("rollback unavailable"))
    with pytest.raises(promotion.PublicationUnresolved):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    saved_manifest = backend.manifest
    saved_checkpoint = backend.checkpoint_record
    # A coordinator retry downloads the original native repositories and runs
    # build-update-repo again. Its timestamped summary bytes differ on this runner.
    (candidate / "flatpak-repo/summary").write_bytes(b"regenerated timestamped summary")
    (candidate / "flatpak-repo/summary.idx").write_bytes(b"regenerated summary index")
    retry = FakeBackend()
    retry.release, retry.manifest = backend.release, saved_manifest
    retry.checkpoint_record = saved_checkpoint
    assert promotion.promote(candidate, "v1.2.3", IMAGE, retry) == "published stable"
    assert retry.calls[:2] == ["restore", "clear-recovery"]
    assert retry.manifest != saved_manifest
    assert retry.calls.index("restore") < retry.calls.index("stage") < retry.calls.index("snapshot")


def test_recovery_cannot_trust_a_draft_with_a_different_saved_manifest(candidate):
    backend = FakeBackend(fail="publish")
    backend.restore = lambda *args: (_ for _ in ()).throw(OSError("offline"))
    with pytest.raises(promotion.PublicationUnresolved):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    backend.manifest = {**backend.manifest, "version": "different"}
    backend.calls.clear()
    with pytest.raises(promotion.PublicationUnresolved, match="saved draft manifest"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls == []


def test_checkpoint_failure_happens_before_stable_writes(candidate):
    backend = FakeBackend(fail="checkpoint")
    with pytest.raises(RuntimeError, match="checkpoint"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert not {"flatpak", "latest-image", "publish", "restore"}.intersection(backend.calls)


def test_timed_out_channel_command_retains_checkpoint_for_reconciliation(candidate):
    backend = FakeBackend()

    def timeout(*args):
        raise promotion.PublicationUnresolved("channel command timed out")

    backend.latest_image = timeout
    with pytest.raises(promotion.PublicationUnresolved, match="timed out"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.checkpoint_record is not None
    assert "restore" not in backend.calls


def test_later_stable_release_prevents_stale_recovery_rollback(candidate):
    backend = FakeBackend(fail="publish")
    # Seed an interrupted recovery without permitting its rollback to complete.
    backend.restore = lambda *args: (_ for _ in ()).throw(OSError("offline"))
    with pytest.raises(promotion.PublicationUnresolved):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    backend.current = "v1.3.0"
    backend.calls.clear()
    with pytest.raises(promotion.PublicationUnresolved, match="Stable release changed"):
        promotion.promote(candidate, "v1.2.3", IMAGE, backend)
    assert backend.calls == []


def test_different_unresolved_draft_blocks_a_new_tag(monkeypatch):
    backend = promotion.Backend("owner/repo")
    monkeypatch.setattr(backend, "releases", lambda: iter([
        {"tag_name": "v1.2.2", "draft": True,
         "assets": [{"name": promotion.RECOVERY_ASSET}]},
    ]))
    with pytest.raises(promotion.PublicationUnresolved, match="Reconcile v1.2.2"):
        backend.require_reconciled_channels("v1.2.3")
    backend.require_reconciled_channels("v1.2.2")


def test_draft_lookup_requires_successful_authenticated_pagination(monkeypatch):
    backend = promotion.Backend("owner/repo")
    paths = []
    draft = {"id": 123, "tag_name": "v1.2.3", "draft": True, "assets": []}

    def api(path):
        paths.append(path)
        if "/tags/" in path:
            return None
        if "&page=1" in path:
            return [{"tag_name": f"v0.0.{n}"} for n in range(100)]
        return [draft]

    monkeypatch.setattr(backend, "api", api)
    assert backend.existing("v1.2.3") == draft
    assert paths[-1].endswith("page=2")
    monkeypatch.setattr(backend, "api", lambda path: None)
    with pytest.raises(RuntimeError, match="authenticated listing"):
        backend.existing("v1.2.3")


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


@pytest.mark.parametrize("bad_arch,failure", [(None, None), ("x86_64", "version"),
                                              ("aarch64", "version"), ("aarch64", "architecture"),
                                              ("aarch64", "signature")])
def test_public_verification_downloads_bytes_and_checks_signed_architectures(candidate, monkeypatch, bad_arch, failure):
    import io
    import subprocess
    import release_services

    (candidate / "flatpak-repo/summary").write_bytes(b"signed summary")
    (candidate / "flatpak-repo/tuneconsole.flatpakrepo").write_text("trusted descriptor")
    _, digests, manifest = promotion.validate(candidate, "v1.2.3", IMAGE)
    (candidate / "release-manifest.json").write_text(json.dumps(manifest))
    base = "https://github.com/reubenfirmin/TuneConsole/releases/download/v1.2.3/"
    urls = []

    def public_response(request, timeout):
        # Cloudflare rejects Python's default user agent with error 1010. Public
        # checks identify the client while remaining independent of CI tokens.
        assert request.get_header("User-agent").startswith("TuneConsole-release-verifier/")
        assert request.get_header("Authorization") is None
        url = request.full_url
        urls.append(url)
        assert timeout == 30
        if url.startswith(base):
            return io.BytesIO((candidate / url.removeprefix(base)).read_bytes())
        assert url.startswith("https://repo.tuneconsole.com/")
        return io.BytesIO((candidate / "flatpak-repo" / url.rsplit("/", 1)[1]).read_bytes())

    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if args[0] == "docker":
            assert kwargs["env"]["DOCKER_CONFIG"] != str(Path.home() / ".docker")
            data = {"manifests": [
                {"platform": {"os": "linux", "architecture": arch}, "digest": digest.split("@")[1]}
                for arch, digest in zip(("amd64", "arm64"), digests)]}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(data))
        assert "--user" in args and "--no-gpg-verify" not in args
        assert kwargs["env"]["XDG_DATA_HOME"].startswith("/tmp/")
        if args[1] == "update":
            arch = next(arg.removeprefix("--arch=") for arg in args if arg.startswith("--arch="))
            if arch == bad_arch and failure == "signature":
                raise subprocess.CalledProcessError(1, args, stderr="Untrusted summary signature")
            version = "0.1.8" if arch == bad_arch and failure == "version" else "1.2.3"
            bundle_arch = "wrong" if arch == bad_arch and failure == "architecture" else arch
            catalog = Path(kwargs["env"]["XDG_DATA_HOME"]) / "flatpak/appstream/release-verification" / arch / "active/appstream.xml"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(f'<components><component><id>com.tuneconsole.TuneConsole</id>'
                               f'<releases><release version="{version}"/></releases>'
                               f'<bundle type="flatpak">app/com.tuneconsole.TuneConsole/{bundle_arch}/stable</bundle>'
                               '</component></components>')
        return subprocess.CompletedProcess(args, 0, stdout="com.tuneconsole.TuneConsole\n")

    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-sent-to-public-endpoints")
    monkeypatch.setattr(release_services.urllib.request, "urlopen", public_response)
    monkeypatch.setattr(release_services.subprocess, "run", run)
    backend = FakeBackend(latest="v1.2.3")
    backend.repository = "reubenfirmin/TuneConsole"
    if bad_arch:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            release_services.verify_public(candidate, "v1.2.3", backend)
        return
    release_services.verify_public(candidate, "v1.2.3", backend)
    assert len([url for url in urls if url.startswith(base)]) == len(manifest["sha256"]) + 1
    assert {args[-1].split(":")[-1] for args in commands if args[0] == "docker"} == {"1.2.3", "v1.2.3", "latest"}
    assert {arg for args in commands for arg in args if arg.startswith("--arch=")} == {"--arch=x86_64", "--arch=aarch64"}


def test_public_download_checksum_failure_is_visible(monkeypatch):
    import io
    import release_services

    monkeypatch.setattr(release_services.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"incorrect"))
    with pytest.raises(ValueError, match="Public download checksum differs"):
        release_services.public_download("https://example.test/asset", "a" * 64)


def test_complete_final_version_provenance_preserves_the_tested_payloads(candidate):
    import tarfile
    import candidate_provenance

    for arch in ("x86_64", "aarch64"):
        with tarfile.open(candidate / f"flatpak-repo-{arch}.tar", "w") as archive:
            archive.add(candidate / "flatpak-repo", arcname="repo")
    with tarfile.open(candidate / "flatpak-repository.tar", "w") as archive:
        archive.add(candidate / "flatpak-repo", arcname="flatpak-repo")
    result = candidate_provenance.create(candidate, "v1.2.3", "a" * 40, "123", "reubenfirmin/TuneConsole")
    assert result["version"] == "1.2.3"
    assert "flatpak-repo-aarch64.tar" in result["sha256"]
    assert candidate_provenance.verify(candidate, "v1.2.3", "a" * 40, "123", "reubenfirmin/TuneConsole") == result
