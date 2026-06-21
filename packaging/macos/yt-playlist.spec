# PyInstaller spec — builds "YT Playlist.app", a self-contained bundle with Python + all deps.
# Build with packaging/macos/build.sh (which installs the project + pyinstaller into a venv first).
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# Bundle the package data the server needs at runtime, plus libraries that load their own data.
datas = []
datas += collect_data_files("yt_playlist")        # web/templates/*.html, web/static/** (incl. vendor JS)
datas += collect_data_files("ytmusicapi")          # bundled locale/i18n JSON
datas += copy_metadata("yt_playlist")              # version metadata (importlib.metadata lookups)

# uvicorn picks its loop/protocol implementations by dynamic import — make them discoverable.
hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

icon = "YtPlaylist.icns" if os.path.exists("YtPlaylist.icns") else None

a = Analysis(
    ["entry.py"],
    pathex=["../../src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="yt-playlist",
    console=False,            # GUI launch: no terminal window
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="yt-playlist")

app = BUNDLE(
    coll,
    name="YT Playlist.app",
    icon=icon,
    bundle_identifier="io._4rc.YtPlaylist",
    version="0.1.0",
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        # the server has no window; keep it a normal app so it appears in the Dock and can be quit
        "LSBackgroundOnly": False,
        "LSMinimumSystemVersion": "11.0",
    },
)
