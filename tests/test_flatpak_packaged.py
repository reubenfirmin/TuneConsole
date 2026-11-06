"""Opt-in real Flatpak build and signed two-version handoff, under pytest's shared lock."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib

import pytest

ROOT = Path(__file__).parents[1]
APP = "com.tuneconsole.TuneConsole"


@pytest.fixture(scope="module")
def packaged_flatpak(tmp_path_factory, request):
    if not request.config.getoption("--run-flatpak-build"):
        pytest.skip("Use --run-flatpak-build to build the real Flatpak")
    tmp_path = tmp_path_factory.mktemp("packaged-flatpak")
    for command in ("flatpak", "flatpak-builder", "uv"):
        if not shutil.which(command):
            pytest.skip(f"{command} is required")
    env = {**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
           "UV_PROJECT_ENVIRONMENT": str(tmp_path / "build-venv"),
           "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")}
    (tmp_path / "runtime").mkdir(mode=0o700)

    def run(*args, **kwargs):
        kwargs.setdefault("timeout", 300)
        result = subprocess.run([str(arg) for arg in args], env=env, cwd=ROOT,
                                capture_output=True, text=True, **kwargs)
        assert result.returncode == 0, f"{args}\n{result.stdout[-8000:]}\n{result.stderr[-8000:]}"
        return result

    run("uv", "sync", "--frozen", "--only-group", "build", "--no-install-project",
        "--python", sys.executable)
    dist = tmp_path / "dist"
    builds = {}

    def build_version(version, runtime="25.08"):
        key = version, runtime
        if key not in builds:
            packaging = tmp_path / f"packaging/flatpak-{runtime}"
            packaging.mkdir(parents=True, exist_ok=True)
            for source in (ROOT / "packaging/flatpak").iterdir():
                if source.is_file():
                    shutil.copy2(source, packaging / source.name)
            if runtime == "24.08":
                # Reconstruct the previous runtime/ABI using the same locked wheel selection.
                # Keep this fixture separate from the maintained release manifest.
                manifest = packaging / "com.tuneconsole.TuneConsole.yaml"
                manifest.write_text(manifest.read_text().replace("runtime-version: '25.08'",
                                                                "runtime-version: '24.08'"))
                generator = runpy.run_path(ROOT / "packaging/generate_deps.py")
                lock = tomllib.loads((ROOT / "uv.lock").read_text())
                config = {**tomllib.loads((ROOT / "packaging/platforms.toml").read_text())["flatpak"],
                          "python": "3.12"}
                exported = run("uv", "export", "--frozen", "--no-dev", "--no-emit-project",
                               "--no-hashes", "--no-header", "--no-annotate").stdout
                (packaging / "python3-requirements.json").write_text(
                    generator["render"](lock, config, exported))
            env["SETUPTOOLS_SCM_PRETEND_VERSION"] = version
            for previous in dist.glob("yt_playlist-*.whl"):
                previous.unlink()
            run("uv", "build", "--wheel", "--no-build-isolation", "--python",
                Path(env["UV_PROJECT_ENVIRONMENT"]) / "bin/python", "--out-dir", dist)
            build = tmp_path / f"build-{version}-{runtime}"
            run("flatpak-builder", "--user", "--disable-rofiles-fuse", "--jobs=1",
                f"--state-dir={tmp_path / 'builder-state'}", "--force-clean", build,
                packaging / "com.tuneconsole.TuneConsole.yaml")
            builds[key] = build
        return builds[key]

    return tmp_path, env, run, build_version


def test_locked_wheel_builds_and_runs_on_supported_flatpak_runtime(packaged_flatpak):
    tmp_path, env, run, build_version = packaged_flatpak
    build = build_version("1.2.3")
    run(sys.executable, ROOT / "packaging/flatpak/smoke.py", "1.2.3", "--build-dir", build,
        "--data-dir", tmp_path / "app-data", timeout=180)
