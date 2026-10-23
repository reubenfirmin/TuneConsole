"""Validate a complete candidate and promote it under the workflow's shared release lock."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

from release_policy import classify
from macos.verify_appcast import verify


def validate(directory, tag, image):
    release = classify(tag)
    root = Path(directory)
    names = {"TuneConsole.flatpak", "TuneConsole-aarch64.flatpak", "TuneConsole-macos.dmg",
             "TuneConsole-macos-arm64.dmg", "TuneConsole-macos-x86_64.dmg",
             "appcast-macos-arm64.xml", "appcast-macos-x86_64.xml",
             "container-amd64.json", "container-arm64.json"}
    wheels = list(root.glob("yt_playlist-*.whl"))
    if len(wheels) != 1:
        raise ValueError("Expected exactly one release wheel")
    wheel = wheels[0]
    if wheel.name != f"yt_playlist-{release.version}-py3-none-any.whl":
        raise ValueError("Wheel filename does not match the release version")
    names.add(wheel.name)
    repo = root / "flatpak-repo"
    for arch in ("x86_64", "aarch64"):
        ref = repo / f"refs/heads/app/com.tuneconsole.TuneConsole/{arch}/master"
        if not ref.is_file() or not re.fullmatch(r"[a-f0-9]{64}\s*", ref.read_text()):
            raise ValueError(f"Missing signed repository architecture: {arch}")
    if not (repo / "summary.sig").is_file():
        raise ValueError("Missing signed Flatpak repository summary")
    for name in names:
        if not (root / name).is_file() or (root / name).stat().st_size == 0:
            raise ValueError(f"Missing release artifact: {name}")
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(f"yt_playlist-{release.version}.dist-info/METADATA").decode()
        if f"\nVersion: {release.version}\n" not in metadata:
            raise ValueError("Wheel metadata version does not match")
    for arch in ("arm64", "x86_64"):
        verify(root / f"appcast-macos-{arch}.xml", root / f"TuneConsole-macos-{arch}.dmg", tag, arch)
    if (root / "TuneConsole-macos.dmg").read_bytes() != (root / "TuneConsole-macos-arm64.dmg").read_bytes():
        raise ValueError("Default macOS download must match the validated ARM64 archive")
    digests = []
    for arch in ("amd64", "arm64"):
        candidate = json.loads((root / f"container-{arch}.json").read_text())
        digest = candidate.get("digest", "")
        if (candidate.get("version") != release.version or candidate.get("arch") != arch
                or not re.fullmatch(re.escape(image) + r"@sha256:[a-f0-9]{64}", digest)):
            raise ValueError(f"Invalid tested container digest for {arch}")
        digests.append(digest)
    checksums = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(names)}
    repository_checksums = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in sorted(repo.rglob("*")) if path.is_file()}
    return release, digests, {"tag": tag, "version": release.version, "sha256": checksums,
                              "flatpak_repository_sha256": repository_checksums}


def newer_than(tag, latest):
    if not latest:
        return True
    current = classify(latest)
    if current.channel != "stable":
        raise ValueError("The current latest release must be stable")
    return tuple(map(int, classify(tag).version.split("."))) > tuple(map(int, current.version.split(".")))


def promote(root, tag, image, backend):
    release, digests, manifest = validate(root, tag, image)
    # Fail before any write if the tag was already published with different bytes.
    existing = backend.existing(tag)
    if existing and not existing["draft"]:
        if backend.published_manifest(existing) != manifest:
            raise ValueError("Published release is immutable; candidate differs")
        return "already published"
    stable = release.channel == "stable" and newer_than(tag, backend.latest())
    backend.stage(tag, manifest, root)
    backend.version_images(release, image, digests)
    previous = backend.snapshot(image) if stable else None
    publishing = False
    try:
        if stable:
            backend.flatpak(root)
            backend.latest_image(image, release.version)
        # GitHub latest is changed last, after every asset and channel operation succeeds.
        publishing = True
        backend.publish(tag, prerelease=release.channel != "stable", latest=stable)
    except BaseException:
        # A lost response can follow a successful GitHub commit. Confirm before rolling back.
        if publishing:
            published = backend.existing(tag)
            if published and not published["draft"] and backend.published_manifest(published) == manifest:
                return "publication confirmed after lost response"
        if stable:
            backend.restore(previous, image)
        raise
    return "published stable" if stable else "published without moving stable channels"


class Backend:
    def __init__(self, repository):
        self.repository = repository

    def api(self, path, *, method="GET", data=None, cloudflare=False, raw=False):
        base = "https://api.cloudflare.com/client/v4/" if cloudflare else "https://api.github.com/"
        token = os.environ["CLOUDFLARE_API_TOKEN" if cloudflare else "GITHUB_TOKEN"]
        request = urllib.request.Request(base + path, method=method,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                             "Accept": "application/octet-stream" if raw else "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not cloudflare and method == "GET":
                return None
            raise
        result = json.loads(content)
        if cloudflare:
            if not result["success"]:
                raise RuntimeError("Cloudflare API request failed")
            return result["result"]
        return result

    def run(self, *args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

    def existing(self, tag):
        return self.api(f"repos/{self.repository}/releases/tags/{tag}")

    def latest(self):
        release = self.api(f"repos/{self.repository}/releases/latest")
        return release["tag_name"] if release else None

    def published_manifest(self, release):
        asset = next((a for a in release["assets"] if a["name"] == "release-manifest.json"), None)
        return self.api(f"repos/{self.repository}/releases/assets/{asset['id']}", raw=True) if asset else None

    def stage(self, tag, manifest, root):
        root = Path(root)
        path = root / "release-manifest.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        if self.existing(tag) is None:
            self.run("gh", "release", "create", tag, "--repo", self.repository, "--verify-tag",
                     "--draft", "--title", tag, "--generate-notes")
        paths = [str(root / name) for name in manifest["sha256"]] + [str(path)]
        self.run("gh", "release", "upload", tag, "--repo", self.repository, "--clobber", *paths)
        staged = self.existing(tag)
        if not staged["draft"]:
            raise ValueError("Release became public during staging")
        assets = {a["name"]: a for a in staged["assets"]}
        for file in paths:
            path = Path(file)
            asset = assets.get(path.name)
            if not asset or asset["size"] != path.stat().st_size:
                raise ValueError(f"Incomplete uploaded artifact: {path.name}")
            digest = asset.get("digest")
            if digest and digest != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError(f"Uploaded artifact differs: {path.name}")
        if self.published_manifest(staged) != manifest:
            raise ValueError("Uploaded release manifest differs")

    def version_images(self, release, image, digests):
        self.run("docker", "buildx", "imagetools", "create", "--tag", f"{image}:{release.version}",
                 "--tag", f"{image}:{release.tag}", *digests)

    def project_path(self):
        return f"accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}/pages/projects/tuneconsole-flatpak"

    def snapshot(self, image):
        project = self.api(self.project_path(), cloudflare=True)
        deployment = project.get("canonical_deployment")
        # A stable baseline must be recoverable before changing either production channel.
        latest = self.latest()
        digest = self.run("docker", "buildx", "imagetools", "inspect", "--format",
                          "{{.Manifest.Digest}}", f"{image}:latest") if latest else None
        if latest and (not deployment or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest or "")):
            raise ValueError("Cannot snapshot the existing stable channels")
        return {"deployment": deployment["id"] if deployment else None, "digest": digest}

    def flatpak(self, root):
        self.run("npx", "--yes", "wrangler@4.151.3", "pages", "deploy", str(Path(root) / "flatpak-repo"),
                 "--project-name", "tuneconsole-flatpak", "--branch", "production")

    def latest_image(self, image, version):
        self.run("docker", "buildx", "imagetools", "create", "--tag", f"{image}:latest", f"{image}:{version}")

    def publish(self, tag, *, prerelease, latest):
        release = self.existing(tag)
        self.api(f"repos/{self.repository}/releases/{release['id']}", method="PATCH",
                 data={"draft": False, "prerelease": prerelease, "make_latest": str(latest).lower()})

    def restore(self, previous, image):
        failures = []
        if previous["deployment"]:
            try:
                self.api(self.project_path() + f"/deployments/{previous['deployment']}/rollback",
                         method="POST", cloudflare=True)
            except Exception as exc:
                failures.append(f"Flatpak rollback failed: {type(exc).__name__}")
        if previous["digest"]:
            try:
                self.run("docker", "buildx", "imagetools", "create", "--tag", f"{image}:latest",
                         image + "@" + previous["digest"])
            except Exception as exc:
                failures.append(f"Container rollback failed: {type(exc).__name__}")
        if failures:
            raise RuntimeError("; ".join(failures))


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise SystemExit(f"Promotion interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("tag")
    parser.add_argument("--repository", default="reubenfirmin/TuneConsole")
    args = parser.parse_args()
    image = "ghcr.io/" + args.repository.lower()
    print(promote(args.directory, args.tag, image, Backend(args.repository)))
