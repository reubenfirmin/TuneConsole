"""Validate the shipped offline resolution, including foreign-architecture wheel compatibility."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tomllib

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).parents[1]
generator = runpy.run_path(ROOT / "packaging/generate_deps.py")


def test_checked_in_flatpak_inputs_match_current_lock_and_markers(tmp_path):
    subprocess.run([sys.executable, "packaging/generate_deps.py", "--check"], cwd=ROOT,
                   env={**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv")}, check=True)
    manifest = json.loads((ROOT / "packaging/flatpak/python3-requirements.json").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    hashes = {w["url"]: w["hash"].removeprefix("sha256:")
              for p in lock["package"] for w in p.get("wheels", [])}
    for module in manifest["modules"]:
        command = module["build-commands"][0]
        for required in ("requests==", "websockets==", "watchfiles==", "dbus-next==", "scipy=="):
            assert required in command
        assert "tomli==" not in command and "colorama==" not in command
        assert "--no-index --no-deps --no-build-isolation" in command
        assert all(hashes[s["url"]] == s["sha256"] for s in module["sources"])
        assert not any("cp312" in s["dest-filename"] for s in module["sources"])


def test_export_markers_are_evaluated_for_target_not_build_host():
    pins = "numpy==1; python_version < '3.13'\nnumpy==2; python_version >= '3.13'\n" \
           "dbus-next==1; sys_platform == 'linux'\ncolorama==1; sys_platform == 'win32'\n" \
           "arm-helper==1; platform_machine == 'aarch64'"
    selected = generator["requirements"]("3.13", "aarch64", pins)
    assert selected == {"numpy": "2", "dbus-next": "1", "arm-helper": "1"}
    assert "arm-helper" not in generator["requirements"]("3.13", "x86_64", pins)


def test_missing_compatible_locked_wheel_fails_instead_of_resolving_again():
    with pytest.raises(ValueError, match="No locked CPython 3.13 aarch64 wheel"):
        generator["wheel_for"]({"name": "binary", "version": "1", "wheels": []}, "3.13", "aarch64")


def test_build_tools_are_exactly_pinned_in_the_universal_lock():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {(canonicalize_name(p["name"]), p["version"]) for p in lock["package"] if "version" in p}
    for group in ("build", "macos"):
        for raw in project["dependency-groups"][group]:
            req = Requirement(raw)
            pin, = req.specifier
            assert pin.operator == "=="
            assert (canonicalize_name(req.name), pin.version) in versions
