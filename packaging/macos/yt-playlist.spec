# PyInstaller spec — builds "YT Playlist.app", a self-contained bundle with Python + all deps.
# Build with packaging/macos/build.sh (which installs the project + pyinstaller into a venv first).
import importlib.metadata
import os

# Version comes from the git tag via hatch-vcs, baked into the installed package metadata — nothing to
# bump here. Sanitize a dev version (0.1.3.dev3+g...) to plain X.Y.Z for the CFBundle keys.
_VERSION = importlib.metadata.version("yt-playlist").split(".dev")[0].split("+")[0]

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
# Belt-and-suspenders for two deps loaded via lazy/indirect imports: python-multipart (starlette
# imports it only when parsing a form POST — e.g. the setup wizard) and websockets (the /bridge/ws
# extension endpoint). collect_submodules is a no-op if a package is absent, so this is safe.
hiddenimports += collect_submodules("multipart")
hiddenimports += collect_submodules("websockets")

icon = "TuneConsole.icns" if os.path.exists("TuneConsole.icns") else None

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
    name="TuneConsole.app",
    icon=icon,
    bundle_identifier="com.tuneconsole.TuneConsole",
    version=_VERSION,
    info_plist={
        "CFBundleShortVersionString": _VERSION,
        "CFBundleVersion": _VERSION,
        "NSHighResolutionCapable": True,
        # the server has no window; keep it a normal app so it appears in the Dock and can be quit
        "LSBackgroundOnly": False,
        "LSMinimumSystemVersion": "11.0",
    },
)
