"""Check an installed candidate on a disposable CI user, without opening a host browser."""
import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import uuid

APP = "com.tuneconsole.TuneConsole"


def check(version, launch, *, installed=True):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def inside(code):
        return subprocess.run(launch("python3") + ["-c", code],
                              check=True, capture_output=True, text=True, timeout=120).stdout.strip()

    info = json.loads(inside("""
import importlib.metadata as m, json
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN
from yt_playlist.core import updatecheck
assert updatecheck.install_kind() == 'flatpak'
print(json.dumps({'version':m.version('yt-playlist'),'origin':EXTENSION_ORIGIN}))
"""))
    assert info["version"] == version, info
    if installed:
        origin = subprocess.run(["flatpak", "info", "--user", "--show-origin", APP],
                                check=True, capture_output=True, text=True).stdout.strip()
        assert origin, "Bundle did not configure an application update origin"

    def request(path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, b"" if response.status == 101 else response.read()
        finally:
            connection.close()

    process = subprocess.Popen(launch("yt-playlist") + ["--port", str(port)], start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Packaged server exited before becoming ready")
            try:
                if request("/bridge/status")[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.2)
        else:
            raise RuntimeError("Packaged server did not become ready")
        status, page = request("/setup")
        assert status == 200 and b"TuneConsole" in page
        headers = {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                   "Sec-WebSocket-Key": base64.b64encode(uuid.uuid4().bytes).decode(), "Origin": info["origin"]}
        assert request("/bridge/ws", headers)[0] == 101
        headers["Origin"] = "https://example.com"
        assert request("/bridge/ws", headers)[0] == 403
        inside("""
from yt_playlist.core import paths
from yt_playlist.core.store import Store
store=Store(paths.db_path()); store.init_schema()
store.settings.set_setting('flatpak_smoke','persisted')
assert paths.app_log_path().is_file()
""")
    finally:
        # flatpak build can exit without forwarding a signal to its sandbox child.
        # This private process group belongs to this smoke launch only.
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    # Flatpak's wrapper may retain SIGTERM's status after closing the sandbox.
    assert process.returncode in (0, -signal.SIGTERM, 128 + signal.SIGTERM), process.returncode
    inside("""
from yt_playlist.core import paths
from yt_playlist.core.store import Store
store=Store(paths.db_path()); store.init_schema()
assert store.settings.get_setting('flatpak_smoke')=='persisted'
""")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--build-dir", type=Path, help="Check a local build without installing it")
    parser.add_argument("--data-dir", type=Path, help="Disposable storage for a local build check")
    args = parser.parse_args()
    # Sandbox children must not outlive this check and retain the caller's captured pipes.
    if args.build_dir:
        if not args.data_dir:
            parser.error("--build-dir requires --data-dir")
        args.data_dir.mkdir(parents=True, exist_ok=True)

        def launch(command):
            return ["flatpak", "build", "--runtime", "--readonly", "--die-with-parent", "--share=network",
                    f"--bind-mount=/run/tuneconsole-smoke={args.data_dir.resolve()}",
                    "--env=YT_PLAYLIST_HOME=/run/tuneconsole-smoke",
                    str(args.build_dir.resolve()), command]
    else:
        def launch(command):
            return ["flatpak", "run", "--user", "--die-with-parent", f"--command={command}", APP]
    check(args.version, launch, installed=not args.build_dir)


if __name__ == "__main__":
    main()
