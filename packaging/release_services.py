"""Focused publication prerequisites and public endpoint verification; no application suite."""
import argparse
import base64
from contextlib import contextmanager
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


# Cloudflare rejects urllib's default user agent even for public repository files.
PUBLIC_USER_AGENT = "TuneConsole-release-verifier/1 (+https://github.com/reubenfirmin/TuneConsole)"


def report(message):
    print(message, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write(message + "\n\n")


class RegistryPreflightError(RuntimeError):
    """Only operation names and numeric statuses may reach the CI log."""

    def __init__(self, operation, status=None):
        self.operation = operation
        self.status = status
        super().__init__(f"GHCR {operation}" + (f" (HTTP {status})" if status is not None else ""))


def registry_response(request, operation, expected_status):
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        raise RegistryPreflightError(operation, exc.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise RegistryPreflightError(operation) from None
    if response.status != expected_status:
        status = response.status
        response.close()
        raise RegistryPreflightError(operation, status)
    report(f"GHCR {operation}: HTTP {response.status}")
    return response


def registry_upload_location(request, response, name):
    location = urllib.parse.urljoin(request.full_url, response.headers["Location"])
    target = urllib.parse.urlsplit(location)
    upload_prefix = f"/v2/{name}/blobs/uploads/"
    if (target.scheme != "https" or target.netloc != "ghcr.io" or target.fragment
            or not target.path.startswith(upload_prefix) or target.path == upload_prefix):
        raise RegistryPreflightError("upload location validation")
    return location


def registry_permissions(image):
    """Prove push permission by completing one fixed, untagged two-byte blob."""
    name = image.removeprefix("ghcr.io/")
    if name == image or not name or "@" in name or ":" in name:
        raise ValueError("Expected a GHCR repository without tag or digest")
    credentials = base64.b64encode(
        f"{os.environ['GITHUB_ACTOR']}:{os.environ['GITHUB_TOKEN']}".encode()).decode()
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{name}:pull,push"})
    request = urllib.request.Request("https://ghcr.io/token?" + query,
                                     headers={"Authorization": "Basic " + credentials})
    with registry_response(request, "token request", 200) as response:
        token = json.load(response)["token"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream"}
    request = urllib.request.Request(f"https://ghcr.io/v2/{name}/blobs/uploads/",
                                     method="POST", data=b"", headers=headers)
    with registry_response(request, "upload initiation", 202) as response:
        location = registry_upload_location(request, response, name)
    # Use the proven POST/PATCH/PUT probe. Transfer the fixed OCI empty JSON
    # payload before finalizing; completing an empty session without first
    # transferring content returned 404 in the hosted investigation.
    blob = b"{}"
    request = urllib.request.Request(location, method="PATCH", data=blob,
                                     headers={**headers, "Content-Range": "0-1"})
    with registry_response(request, "blob transfer", 202) as response:
        location = registry_upload_location(request, response, name)
    # Preserve the registry's updated opaque session state exactly.
    digest = urllib.parse.urlencode({"digest": "sha256:" + hashlib.sha256(blob).hexdigest()})
    location += ("&" if "?" in location else "?") + digest
    request = urllib.request.Request(location, method="PUT", data=b"", headers=headers)
    with registry_response(request, "upload completion", 201):
        pass


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
    checks = [
        ("Cloudflare Pages Write", lambda: pages_permissions(backend)),
    ]
    # Synthetic publisher and registry exercises belong to PR checks, never
    # main's candidate preparation or a release publication attempt.
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        checks[:0] = [
            ("GitHub contents write / draft assets", lambda: rehearse(backend, rehearsal_tag, revision)),
            ("GHCR package push / blob completion", lambda: registry_permissions(image)),
        ]
    for service, check in checks:
        try:
            check()
        except RegistryPreflightError as exc:
            report(f"FAILED: {service}: {exc}. Check this service's credentials and permissions.")
            failures.append(service)
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
        raise RuntimeError("Publication preflight failed: " + ", ".join(failures)) from None


class PublicVerificationError(RuntimeError):
    """Identify the anonymous operation without logging credentials or response bodies."""

    def __init__(self, url, status=None):
        target = urllib.parse.urlsplit(url)
        safe_url = urllib.parse.urlunsplit(
            (target.scheme, target.netloc.rsplit("@", 1)[-1], target.path, "", ""))
        super().__init__(f"Public download GET {safe_url}" +
                         (f" (HTTP {status})" if status is not None else " failed"))


@contextmanager
def public_response(url):
    # Deliberately anonymous: a release must work for users without CI credentials.
    request = urllib.request.Request(url, headers={"User-Agent": PUBLIC_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            yield response
    except urllib.error.HTTPError as exc:
        raise PublicVerificationError(url, exc.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise PublicVerificationError(url) from None


def public_bytes(url):
    with public_response(url) as response:
        return response.read()


def public_download(url, digest):
    checksum = hashlib.sha256()
    with public_response(url) as response:
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
                raise ValueError(f"Public container {reference} differs from built architecture digests")
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
    parser.add_argument("operation", choices=("preflight", "registry", "public"))
    parser.add_argument("tag", nargs="?")
    parser.add_argument("--directory", type=Path, default=Path("candidate"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--rehearsal-tag")
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    args = parser.parse_args()
    if args.operation != "registry" and not args.tag:
        parser.error("tag is required for preflight and public verification")
    backend = Backend(args.repository)
    if args.operation == "registry":
        registry_permissions("ghcr.io/" + backend.repository.lower())
        report("Verified: GHCR package push / blob completion")
    elif args.operation == "preflight":
        preflight(backend, args.tag, args.revision, args.rehearsal_tag,
                  stable=classify(args.tag).channel == "stable")
    else:
        verify_public(args.directory, args.tag, backend)
