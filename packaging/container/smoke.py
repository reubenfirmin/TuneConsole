#!/usr/bin/env python3
"""Exercise a built image with Docker or rootless Podman, using disposable data."""
import argparse
import base64
import hashlib
import http.client
import json
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", help="expected installed package version")
    args = parser.parse_args()
    name = "tuneconsole-smoke-" + uuid.uuid4().hex[:12]
    volume = name + "-data"

    def run(*command, check=True):
        result = subprocess.run([args.engine, *command], text=True, capture_output=True,
                                timeout=90)
        if check and result.returncode:
            raise RuntimeError(f"{args.engine} {' '.join(command)}\n"
                               f"{result.stdout}{result.stderr}")
        return result.stdout.strip()

    def request(port, path, *, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            # Do not read a WebSocket stream as an HTTP body.
            body = b"" if response.status == 101 else response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            conn.close()

    def start():
        run("run", "--detach", "--name", name,
            "--publish", "127.0.0.1::8765", "--volume", f"{volume}:/data", args.image)
        address = run("port", name, "8765/tcp")
        assert address.startswith("127.0.0.1:"), address
        port = int(address.rsplit(":", 1)[1])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                status, _, body = request(port, "/bridge/status")
                if status == 200 and "now_playing" in json.loads(body):
                    return port
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.25)
        raise RuntimeError("The app did not become ready within 60 seconds")

    def stop():
        run("stop", "--time", "10", name)
        code = run("inspect", "--format", "{{.State.ExitCode}}", name)
        assert code == "0", f"Server did not stop cleanly: exit {code}"
        run("rm", name)

    try:
        run("volume", "create", volume)
        port = start()
        for path, content in (("/setup", b"TuneConsole"), ("/static/favicon.svg", b"<svg")):
            status, _, body = request(port, path)
            assert status == 200 and content in body, (path, status)

        info = json.loads(run("exec", name, "python", "-c", """
import importlib.metadata, json, os
from yt_playlist.core import paths, updatecheck
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN
assert os.getuid() != 0, 'The app must run as a regular user'
assert str(paths.data_dir()) == '/data'
assert str(paths.config_path()) == '/data/config.toml'
assert os.environ['YT_PLAYLIST_CONTAINER'] == '1'
assert paths.app_log_path().is_file()
assert updatecheck.is_dev_install() or updatecheck.install_kind() == 'container'
print(json.dumps({'version': importlib.metadata.version('yt-playlist'),
                  'origin': EXTENSION_ORIGIN}))
"""))
        if args.version:
            assert info["version"] == args.version, info

        key = base64.b64encode(uuid.uuid4().bytes).decode()
        headers = {"Connection": "Upgrade", "Upgrade": "websocket",
                   "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": key,
                   "Origin": info["origin"]}
        status, response_headers, _ = request(port, "/bridge/ws", headers=headers)
        assert status == 101, f"Extension pairing failed: HTTP {status}"
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        assert {k.lower(): v for k, v in response_headers.items()}["sec-websocket-accept"] == expected
        for origin in (None, "https://example.com"):
            rejected_headers = {k: v for k, v in headers.items() if k != "Origin"}
            if origin:
                rejected_headers["Origin"] = origin
            status, _, _ = request(port, "/bridge/ws", headers=rejected_headers)
            assert status == 403, f"Untrusted bridge origin was accepted: {origin}"

        status, _, _ = request(port, "/onboard/update/dismiss", method="POST",
                               headers={"Origin": "https://example.com"})
        assert status == 403, "Cross-site writes must remain blocked"

        run("exec", name, "python", "-c", """
from yt_playlist.core import paths
from yt_playlist.core.store import Store
store = Store(paths.db_path())
store.init_schema()
store.set_setting('container_smoke', 'persisted')
(paths.backups_dir() / 'container-smoke.txt').write_text('persisted')
""")
        stop()
        port = start()
        run("exec", name, "python", "-c", """
from yt_playlist.core import paths
from yt_playlist.core.store import Store
store = Store(paths.db_path())
store.init_schema()
assert store.get_setting('container_smoke') == 'persisted'
assert (paths.backups_dir() / 'container-smoke.txt').read_text() == 'persisted'
assert paths.app_log_path().is_file()
""")
        stop()
        print(f"{args.engine}: {args.image} passed startup, pairing, origin guards, "
              "non-root storage, persistence, and graceful shutdown checks")
    except BaseException:
        print(run("logs", name, check=False))
        raise
    finally:
        run("rm", "--force", name, check=False)
        run("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
