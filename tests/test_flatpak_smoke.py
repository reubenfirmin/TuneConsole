"""Exercise the same identity probe used by the packaged Flatpak acceptance check."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
smoke = runpy.run_path(ROOT / "packaging/flatpak/smoke.py")


@pytest.mark.parametrize("version", ["0.0.0.dev0", "1.2.3"])
@pytest.mark.parametrize("app_id", [smoke["APP"], "com.example.Other", ""])
def test_identity_probe_accepts_ci_and_release_versions_only_in_our_flatpak(tmp_path, version, app_id):
    # Supply real distribution metadata without rebuilding the application for each version.
    metadata = tmp_path / f"yt_playlist-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Name: yt-playlist\nVersion: {version}\n")
    environment = {**os.environ, "FLATPAK_ID": app_id,
                   "PYTHONPATH": os.pathsep.join((str(tmp_path), str(ROOT / "src")))}
    result = subprocess.run([sys.executable, "-c", smoke["IDENTITY_PROBE"]],
                            env=environment, capture_output=True, text=True, timeout=30)
    if app_id == smoke["APP"]:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "version": version, "origin": "chrome-extension://opmmbolbdgdkphfocffpjpgmelkkjkcn"}
    else:
        assert result.returncode != 0
        assert "AssertionError" in result.stderr


def test_probe_failure_includes_captured_output(monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, output="probe output", stderr="underlying failure")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="probe output\nunderlying failure"):
        smoke["check"]("0.0.0.dev0", lambda command: [command])
