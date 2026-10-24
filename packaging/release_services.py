"""Focused publication prerequisites and public endpoint verification; no application suite."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from promote_release import Backend, classify, validate
from rehearse_publisher import rehearse
from flatpak.verify_metadata import verify_cache


def report(message):
    print(message, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write(message + "\n\n")


def registry_open(request, operation):
    try:
        return urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        # Upload locations can contain credentials; expose only the named operation and status.
        report(f"FAILED: GHCR {operation} (HTTP {exc.code}).")
        raise


def registry_permissions(image):
    """Prove push permission by completing an empty, untagged blob upload."""
    name = image.removeprefix("ghcr.io/")
    if name == image or not name or "@" in name or ":" in name:
        raise ValueError("Expected a GHCR repository without tag or digest")
    credentials = base64.b64encode(
        f"{os.environ['GITHUB_ACTOR']}:{os.environ['GITHUB_TOKEN']}".encode()).decode()
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{name}:pull,push"})
    request = urllib.request.Request("https://ghcr.io/token?" + query,
                                     headers={"Authorization": "Basic " + credentials})
    with registry_open(request, "token request") as response:
        token = json.load(response)["token"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream"}
    request = urllib.request.Request(f"https://ghcr.io/v2/{name}/blobs/uploads/",
                                     method="POST", data=b"", headers=headers)
    with registry_open(request, "upload initiation") as response:
        if response.status != 202:
            raise ValueError("GHCR did not accept a disposable upload")
        location = urllib.parse.urljoin(request.full_url, response.headers["Location"])
    if urllib.parse.urlparse(location)[:2] != ("https", "ghcr.io"):
        raise ValueError("GHCR returned an unexpected upload origin")
    # GHCR does not implement upload cancellation (DELETE returns 405). Complete
    # the zero-byte blob instead: one fixed digest, no manifest or image tag.
    digest = urllib.parse.urlencode({"digest": "sha256:" + hashlib.sha256(b"").hexdigest()})
    location += ("&" if "?" in location else "?") + digest
    request = urllib.request.Request(location, method="PUT", data=b"", headers=headers)
    with registry_open(request, "upload completion") as response:
        if response.status != 201:
            raise ValueError("GHCR did not complete the empty blob upload")


def pages_permissions(backend):
    result = backend.api(backend.project_path() + "/upload-token", cloudflare=True)
    if not isinstance(result, dict) or not result.get("jwt"):
        raise ValueError("Cloudflare did not grant a Pages upload token")


def preflight(backend, tag, revision, rehearsal_tag, *, stable):
    for tool in ("gh", "docker", "npx", "gpg"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Publication prerequisite missing: {tool}")
    backend.run("docker", "buildx", "version")
    image = "ghcr.io/" + backend.repository.lower()
    failures = []
    for service, check in (
        ("GitHub contents write / draft assets", lambda: rehearse(backend, rehearsal_tag, revision)),
        ("GHCR package push / blob completion", lambda: registry_permissions(image)),
        ("Cloudflare Pages Write", lambda: pages_permissions(backend)),
    ):
        try:
            check()
        except Exception as exc:
            # Subprocess and HTTP exceptions can contain auth-bearing response
            # URLs or headers. Report the service and type without dumping them.
            status = f", HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
            report(f"FAILED: {service} ({type(exc).__name__}{status}). Check this service's configuration and permissions.")
            failures.append(service)
        else:
            report(f"Verified: {service}")
    if stable:
        try:
            backend.require_reconciled_channels(tag)
            backend.snapshot(image)
        except Exception as exc:
            status = f", HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
            report(f"FAILED: recoverable stable-channel baseline ({type(exc).__name__}{status}).")
            failures.append("recoverable stable-channel baseline")
        else:
            report("Verified: recoverable stable-channel baseline (rechecked under the production lock before promotion)")
    if failures:
        raise RuntimeError("Publication preflight failed: " + ", ".join(failures))


def public_bytes(url):
    # Deliberately anonymous: a release must work for users without CI credentials.
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def public_download(url, digest):
    checksum = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=30) as response:
        while block := response.read(1024 * 1024):
            checksum.update(block)
    if checksum.hexdigest() != digest:
        raise ValueError(f"Public download checksum differs: {url}")


def image_descriptors(image, reference, *, docker_config):
    completed = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", f"{image}:{reference}"],
        check=True, capture_output=True, text=True, timeout=60,
        env={**os.environ, "DOCKER_CONFIG": str(docker_config)},
    )
    manifests = json.loads(completed.stdout)["manifests"]
    return {(m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture")): m["digest"]
            for m in manifests if m.get("platform", {}).get("architecture") in {"amd64", "arm64"}}


def verify_flatpak(root, version, checksums, environment, *, repo_url="https://repo.tuneconsole.com/"):
    for name in ("summary", "summary.sig"):
        if hashlib.sha256(public_bytes(repo_url + name)).hexdigest() != checksums[name]:
            raise ValueError(f"Public Flatpak {name} differs from signed candidate")
    descriptor = Path(root) / "flatpak-repo/tuneconsole.flatpakrepo"
    subprocess.run(["flatpak", "remote-add", "--user", "--from", "release-verification", str(descriptor)],
                   check=True, capture_output=True, env=environment, timeout=60)
    for arch in ("x86_64", "aarch64"):
        # remote-ls reads the local AppStream cache even for a freshly added remote.
        try:
            subprocess.run(["flatpak", "update", "--user", "--appstream", f"--arch={arch}",
                            "release-verification"], check=True, capture_output=True, text=True,
                           env=environment, timeout=60)
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"Cannot refresh signed Flatpak metadata for {arch}: "
                             f"{exc.stderr[:1024]}") from None
        result = subprocess.run(
            ["flatpak", "remote-ls", "--user", "--app", f"--arch={arch}",
             "--columns=application", "release-verification"],
            check=True, capture_output=True, text=True, env=environment, timeout=60,
        )
        if "com.tuneconsole.TuneConsole" not in result.stdout.split():
            raise ValueError(f"Public signed Flatpak repository is missing {arch}; "
                             f"observed {result.stdout[:512]!r}")
        # remote-ls looks up display versions using the host architecture's catalog.
        # Check each downloaded, signature-verified catalog and its bundle directly.
        verify_cache(environment["XDG_DATA_HOME"], "release-verification", arch, version)
    report(f"Verified signed Flatpak summary and version {version} for x86_64/aarch64: [repository]({repo_url})")


def verify_public(root, tag, backend):
    release, digests, manifest = validate(root, tag, "ghcr.io/" + backend.repository.lower())
    root = Path(root)
    base = f"https://github.com/{backend.repository}/releases/download/{tag}/"
    for name, checksum in manifest["sha256"].items():
        url = base + name
        public_download(url, checksum)
        report(f"Verified public download: [{name}]({url})")
    if json.loads(public_bytes(base + "release-manifest.json")) != manifest:
        raise ValueError("Anonymous release manifest differs")
    report(f"Verified public release {tag}: version {release.version}, both macOS and Flatpak architectures, signed appcasts")

    image = "ghcr.io/" + backend.repository.lower()
    expected = {("linux", arch): digest.split("@", 1)[1]
                for arch, digest in zip(("amd64", "arm64"), digests)}
    stable = release.channel == "stable" and backend.latest() == tag
    with tempfile.TemporaryDirectory(prefix="release-public-") as directory:
        temporary = Path(directory)
        # No registry login is inherited: these checks prove anonymous downloads.
        for reference in (release.version, tag, "latest") if stable else (release.version, tag):
            if image_descriptors(image, reference, docker_config=temporary) != expected:
                raise ValueError(f"Public container {reference} differs from tested architecture digests")
            report(f"Verified public container: `{image}:{reference}` (linux/amd64, linux/arm64)")
        if stable:
            # Separate user state makes signature verification independent of any
            # preconfigured remote, trust override, cache or prior installation.
            environment = {**os.environ, "XDG_DATA_HOME": str(temporary / "data"),
                           "XDG_CACHE_HOME": str(temporary / "cache"),
                           "XDG_CONFIG_HOME": str(temporary / "config")}
            verify_flatpak(root, release.version, manifest["flatpak_repository_sha256"], environment)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "public"))
    parser.add_argument("tag")
    parser.add_argument("--directory", type=Path, default=Path("candidate"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--rehearsal-tag")
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    args = parser.parse_args()
    backend = Backend(args.repository)
    if args.operation == "preflight":
        preflight(backend, args.tag, args.revision, args.rehearsal_tag,
                  stable=classify(args.tag).channel == "stable")
    else:
        verify_public(args.directory, args.tag, backend)
