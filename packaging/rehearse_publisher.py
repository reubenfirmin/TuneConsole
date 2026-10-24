"""Exercise authenticated draft uploads without publishing or changing update channels."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile

from promote_release import Backend


def cleanup(backend, tag, *, wait_seconds=0):
    release = backend.existing(tag, wait_seconds=wait_seconds)
    if release is not None:
        if not release["draft"] or not tag.startswith("publisher-contract-"):
            raise ValueError("Cleanup requires an isolated publisher-contract draft")
        backend.api(f"repos/{backend.repository}/releases/{release['id']}", method="DELETE")


def rehearse(backend, tag, revision):
    if not re.fullmatch(r"publisher-contract-[a-zA-Z0-9_-]+", tag):
        raise ValueError("Rehearsal tag must be isolated from release tags")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Rehearsal requires an exact commit")
    if backend.existing(tag) is not None:
        raise ValueError("Rehearsal draft already exists; clean it up explicitly before retrying")
    try:
        backend.api(f"repos/{backend.repository}/releases", method="POST", data={
            "tag_name": tag, "target_commitish": revision, "name": tag,
            "draft": True, "prerelease": True, "make_latest": "false",
            "body": "Isolated publisher contract rehearsal; never publish this draft.",
        })
        release = backend.existing(tag, wait_seconds=10)
        if not release or not release["draft"]:
            raise RuntimeError("Authenticated draft lookup failed")
        with tempfile.TemporaryDirectory(prefix="publisher-contract-") as directory:
            root = Path(directory)
            files = {"contract-first.txt": b"draft upload contract\n",
                     "contract-second.txt": revision.encode() + b"\n"}
            for name, content in files.items():
                (root / name).write_bytes(content)
            manifest = {"tag": tag, "revision": revision, "sha256": {
                name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
            # Start with a partial upload, then exercise the same resumable stage
            # operation as a real release. Repeating it verifies draft idempotency.
            backend.run("gh", "release", "upload", tag, "--repo", backend.repository,
                        str(root / "contract-first.txt"))
            backend.stage(tag, manifest, root)
            backend.stage(tag, manifest, root)
            if backend.published_manifest(backend.existing(tag)) != manifest:
                raise ValueError("Rehearsal upload verification differs")
    finally:
        # Also runs after a lost draft-create response. The workflow repeats this
        # cleanup in an always() step to cover process termination.
        cleanup(backend, tag, wait_seconds=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()
    backend = Backend(args.repository)
    if args.cleanup_only:
        cleanup(backend, args.tag)
    else:
        rehearse(backend, args.tag, args.revision)
    print("Publisher contract draft cleaned up" if args.cleanup_only else "Publisher contract rehearsal passed")
