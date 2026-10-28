"""Exercise authenticated draft uploads without publishing or changing update channels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from promote_release import Backend


def require_created_draft(release, tag, release_id):
    if (not isinstance(release, dict) or release.get("id") != release_id
            or release.get("tag_name") != tag or release.get("draft") is not True):
        raise RuntimeError("Created rehearsal release identity or draft state differs")


def discover_created_draft(backend, tag, created):
    if not isinstance(created, dict) or type(created.get("id")) is not int:
        raise RuntimeError("Draft creation did not return a release ID")
    release_id = created["id"]
    require_created_draft(created, tag, release_id)
    deadline = time.monotonic() + 10
    attempt = 0
    while True:
        attempt += 1
        release = backend.existing(tag)
        print("Publisher draft discovery: " + json.dumps({"attempt": attempt,
            "created_id": release_id, "found_id": release.get("id") if release else None,
            "response": backend.last_response}, sort_keys=True), flush=True)
        if release is not None:
            require_created_draft(release, tag, release_id)
            return release
        # Recover only a demonstrated discovery lag: the exact POST-created
        # resource must still be an authenticated, isolated draft by ID.
        direct = backend.api(f"repos/{backend.repository}/releases/{release_id}")
        require_created_draft(direct, tag, release_id)
        print("Publisher draft visible by ID; authenticated tag/list discovery is pending: "
              + json.dumps(backend.last_response, sort_keys=True), flush=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Authenticated draft lookup failed after 10 seconds")
        time.sleep(min(1, remaining))


def cleanup(backend, tag, *, created=None, wait_seconds=0):
    if not re.fullmatch(r"publisher-contract-[a-zA-Z0-9_-]+", tag):
        raise ValueError("Cleanup requires an isolated publisher-contract draft")
    # A known creation ID can be checked directly; a lost POST response still
    # needs main's bounded listing wait to find and remove its isolated draft.
    release = backend.existing(tag, wait_seconds=wait_seconds if created is None else 0)
    if created is not None:
        release_id = created.get("id")
        require_created_draft(created, tag, release_id)
        if release is None:
            release = backend.api(f"repos/{backend.repository}/releases/{release_id}")
        if release is not None:
            require_created_draft(release, tag, release_id)
    if release is not None:
        if release.get("draft") is not True or release.get("tag_name") != tag:
            raise ValueError("Cleanup requires an isolated publisher-contract draft")
        backend.api(f"repos/{backend.repository}/releases/{release['id']}", method="DELETE")


def rehearse(backend, tag, revision):
    if not re.fullmatch(r"publisher-contract-[a-zA-Z0-9_-]+", tag):
        raise ValueError("Rehearsal tag must be isolated from release tags")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Rehearsal requires an exact commit")
    if backend.existing(tag) is not None:
        raise ValueError("Rehearsal draft already exists; clean it up explicitly before retrying")
    created = None
    try:
        created = backend.api(f"repos/{backend.repository}/releases", method="POST", data={
            "tag_name": tag, "target_commitish": revision, "name": tag,
            "draft": True, "prerelease": True, "make_latest": "false",
            "body": "Isolated publisher contract rehearsal; never publish this draft.",
        })
        discover_created_draft(backend, tag, created)
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
        cleanup(backend, tag, created=created, wait_seconds=10)


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
