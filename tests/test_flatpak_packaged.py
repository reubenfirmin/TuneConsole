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


@pytest.mark.browser
@pytest.mark.parametrize("initial_runtime", ["25.08", "24.08"])
def test_signed_packaged_update_restarts_same_tab_and_preserves_library(packaged_flatpak, page, initial_runtime):
    """Use the real portal and Home buttons; only desktop consent is pregranted, in a private bus."""
    from playwright.sync_api import expect
    from websockets.sync.client import connect

    tmp_path, build_env, _, build_version = packaged_flatpak
    for executable in ("gpg", "dbus-daemon", "gdbus", "/usr/libexec/flatpak-portal",
                       "/usr/libexec/xdg-permission-store", "/usr/libexec/flatpak-session-helper"):
        if not shutil.which(executable):
            pytest.skip(f"{executable} is required for the real update handoff")
    initial = build_version("1.2.3", initial_runtime)
    subsequent = build_version("1.2.4")
    tmp_path = tmp_path / f"handoff-{initial_runtime}"
    tmp_path.mkdir()
    home = tmp_path / "clean-home"
    home.mkdir()
    env = {**build_env, "HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
           "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache")}
    # Do not inherit overrides that could point the test app at an existing user's database.
    env.pop("YT_PLAYLIST_HOME", None)

    def run(*args):
        result = subprocess.run([str(arg) for arg in args], env=env, cwd=ROOT,
                                text=True, capture_output=True, timeout=120)
        assert result.returncode == 0, f"{args}\n{result.stdout}\n{result.stderr}"
        return result.stdout.strip()

    def inside(code):
        return run("flatpak", "run", "--user", "--command=python3", APP, "-c", code)

    keys = tmp_path / "handoff-keys"
    keys.mkdir(mode=0o700)
    gpg = ["gpg", "--homedir", keys, "--batch", "--pinentry-mode", "loopback", "--passphrase", ""]
    run(*gpg, "--quick-generate-key", "Update Test <update@example.invalid>", "rsa2048", "sign", "1d")
    fingerprint = next(line.split(":")[9] for line in run(*gpg, "--with-colons", "--list-keys").splitlines()
                       if line.startswith("fpr:"))
    public_key = tmp_path / "handoff-public.gpg"
    run(*gpg, "--output", public_key, "--export", fingerprint)
    repo = tmp_path / "handoff-repo"

    def export(build):
        run("flatpak", "build-export", f"--gpg-homedir={keys}", f"--gpg-sign={fingerprint}", repo, build)
        run("flatpak", "build-update-repo", f"--gpg-homedir={keys}", f"--gpg-sign={fingerprint}", repo)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(repo)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    processes = []
    sockets = []
    log = (tmp_path / "handoff-processes.log").open("w+")

    def start(*args, **kwargs):
        process = subprocess.Popen([str(arg) for arg in args], env=env, cwd=ROOT,
                                   stderr=log, **kwargs)
        processes.append(process)
        return process

    try:
        # No activation directories: this bus cannot start services in the user's desktop session.
        bus_config = tmp_path / "bus.conf"
        bus_config.write_text(
            '<busconfig><type>session</type><listen>unix:tmpdir=' + str(tmp_path) + '</listen>'
            '<auth>EXTERNAL</auth><policy context="default"><allow send_destination="*"/>'
            '<allow receive_sender="*"/><allow own="*"/></policy></busconfig>')
        bus = start("dbus-daemon", "--nofork", f"--config-file={bus_config}", "--print-address=1",
                    stdout=subprocess.PIPE, text=True)
        env["DBUS_SESSION_BUS_ADDRESS"] = bus.stdout.readline().strip()
        assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:")
        start("/usr/libexec/xdg-permission-store", stdout=log)
        start("/usr/libexec/flatpak-session-helper", stdout=log)

        def wait_for(predicate, description, timeout=60):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(.2)
            log.flush()
            pytest.fail(f"Timed out waiting for {description}\n{(tmp_path / 'handoff-processes.log').read_text()}")

        def bus_owned(name):
            return "true" in run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                                 "--object-path", "/org/freedesktop/DBus", "--method",
                                 "org.freedesktop.DBus.NameHasOwner", name)

        wait_for(lambda: bus_owned("org.freedesktop.impl.portal.PermissionStore"), "private permission store")
        run("flatpak", "permission-set", "flatpak", "updates", APP, "yes")
        start("/usr/libexec/flatpak-portal", "--no-idle-exit", "--poll-timeout=1",
              "--poll-when-metered", stdout=log)
        wait_for(lambda: bus_owned("org.freedesktop.portal.Flatpak"), "private Flatpak portal")
        export(initial)
        bundle = runpy.run_path(ROOT / "packaging/flatpak/bundle.py")
        archive = tmp_path / "handoff.flatpak"
        origin_url = f"http://127.0.0.1:{server.server_port}/"
        bundle["build"](repo, archive, public_key, repo_url=origin_url)
        run("flatpak", "install", "--user", "--noninteractive", "--no-deps", "--no-related", archive)
        assert origin_url in run("flatpak", "remotes", "--user", "--columns=name,url")
        assert "gpg-verify=true" in (home / "data/flatpak/repo/config").read_text()
        old_commit = run("flatpak", "info", "--user", "--show-commit", APP)
        assert run("flatpak", "info", "--user", "--show-runtime", APP).endswith("/" + initial_runtime)
        seed = inside("""
import importlib.metadata as m, json, time
from yt_playlist.core import paths
from yt_playlist.core import updatecheck
from yt_playlist.core.store import Store
from yt_playlist.core.setup import write_config, BROWSER_CREDENTIAL_FILENAME
store=Store(paths.db_path()); store.init_schema()
iid=store.identities.upsert_identity('main', BROWSER_CREDENTIAL_FILENAME, None, True)
store.playlists.upsert_playlist(iid, 'PL-PRESERVED', 'Preserved update library', 0, 'hash', 0)
write_config([{'id':iid, 'label':'main', 'is_master':True,
               'credential_ref':BROWSER_CREDENTIAL_FILENAME}], paths.config_path())
store.settings.set_setting('latest_version_seen','1.2.4')
store.settings.set_setting('latest_version_checked_at',str(time.time()))
store.settings.set_setting('last_sync_at',str(time.time()))
print(json.dumps({'version':m.version('yt-playlist'), 'nudge':updatecheck.update_nudge(store),
 'config':paths.config_path().read_text(), 'database':str(paths.db_path())}))
""")
        snapshot = json.loads(seed)
        assert snapshot["version"] == "1.2.3", snapshot
        assert snapshot["nudge"] and snapshot["nudge"]["current"] == "1.2.3", snapshot
        assert Path(snapshot["database"]).is_relative_to(home), snapshot
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        instance_path = tmp_path / "initial-instance"
        with instance_path.open("w") as instance_file:
            old_process = start("flatpak", "run", "--user", "--command=yt-playlist",
                                f"--instance-id-fd={instance_file.fileno()}", APP,
                                "--port", port, stdout=log, pass_fds=(instance_file.fileno(),))

        def ready():
            try:
                return page.request.get(base_url + "/bridge/status", timeout=1000).ok
            except Exception:
                return False

        wait_for(ready, "initial packaged server")
        old_flatpak_instance = instance_path.read_text().strip()
        assert old_flatpak_instance.isdecimal()
        origin = "chrome-extension://opmmbolbdgdkphfocffpjpgmelkkjkcn"
        sockets.append(connect(f"ws://127.0.0.1:{port}/bridge/ws", origin=origin))
        page.goto(base_url + "/#notices")
        expect(page.locator("#update-nudge")).to_contain_text("You're using 1.2.3")
        wait_for(lambda: page.request.get(base_url + "/bridge/status").json()["connected"], "browser pairing")
        old_instance = page.request.get(base_url + "/app-update/status").json()["instance"]
        export(subsequent)
        page.get_by_role("button", name="Update now", exact=True).click()
        expect(page.get_by_role("button", name="Restart to update", exact=True)).to_be_visible(timeout=90_000)
        new_commit = run("flatpak", "info", "--user", "--show-commit", APP)
        assert new_commit != old_commit
        assert run("flatpak", "info", "--user", "--show-runtime", APP).endswith("/25.08")
        # Installation must leave the running version and this tab alive until restart is requested.
        assert old_process.poll() is None
        assert page.request.get(base_url + "/app-update/status").json()["instance"] == old_instance
        assert old_commit[:12] in run("flatpak", "ps", "--columns=commit")
        page.get_by_role("button", name="Restart to update", exact=True).click()
        if initial_runtime == "24.08":
            # Spawn retains the caller's runtime branch. Keep the current server
            # alive and guide a fresh desktop launch when the new ABI cannot start.
            expect(page.locator("#update-nudge .update-status")).to_contain_text(
                "applications menu", timeout=30_000)
            status = page.request.get(base_url + "/app-update/status").json()
            assert status["phase"] == "manual_restart" and status["instance"] == old_instance
            assert old_process.poll() is None
            assert page.request.get(base_url + "/bridge/status").json()["connected"]
            # Emulate closing the old desktop application and launching it afresh.
            run("flatpak", "kill", old_flatpak_instance)
            old_process.wait(timeout=15)
            start("flatpak", "run", "--user", "--command=yt-playlist", APP,
                  "--port", port, stdout=log)
        expect(page.locator("#update-nudge")).to_have_count(0, timeout=60_000)
        assert page.url == base_url + "/#notices"
        assert page.request.get(base_url + "/app-update/status").json()["instance"] != old_instance
        old_process.wait(timeout=15)
        assert new_commit[:12] in run("flatpak", "ps", "--columns=commit")
        # Check the persisted pairing before making another connection, then reconnect the same origin.
        state = json.loads(inside("""
import importlib.metadata as m, json
from yt_playlist.core import paths
from yt_playlist.core.store import Store
store=Store(paths.db_path()); store.init_schema()
print(json.dumps({'version':m.version('yt-playlist'), 'config':paths.config_path().read_text(),
 'database':str(paths.db_path()), 'paired':store.settings.get_setting('bridge_paired'),
 'playlists':[p.title for p in store.playlists.get_playlists()]}))
"""))
        assert state["version"] == "1.2.4"
        assert state["config"] == snapshot["config"]
        assert state["database"] == snapshot["database"]
        assert state["paired"] == "1"
        assert state["playlists"] == ["Preserved update library"]
        sockets.append(connect(f"ws://127.0.0.1:{port}/bridge/ws", origin=origin))
        assert page.request.get(base_url + "/bridge/status").json()["connected"]
        page.goto(base_url + "/playlists")
        expect(page.get_by_role("link", name="Preserved update library", exact=True)).to_be_visible()
    finally:
        for connection in sockets:
            connection.close()
        # Instance IDs belong exclusively to the temporary XDG_RUNTIME_DIR; never kill by app ID.
        result = subprocess.run(["flatpak", "ps", "--columns=instance,application"], env=env,
                                capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == APP:
                subprocess.run(["flatpak", "kill", fields[0]], env=env, capture_output=True, timeout=10)
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        subprocess.run(["gpgconf", "--homedir", str(keys), "--kill", "gpg-agent"], env=env,
                       capture_output=True, timeout=10)
        log.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
