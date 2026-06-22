import argparse
import logging
import socket
import threading
import time
import uvicorn
import webbrowser
from pathlib import Path
from yt_playlist.core import paths
from yt_playlist.core.store import Store
from yt_playlist.core.config import credential_path
from yt_playlist.core.runtime import Runtime
from yt_playlist.web.app import create_app

def sync_identities_into_store(store, identity_configs):
    """Ensure each configured identity exists in the store; return {label: id}."""
    return {cfg.label: store.upsert_identity(cfg.label, cfg.credential_ref, cfg.brand_account_id, cfg.is_master)
            for cfg in identity_configs}

def validate_credentials(identity_configs, base_dir=None):
    """Check that each identity's credential file exists; raise SystemExit if any are missing."""
    if base_dir is None:
        base_dir = paths.config_path().parent
    base_dir = Path(base_dir)
    for cfg in identity_configs:
        try:
            path = credential_path(base_dir, cfg.credential_ref)
        except ValueError as e:
            raise SystemExit(f"Identity '{cfg.label}': {e}")
        if not path.exists():
            raise SystemExit(f"Identity '{cfg.label}': credential file not found at {path}")

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="yt-playlist",
        description="Local web tool to dedupe, merge, and prune YouTube Music playlists across "
                    "YouTube brand identities.",
        epilog=f"Config and credentials live in {paths.config_path().parent} "
               f"(override the location with YT_PLAYLIST_HOME). Data is stored in "
               f"{paths.data_dir()}.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: 127.0.0.1, loopback only)")
    parser.add_argument("--port", type=int, default=8765,
                        help="port to listen on (default: 8765)")
    parser.add_argument("--reload", action="store_true",
                        help="dev: auto-restart the server when source files change "
                             "(needs the dev extras for watchfiles)")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="open the app in your default web browser once the server is up "
                             "(used by the packaged Linux/macOS launchers)")
    return parser.parse_args(argv)

def build_app():
    """Construct the ASGI app from on-disk config. Importable so uvicorn --reload can re-import it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Install the egress guard before anything can make a network call: from here on every
    # server-side HTTP request is allowlisted and logged (see yt_playlist.egress, read at /network).
    from yt_playlist import egress
    egress.install()
    store = Store(paths.db_path())
    store.init_schema()
    config_path = paths.config_path()
    runtime = Runtime(store, config_path, config_path.parent)
    runtime.load()
    if not runtime.configured:
        logging.getLogger(__name__).warning("no usable config yet — open /setup to finish setup")
    return create_app(store, runtime.clients, now_fn=time.time, setup=runtime)

def _open_browser_when_ready(host, port):
    """Wait (briefly) for the server to accept connections, then open it in the default browser.
    Runs in a daemon thread so it never blocks or delays shutdown."""
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    for _ in range(150):                        # up to ~15s; the server is usually up in well under 1s
        try:
            with socket.create_connection((target, port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    webbrowser.open(f"http://{target}:{port}/")

def main(argv=None):
    args = parse_args(argv)
    if args.open_browser:
        threading.Thread(target=_open_browser_when_ready, args=(args.host, args.port), daemon=True).start()
    if args.reload:
        # The reloader runs the app in a child process that re-imports this module on change,
        # so it needs an import string + factory, not an already-built app object. Watch only
        # src/ to avoid churning on node_modules/.venv.
        src_dir = str(Path(__file__).resolve().parent.parent)
        uvicorn.run("yt_playlist.__main__:build_app", factory=True, reload=True,
                    reload_dirs=[src_dir], host=args.host, port=args.port,
                    timeout_graceful_shutdown=2)
    else:
        # bound graceful shutdown so a Ctrl-C lands promptly even with an open sync SSE stream
        uvicorn.run(build_app(), host=args.host, port=args.port, timeout_graceful_shutdown=2)

if __name__ == "__main__":
    main()
