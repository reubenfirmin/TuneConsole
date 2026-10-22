"""Opt-in native container build and smoke check using the shared test lock."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = Path(__file__).parents[1]
ENGINE = os.environ.get("TUNECONSOLE_TEST_CONTAINER_ENGINE")


@pytest.mark.skipif(ENGINE not in ("docker", "podman"),
                    reason="Set TUNECONSOLE_TEST_CONTAINER_ENGINE=docker or podman")
def test_locked_container_build_and_packaged_behavior():
    if not shutil.which(ENGINE):
        pytest.skip(f"{ENGINE} is required")
    image = "tuneconsole-p3-check:" + uuid.uuid4().hex[:12]
    build = [ENGINE, "build", "--tag", image, "--build-arg", "VERSION=1.2.3"]
    if ENGINE == "podman":
        build += ["--format", "docker"]
    try:
        subprocess.run([*build, "."], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "packaging/container/smoke.py", "--engine", ENGINE,
                        "--image", image, "--version", "1.2.3"], cwd=ROOT, check=True)
    finally:
        subprocess.run([ENGINE, "image", "rm", image], capture_output=True)
