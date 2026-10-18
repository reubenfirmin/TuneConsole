"""PyInstaller entry point for the macOS app bundle.

Double-clicking the .app launches the local server and opens the default browser. Args still pass
through if run from a terminal (e.g. `YT\ Playlist.app/Contents/MacOS/yt-playlist --port 9000`).
"""
import sys

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        from yt_playlist.__main__ import main
        main(argv)
    else:
        from yt_playlist.macos import run
        run(argv)
