"""Exercise bundle-origin creation and signed updates without touching the user's installation."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import threading

import pytest
import yaml

bundle = runpy.run_path(Path(__file__).parents[1] / "packaging/flatpak/bundle.py")


def test_workflow_candidate_command_installs_an_unsigned_bundle(tmp_path):
    """Exercise the workflow's install options with the real Flatpak CLI."""
    if not shutil.which("flatpak"):
        pytest.skip("flatpak is required for candidate installation")
    definition = yaml.load(
        (Path(__file__).parents[1] / ".github/workflows/flatpak.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    script = next(step["run"] for step in definition["jobs"]["build"]["steps"]
                  if step.get("name") == "Build candidate and check packaged startup and bridge")
    unsigned = script.rsplit("\nelse\n", 1)[1]
    install = next(shlex.split(line.strip()) for line in unsigned.splitlines()
                   if line.strip().startswith("flatpak install "))
    isolated = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
                "TMPDIR": str(tmp_path)}

    def run(args):
        result = subprocess.run([str(arg) for arg in args], env=isolated, text=True,
                                capture_output=True, timeout=60)
        assert result.returncode == 0, f"{result.args}\n{result.stdout}\n{result.stderr}"
        return result.stdout.strip()

    arch = run(["flatpak", "--default-arch"])
    build = tmp_path / "build"
    (build / "files/bin").mkdir(parents=True)
    (build / "export").mkdir()
    (build / "metadata").write_text(
        f"[Application]\nname={bundle['APP_ID']}\n"
        f"runtime=org.freedesktop.Platform/{arch}/25.08\ncommand=version\n")
    (build / "files/bin/version").write_text("candidate version\n")
    repo = tmp_path / "repo"
    run(["flatpak", "build-export", "--disable-sandbox", repo, build])
    archive = tmp_path / f"TuneConsole-{arch}.flatpak"
    run(["flatpak", "build-bundle", repo, archive, bundle["APP_ID"]])
    command = [str(archive) if arg == "TuneConsole-$ARCH.flatpak" else arg for arg in install]
    # The tiny fixture needs no runtime downloads and uses only the private installation.
    run([*command[:2], "--no-deps", "--no-related", *command[2:]])
    location = run(["flatpak", "info", "--user", "--show-location", bundle["APP_ID"]])
    assert (Path(location) / "files/bin/version").read_text() == "candidate version\n"


@pytest.mark.parametrize("initial_runtime", ["25.08", "24.08"])
def test_signed_bundle_configures_origin_and_updates_from_it(tmp_path, initial_runtime):
    for tool in ("flatpak", "gpg"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is required for the bundle integration check")

    def run(*args, env=None):
        result = subprocess.run([str(a) for a in args], text=True,
                                capture_output=True, env=env)
        assert result.returncode == 0, (
            f"Command failed: {result.args}\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    sdk = "org.freedesktop.Sdk//25.08"
    if subprocess.run(["flatpak", "info", sdk], capture_output=True).returncode:
        pytest.skip(f"{sdk} is required for the bundle integration check")
    if subprocess.run(["flatpak", "info", f"org.freedesktop.Sdk//{initial_runtime}"], capture_output=True).returncode:
        pytest.skip(f"SDK {initial_runtime} is required for the upgrade check")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    gpg = ["gpg", "--homedir", str(keys), "--batch", "--pinentry-mode", "loopback",
           "--passphrase", ""]
    run(*gpg, "--quick-generate-key", "Bundle Test <bundle@example.invalid>", "rsa2048", "sign", "1d")
    fingerprint = next(line.split(":")[9] for line in run(*gpg, "--with-colons", "--list-keys").splitlines()
                       if line.startswith("fpr:"))
    key = tmp_path / "public.gpg"
    key.write_bytes(subprocess.run([*gpg, "--export", fingerprint], check=True, capture_output=True).stdout)
    repo = tmp_path / "repo"

    def export(version, runtime="25.08"):
        build = tmp_path / f"build-{version}"
        run("flatpak", "build-init", build, bundle["APP_ID"], "org.freedesktop.Sdk",
            "org.freedesktop.Platform", runtime)
        (build / "files/bin").mkdir(parents=True)
        executable = build / "files/bin/version"
        executable.write_text(f"#!/bin/sh\necho {version}\n")
        executable.chmod(0o755)
        run("flatpak", "build-finish", "--command=version", build)
        run("flatpak", "build-export", f"--gpg-homedir={keys}", f"--gpg-sign={fingerprint}", repo, build)
        run("flatpak", "build-update-repo", f"--gpg-homedir={keys}", f"--gpg-sign={fingerprint}", repo)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(repo)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Flatpak's bundle unpacking also needs a private, writable temporary directory.
    isolated = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
                "TMPDIR": str(tmp_path)}
    try:
        export("one", initial_runtime)
        archive = tmp_path / "TuneConsole.flatpak"
        origin_url = f"http://127.0.0.1:{server.server_port}/"
        bundle["build"](repo, archive, key, repo_url=origin_url)
        run("flatpak", "install", "--user", "--noninteractive", "--no-deps", "--no-related", archive,
            env=isolated)
        origin = run("flatpak", "info", "--user", "--show-origin", bundle["APP_ID"], env=isolated)
        assert origin
        assert origin_url in run("flatpak", "remotes", "--user", "--columns=name,url", env=isolated)
        config = (tmp_path / "data/flatpak/repo/config").read_text()
        assert "gpg-verify=true" in config
        before = run("flatpak", "info", "--user", "--show-commit", bundle["APP_ID"], env=isolated)
        data = tmp_path / "data/flatpak/app/com.tuneconsole.TuneConsole/preserved-library.txt"
        data.write_text("existing library")
        export("two")
        run("flatpak", "update", "--user", "--noninteractive", "--no-deps", "--no-related",
            bundle["APP_ID"], env=isolated)
        assert run("flatpak", "info", "--user", "--show-commit", bundle["APP_ID"], env=isolated) != before
        location = run("flatpak", "info", "--user", "--show-location", bundle["APP_ID"], env=isolated)
        assert "echo two" in (Path(location) / "files/bin/version").read_text()
        assert "25.08" in run("flatpak", "info", "--user", "--show-runtime", bundle["APP_ID"], env=isolated)
        assert data.read_text() == "existing library"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
