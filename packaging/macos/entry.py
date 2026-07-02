"""PyInstaller entry point for the macOS app bundle.

Double-clicking the .app launches the local server and opens the default browser. Args still pass
through if run from a terminal (e.g. `YT\ Playlist.app/Contents/MacOS/yt-playlist --port 9000`).
"""
import sys

from yt_playlist.__main__ import main

if __name__ == "__main__":
    # default to --open so the GUI launch shows the UI; allow extra/overriding args from a terminal
    argv = sys.argv[1:]
    if "--open" not in argv:
        argv = ["--open", *argv]
    if "--exit-on-idle" not in argv:
        argv = ["--exit-on-idle", *argv]
    main(argv)
