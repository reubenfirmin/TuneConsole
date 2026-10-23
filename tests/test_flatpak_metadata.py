"""The exported AppStream version follows the wheel, including prospective releases."""
import runpy
import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest
import yaml

ROOT = Path(__file__).parents[1]
stamp = runpy.run_path(ROOT / "packaging/flatpak/stamp_metainfo.py")["stamp"]


@pytest.mark.parametrize("version", ["1.2.3", "1.2.3rc1", "0.0.0.dev0"])
def test_real_metainfo_exports_the_exact_packaged_wheel_version(tmp_path, version):
    metainfo = tmp_path / "com.tuneconsole.TuneConsole.metainfo.xml"
    shutil.copy2(ROOT / "packaging/flatpak" / metainfo.name, metainfo)
    with zipfile.ZipFile(tmp_path / f"yt_playlist-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"yt_playlist-{version}.dist-info/METADATA", f"Name: yt-playlist\nVersion: {version}\n")
    stamp(metainfo, tmp_path, date="2026-10-23")
    stamp(metainfo, tmp_path, date="2026-10-23")
    releases = ET.parse(metainfo).findall("releases/release")
    assert releases[0].attrib == {"version": version, "date": "2026-10-23"}
    assert sum(release.get("version") == version for release in releases) == 1
    assert any(release.get("version") == "0.1.8" for release in releases)
    manifest = yaml.safe_load((ROOT / "packaging/flatpak/com.tuneconsole.TuneConsole.yaml").read_text())
    module = manifest["modules"][-1]
    stamp_index = module["build-commands"].index(
        "python3 stamp_metainfo.py com.tuneconsole.TuneConsole.metainfo.xml wheel")
    install_index = next(index for index, command in enumerate(module["build-commands"])
                         if command.startswith("install ") and "share/metainfo/" in command)
    assert stamp_index < install_index
    assert {"type": "file", "path": "stamp_metainfo.py"} in module["sources"]


def test_missing_wheel_cannot_export_a_stale_appstream_version(tmp_path):
    with pytest.raises(ValueError, match="exactly one packaged wheel"):
        stamp(tmp_path / "metadata.xml", tmp_path)


def test_real_signed_repository_reports_stamped_version_for_both_architectures(tmp_path):
    for command in ("flatpak", "gpg", "appstreamcli"):
        if not shutil.which(command):
            pytest.skip(f"{command} is required for the AppStream export check")
    sys.path.insert(0, str(ROOT / "packaging"))
    from release_services import verify_flatpak

    environment = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"),
                   "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)

    def run(*args):
        result = subprocess.run([str(arg) for arg in args], env=environment,
                                capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode()
        return result.stdout

    gpg = ["gpg", "--homedir", keys, "--batch", "--pinentry-mode", "loopback", "--passphrase", ""]
    repo = tmp_path / "flatpak-repo"
    try:
        run(*gpg, "--quick-generate-key", "Metadata Test <metadata@example.invalid>", "rsa2048", "sign", "1d")
        fingerprint = next(line.split(":")[9] for line in run(*gpg, "--with-colons", "--list-keys").decode().splitlines()
                           if line.startswith("fpr:"))
        for arch in ("x86_64", "aarch64"):
            build = tmp_path / arch
            files = build / "files"
            metainfo = files / "share/metainfo/com.tuneconsole.TuneConsole.metainfo.xml"
            metainfo.parent.mkdir(parents=True)
            shutil.copy2(ROOT / "packaging/flatpak" / metainfo.name, metainfo)
            for source, destination in (
                    ("com.tuneconsole.TuneConsole.desktop", "share/applications/com.tuneconsole.TuneConsole.desktop"),
                    ("com.tuneconsole.TuneConsole.png", "share/icons/hicolor/256x256/apps/com.tuneconsole.TuneConsole.png")):
                target = files / destination
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / "packaging/flatpak" / source, target)
            with zipfile.ZipFile(build / "yt_playlist-1.2.3-py3-none-any.whl", "w") as wheel:
                wheel.writestr("yt_playlist-1.2.3.dist-info/METADATA", "Name: yt-playlist\nVersion: 1.2.3\n")
            stamp(metainfo, build)
            run("appstreamcli", "compose", "--no-net", "--prefix=/",
                "--origin=com.tuneconsole.TuneConsole", f"--result-root={files}",
                f"--data-dir={files}/share/app-info/xmls",
                f"--icons-dir={files}/share/app-info/icons/flatpak",
                "--components=com.tuneconsole.TuneConsole", files)
            (build / "metadata").write_text(
                f"[Application]\nname=com.tuneconsole.TuneConsole\nruntime=org.freedesktop.Platform/{arch}/25.08\n")
            run("flatpak", "build-finish", build)
            run("flatpak", "build-export", f"--arch={arch}", f"--gpg-homedir={keys}",
                f"--gpg-sign={fingerprint}", repo, build)
        run("flatpak", "build-update-repo", "--no-summary-index",
            f"--gpg-homedir={keys}", f"--gpg-sign={fingerprint}", repo)
        public_key = base64.b64encode(run(*gpg, "--export", fingerprint)).decode()
        repo_url = repo.as_uri() + "/"
        (repo / "tuneconsole.flatpakrepo").write_text(
            f"[Flatpak Repo]\nTitle=Metadata Test\nUrl={repo_url}\nGPGKey={public_key}\n")
        checksums = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in ("summary", "summary.sig")}
        verify_flatpak(tmp_path, "1.2.3", checksums, environment, repo_url=repo_url)
    finally:
        subprocess.run(["gpgconf", "--homedir", str(keys), "--kill", "gpg-agent"],
                       env=environment, capture_output=True, timeout=10)
