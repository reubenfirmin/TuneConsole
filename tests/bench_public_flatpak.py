"""Read-only acceptance check of an already published, retained Flatpak candidate.

TUNECONSOLE_PUBLIC_RELEASE_MANIFEST=/path/to/release-manifest.json \
  uv run pytest -s tests/bench_public_flatpak.py
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
from release_services import public_bytes, verify_flatpak


def test_public_signed_flatpak_matches_retained_candidate(tmp_path):
    source = os.environ.get("TUNECONSOLE_PUBLIC_RELEASE_MANIFEST")
    if not source:
        pytest.skip("Set TUNECONSOLE_PUBLIC_RELEASE_MANIFEST to a trusted retained manifest")
    if not shutil.which("flatpak"):
        pytest.skip("flatpak is required for signed public repository verification")
    manifest = json.loads(Path(source).read_text())
    checksums = manifest["flatpak_repository_sha256"]
    descriptor = public_bytes("https://repo.tuneconsole.com/tuneconsole.flatpakrepo")
    # Bind the remote URL and embedded GPG trust to the retained candidate too.
    assert hashlib.sha256(descriptor).hexdigest() == checksums["tuneconsole.flatpakrepo"]
    repository = tmp_path / "flatpak-repo"
    repository.mkdir()
    (repository / "tuneconsole.flatpakrepo").write_bytes(descriptor)
    environment = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"),
                   "XDG_CACHE_HOME": str(tmp_path / "cache"),
                   "XDG_CONFIG_HOME": str(tmp_path / "config")}
    verify_flatpak(tmp_path, manifest["version"], checksums, environment)
