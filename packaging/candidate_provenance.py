"""Bind final-version candidate hashes to a release tag and commit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tarfile

from promote_release import classify, validate


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def create(root, tag, revision, run_id, repository):
    root = Path(root)
    release, _, manifest = validate(root, tag, "ghcr.io/" + repository.lower(), require_repository=False)
    files = dict(manifest["sha256"])
    for arch in ("x86_64", "aarch64"):
        name = f"flatpak-repo-{arch}.tar"
        with tarfile.open(root / name) as archive:
            ref = archive.extractfile(f"repo/refs/heads/app/com.tuneconsole.TuneConsole/{arch}/master")
            signature = archive.extractfile("repo/summary.sig")
            if not ref or len(ref.read().strip()) != 64 or not signature or not signature.read():
                raise ValueError(f"Missing signed Flatpak candidate for {arch}")
        files[name] = digest(root / name)
    files["flatpak-repository.tar"] = digest(root / "flatpak-repository.tar")
    provenance = {"schema": 1, "tag": tag, "version": release.version, "revision": revision,
                  "source_run": str(run_id), "repository": repository, "sha256": files,
                  "architectures": {"macos": ["arm64", "x86_64"], "flatpak": ["aarch64", "x86_64"],
                                    "container": ["amd64", "arm64"]}}
    (root / "release-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return provenance


def check_identity(provenance, tag, revision, run_id, repository):
    if (provenance.get("schema"), provenance.get("tag"), provenance.get("version"),
            provenance.get("revision"), provenance.get("source_run"), provenance.get("repository")) != (
            1, tag, classify(tag).version, revision, str(run_id), repository):
        raise ValueError("Candidate provenance must match the exact version, tag, commit, source run and repository")
    if provenance.get("architectures") != {"macos": ["arm64", "x86_64"], "flatpak": ["aarch64", "x86_64"],
                                          "container": ["amd64", "arm64"]}:
        raise ValueError("Candidate provenance is missing a supported architecture")


def verify(root, tag, revision, run_id, repository):
    root = Path(root)
    provenance = json.loads((root / "release-provenance.json").read_text())
    check_identity(provenance, tag, revision, run_id, repository)
    if not provenance.get("sha256"):
        raise ValueError("Candidate provenance contains no payload hashes")
    for name, expected in provenance["sha256"].items():
        if Path(name).name != name or digest(root / name) != expected:
            raise ValueError(f"Candidate differs from the release build's recorded bytes: {name}")
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("tag")
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--source-run", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args()
    operation = create if args.operation == "create" else verify
    operation(args.directory, args.tag, args.revision, args.source_run, args.repository)
